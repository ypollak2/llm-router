"""NS-L (D-447/D-448/D-449): ``--serve local-agent --local-with-tiers`` and the session exemption.

Combined mode: an allowed turn is served locally exactly as local-agent does today; every other turn
(vetoed, ineligible, kill-switched, exempt, failed) goes through the EXISTING tier path unchanged, so its
upstream request bytes and tier_* ledger fields equal the ones a ``--tiers conversation`` proxy writes for the
same request. The old tiers-off local-agent mode is unchanged (tests/test_proxy_local_mode.py runs beside this).

No Ollama, no network: the classifier is injected, Anthropic is a mocked upstream, the local model a fake
backend (helpers are reused from test_proxy_local_mode / test_proxy_cls_decide / test_proxy_tiers).
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from llm_router import paths, session_kind
from llm_router.proxy import backends as pb
from llm_router.proxy import ledger, local_mode
from llm_router.proxy import server as ps
from tests.test_proxy_cls_decide import (  # noqa: F401  (fixtures and helpers)
    _STABLE_DROP, ADDITIVE, SID, FakeClassifier, _env, decision_model_on, passing_proxy_lane, turn_body,
)
from tests.test_proxy_local_mode import (  # noqa: F401  (autouse fixture and helpers)
    GOOD_RUNNER, MODEL, Backend, Breaker, _call, _first_call, _next_turn, _ollama, _overrides_readable, _pre, _runner, _served_blocks,
    _with_image,
)
from tests.test_proxy_tiers import OPUS, SONNET, Upstream, _raw_policy, _sse  # noqa: F401

#: Keys only a local-agent row carries (or sets differently): they are the local step's own bookkeeping.
LOCAL_KEYS = {"serve_mode", "local_mode", "egress", "reason", "detail", "cls_applied"}


@pytest.fixture(autouse=True)
def _moderate(monkeypatch):
    """The tier classifier calls every request moderate: the policy rewrites Opus to Sonnet."""
    async def choose(text, pinned, *, anthropic=False):
        return {"task_type": "code", "complexity": "moderate", "chain_head": [], "model": None}

    monkeypatch.setattr(pb, "tier_classify", choose)

    async def _boom(*a, **k):
        raise AssertionError("local-agent mode must not consult the routing policy (P2)")

    monkeypatch.setattr(ps, "choose_model", _boom)


def _policy_file(tmp_path):
    import yaml

    raw = _raw_policy()
    raw["stickiness"]["switch_after_first_call"] = True
    path = tmp_path / "tiers.yaml"
    path.write_text(yaml.safe_dump(raw))
    return str(path)


class Rig:
    """One proxy app + mocked Anthropic + fake local backend, writing under ``tmp_path``."""

    def __init__(self, tmp_path, *, combined=True, upstream=None, backend=None, kill="own", **cfg):
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.up = upstream or Upstream()
        self.backend = backend or Backend(_ollama("done"))
        self.ledger = tmp_path / "proxy_calls.jsonl"
        self.kill = tmp_path / "kill" if kill == "own" else None
        base = dict(steps=frozenset(), upstream="http://127.0.0.1:9", ledger_path=self.ledger, kill_switch=self.kill,
                    tiers=ps.TIERS_CONVERSATION, tier_policy=_policy_file(tmp_path))
        if combined:
            base.update(model=MODEL, trim="none", serve="local-agent", hedge_s=None, warm_up=False,
                        local_with_tiers=True)
        base.update(cfg)
        self.app = ps.build_app(ps.ProxyConfig(**base), client=httpx.AsyncClient(transport=httpx.MockTransport(self.up)),
                                backend_factory=lambda m: self.backend, breaker_fn=Breaker())

    def post(self, body):
        headers = {"authorization": "Bearer sk-ant-oat01-" + "Zq9" * 20, "anthropic-version": "2023-06-01",
                   "content-type": "application/json"}

        async def go():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                         base_url="http://127.0.0.1:8787") as c:
                return await c.post("/v1/messages?beta=true", content=json.dumps(body), headers=headers)

        return asyncio.run(go())

    def rows(self):
        return ledger.read_rows(self.ledger)

    def sent(self):
        return [json.loads(r.content) for r in self.up.requests]


def opus(body):
    body["model"] = OPUS
    return body


def comparable(row, *, drop_local=True):
    drop = set(_STABLE_DROP) | {"tier_shadow_schedule_ms"} | ADDITIVE | (LOCAL_KEYS if drop_local else set())
    return {k: v for k, v in row.items() if k not in drop}


# -- combined mode: allowed -> local, vetoed -> the tier path ---------------------------------------------------


def test_an_allowed_turn_is_served_locally_and_never_reaches_anthropic(tmp_path, monkeypatch):
    decision_model_on(monkeypatch)
    passing_proxy_lane(monkeypatch)
    FakeClassifier(monkeypatch, tier="haiku")
    rig = Rig(tmp_path)
    r = rig.post(opus(turn_body(["rename the helper"])))
    assert r.status_code == 200 and rig.up.requests == [] and len(rig.backend.calls) == 1
    (row,) = rig.rows()
    assert row["decision"] == ledger.DECISION_SERVED and row["serve_mode"] == "local-agent"
    assert row["local_mode"]["pin"] == "local" and row["model"] == MODEL
    assert "tier_reason" not in row or row["tier_reason"] is None  # the tier policy never saw a local turn


def test_a_vetoed_turn_takes_the_same_tier_path_as_tiers_alone(tmp_path, monkeypatch):
    decision_model_on(monkeypatch)
    FakeClassifier(monkeypatch, tier="opus")
    combined = Rig(tmp_path / "combined")
    plain = Rig(tmp_path / "plain", combined=False)
    for rig in (combined, plain):
        assert rig.post(opus(turn_body(["design a multi-file change"]))).status_code == 200
    assert combined.backend.calls == []
    (crow,), (prow,) = combined.rows(), plain.rows()
    assert crow["reason"] == "classifier_veto" and crow["cls_applied"] is True and crow["egress"] is True
    assert [q.content for q in combined.up.requests] == [q.content for q in plain.up.requests]   # same bytes
    assert combined.sent()[0]["model"] == SONNET                                                  # same rewrite
    assert comparable(crow) == comparable(prow)                      # tier_* and every other field alike
    assert crow["tier_reason"] == prow["tier_reason"] and crow["served_model"] == SONNET


@pytest.mark.parametrize("how", ["decision_unset", "shadow"])
def test_with_no_active_classifier_nothing_is_vetoed_and_ineligible_turns_equal_tiers_alone(tmp_path, monkeypatch, how):
    """Decision model unset, or door mode shadow: no veto, so an eligible turn is served locally; an
    ineligible one (side call without tools, media) takes the tier path and its row equals the tiers-only row
    apart from the A1 keys and the local step's own bookkeeping."""
    if how == "shadow":
        decision_model_on(monkeypatch)
        monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "shadow")
    fake = FakeClassifier(monkeypatch, tier="opus")
    combined = Rig(tmp_path / "combined")
    plain = Rig(tmp_path / "plain", combined=False)
    side = turn_body(["title this"])
    side.pop("tools")
    image = _with_image(opus(turn_body(["what is this"])))
    served = combined.post(opus(turn_body(["fix the bug"])))
    assert served.status_code == 200 and combined.up.requests == [] and combined.rows()[0]["decision"] == "served"
    for body in (opus(side), image):
        combined.post(body)
        plain.post(body)
    if how == "decision_unset":
        assert fake.calls == []          # shadow mode may still log a verdict on the tier path; it applies nothing
    crows, (prow_side, prow_img) = combined.rows()[1:], plain.rows()
    assert [r["reason"] for r in crows] == ["not_eligible", "media_present"]
    assert [q.content for q in combined.up.requests] == [q.content for q in plain.up.requests]
    assert [comparable(r) for r in crows] == [comparable(prow_side), comparable(prow_img)]
    assert all(r["cls_applied"] is False for r in combined.rows())


def test_a_failed_local_step_pins_to_claude_and_that_turn_takes_the_tier_path(tmp_path, monkeypatch):
    """D-442: a failed local step goes to Claude and the conversation stays there, disclosed."""
    decision_model_on(monkeypatch)
    passing_proxy_lane(monkeypatch)
    FakeClassifier(monkeypatch, tier="haiku")
    rig = Rig(tmp_path, backend=Backend(_ollama("", [_call("Nope", {})])))
    assert rig.post(opus(turn_body(["rename the helper"]))).status_code == 200
    assert len(rig.backend.calls) == 1 and len(rig.up.requests) == 1
    row = rig.rows()[0]
    assert row["decision"] == "fallback" and row["local_mode"]["pin_change"] == "local->claude"
    assert row["tier_reason"] and row["served_model"] == SONNET
    rig.post(opus(turn_body(["rename the helper"], loops=1)))
    assert len(rig.backend.calls) == 1 and rig.rows()[1]["reason"] == "pinned_claude"
    assert rig.rows()[1]["tier_reason"]


# -- a Claude turn after a local turn keeps its history and the mixed-history guard ----------------------------


def test_a_claude_turn_after_a_local_turn_goes_through_the_tier_path_with_the_history_and_the_thinking_retry(
        tmp_path, monkeypatch):
    decision_model_on(monkeypatch)
    passing_proxy_lane(monkeypatch)
    FakeClassifier(monkeypatch, tier="haiku")
    rejection = json.dumps({"type": "error", "error": {"type": "invalid_request_error", "message":
                            "messages.3.content.0: Expected `thinking` or `redacted_thinking`"}}).encode()
    rig = Rig(tmp_path / "combined", upstream=Upstream([(400, rejection)]),
              backend=Backend(_ollama("", [_call("Read", {"file_path": "/work/repo/a.py"})])))
    first = rig.post(opus(_first_call()))
    assert rig.up.requests == [] and len(rig.backend.calls) == 1
    nxt = opus(dict(_next_turn(_first_call(), {"content": _served_blocks(first)}), stream=False))
    rig.kill.write_text("")                                    # the D1/P4 pause: the next step must go to Claude
    assert rig.post(nxt).status_code == 200
    assert len(rig.backend.calls) == 1                         # the local model was not asked again
    row = rig.rows()[1]
    assert row["reason"] == "kill_switch" and row["mixed_history"] is True and row["thinking_retry"] is True
    sent = rig.sent()
    assert len(sent) == 2 and "thinking" in sent[0] and "thinking" not in sent[1]   # retried once, without thinking
    assert sent[0]["messages"] == nxt["messages"] == sent[1]["messages"]            # the history is intact
    assert row["tier_mode"] == "conversation" and row["tier_reason"]                # decided by the tier policy


# -- D-448: exemption ------------------------------------------------------------------------------------------


def write_overrides(content):
    path = session_kind.overrides_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content if isinstance(content, str) else json.dumps(content))
    return path


@pytest.fixture(autouse=True)
def clean_overrides(monkeypatch):
    """A readable, empty overrides file unless a test writes its own (or unlinks it)."""
    monkeypatch.setattr(session_kind, "_OVERRIDES_CACHE", {})
    monkeypatch.setattr(session_kind, "_OVERRIDES_PROBLEM", {})
    path = session_kind.overrides_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}")
    yield path
    path.unlink(missing_ok=True)


@pytest.mark.parametrize("kind", ["research", "harness", "headless"])
def test_a_listed_non_organic_session_is_exempt_and_goes_to_the_tier_path(tmp_path, clean_overrides, kind):
    write_overrides({SID: {"kind": kind, "reason": "D-335 coordinator"}})
    rig = Rig(tmp_path)
    rig.post(opus(turn_body(["fix the bug"])))
    rig.post(opus(turn_body(["fix the bug"], loops=2)))
    first, second = rig.rows()
    assert rig.backend.calls == [] and len(rig.up.requests) == 2
    for row in (first, second):
        assert row["reason"] == "exempt_session" and row["local_mode"]["reason"] == "exempt_session"
        assert row["local_mode"]["detail"] == "coordinator_session" and row["egress"] is True
        assert row["decision"] == ledger.DECISION_FORWARDED and row["tier_reason"]
        assert "overrides_unreadable" not in row["local_mode"]
    assert first["local_mode"]["pin_change"] == "new->claude"


def test_a_session_listed_as_organic_or_not_listed_is_not_exempt(tmp_path, clean_overrides):
    write_overrides({SID: {"kind": "organic", "reason": "explicitly organic"},
                     "other-session": {"kind": "research", "reason": "x"}})
    rig = Rig(tmp_path)
    rig.post(opus(turn_body(["fix the bug"])))
    (row,) = rig.rows()
    assert row["decision"] == ledger.DECISION_SERVED and len(rig.backend.calls) == 1 and rig.up.requests == []


@pytest.mark.parametrize("content,detail,problem", [
    (None, "missing", "missing"), ("{not json", "overrides_unreadable", "unreadable"),
    ('["a list"]', "overrides_unreadable", "unreadable"), ("", "overrides_unreadable", "unreadable"),
])
def test_an_unreadable_overrides_file_refuses_every_session_and_says_so_in_the_row(
        tmp_path, clean_overrides, content, detail, problem):
    """Fail closed (coordinator decision): when the file is missing, unreadable or malformed no session is
    served locally; each is refused exempt_session and the row records the problem."""
    if content is None:
        clean_overrides.unlink()
    else:
        write_overrides(content)
    rig = Rig(tmp_path)
    rig.post(opus(turn_body(["fix the bug"])))
    (row,) = rig.rows()
    assert rig.backend.calls == [] and len(rig.up.requests) == 1
    assert row["reason"] == "exempt_session" and row["local_mode"]["detail"] == detail
    assert row["local_mode"]["overrides_unreadable"] == problem and row["egress"] is True


def test_a_readable_file_listing_only_other_sessions_allows_an_organic_session(tmp_path, clean_overrides):
    write_overrides({"someone-else": {"kind": "research", "reason": "coordinator"}})
    rig = Rig(tmp_path)
    rig.post(opus(turn_body(["fix the bug"])))
    (row,) = rig.rows()
    assert row["decision"] == ledger.DECISION_SERVED and "overrides_unreadable" not in row["local_mode"]


def test_the_overrides_file_is_re_read_per_turn_without_a_restart(tmp_path, clean_overrides):
    rig = Rig(tmp_path, backend=Backend(_ollama("", [_call("Bash", {"command": "ls"})])))
    rig.post(opus(turn_body(["fix the bug"], sid="aaaaaaaa-0000-0000-0000-000000000001")))
    write_overrides({"aaaaaaaa-0000-0000-0000-000000000002": {"kind": "research", "reason": "coordinator"}})
    rig.post(opus(turn_body(["fix the bug"], sid="aaaaaaaa-0000-0000-0000-000000000002")))
    first, second = rig.rows()
    assert first["decision"] == "served" and second["reason"] == "exempt_session"


SUBAGENT_SYSTEM = [{"type": "text", "text": "You are an agent for Claude Code, Anthropic's official CLI for Claude."},
                   {"type": "text", "text": "Agent threads always have their cwd reset between bash calls."}]


@pytest.mark.parametrize("how", ["system_marker", "handback_tool"])
def test_a_subagent_thread_is_exempt_and_goes_to_the_tier_path(tmp_path, clean_overrides, how):
    body = opus(turn_body(["look into the failing test"]))
    if how == "system_marker":
        body["system"] = list(body["system"]) + SUBAGENT_SYSTEM
    else:
        body["tools"] = body["tools"] + [{"name": "SubagentHandback", "description": "hand back",
                                          "input_schema": {"type": "object", "properties": {}}}]
    rig = Rig(tmp_path)
    rig.post(body)
    (row,) = rig.rows()
    assert rig.backend.calls == [] and len(rig.up.requests) == 1
    assert row["reason"] == "exempt_session" and row["local_mode"]["detail"] == "subagent_thread"
    assert row["egress"] is True and row["tier_reason"]


def test_a_main_thread_session_is_not_taken_for_a_subagent(tmp_path, clean_overrides):
    body = opus(turn_body(["fix the bug"]))
    body["tools"] = body["tools"] + [{"name": "Agent", "description": "launch",
                                      "input_schema": {"type": "object", "properties": {}}}]
    rig = Rig(tmp_path)
    rig.post(body)
    (row,) = rig.rows()
    assert row["is_main_thread"] is True and row["decision"] == "served"


def test_the_exemption_also_holds_in_the_old_tiers_off_mode(tmp_path, clean_overrides):
    write_overrides({SID: {"kind": "research", "reason": "coordinator"}})
    rig = Rig(tmp_path, tiers=ps.TIERS_OFF, tier_policy=None, local_with_tiers=False)
    rig.post(turn_body(["fix the bug"]))
    (row,) = rig.rows()
    assert row["reason"] == "exempt_session" and rig.backend.calls == [] and len(rig.up.requests) == 1


def test_a_subagent_brief_turn_with_no_marker_is_refused_on_thread_evidence_alone(tmp_path, clean_overrides):
    """rv40: a spawned agent's turn (no Agent/Task launcher among its tools, so is_main_thread is False and
    turn_origin is subagent_brief) carried none of the system-prompt markers and was attempted locally."""
    body = opus(turn_body(["write the helper"]))
    body["tools"] = [t for t in body["tools"] if t.get("name") not in ("Agent", "Task")]
    assert body["tools"]
    rig = Rig(tmp_path)
    rig.post(body)
    (row,) = rig.rows()
    assert row["is_main_thread"] is False and row["turn_origin"] == "subagent_brief"
    assert row["reason"] == "exempt_session" and row["local_mode"]["detail"] == "subagent_thread"
    assert rig.backend.calls == [] and len(rig.up.requests) == 1


def test_a_main_thread_turn_still_serves_after_the_thread_rule(tmp_path, clean_overrides):
    rig = Rig(tmp_path)
    assert rig.post(opus(turn_body(["rename the helper"]))).status_code == 200
    assert rig.rows()[0]["decision"] == "served" and rig.rows()[0]["is_main_thread"] is True


class _Stalled(httpx.AsyncByteStream):
    async def __aiter__(self):
        await asyncio.sleep(5)
        yield b""


def test_ollama_backend_gives_up_at_the_hedge_when_no_first_token_arrives(tmp_path):
    from llm_router.proxy.backends import HedgeTimeout, OllamaBackend
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=_Stalled())))
    be = OllamaBackend(MODEL, client, base_url="http://127.0.0.1:9", num_ctx=8192, hedge_s=0.2, num_predict=50)

    async def go():
        t0 = asyncio.get_running_loop().time()
        with pytest.raises(HedgeTimeout):
            await be.complete(turn_body(["hi"]), 30.0)
        return asyncio.get_running_loop().time() - t0

    assert asyncio.run(go()) < 2.0


def test_a_hedge_timeout_in_combined_mode_falls_back_to_the_tier_path_and_pins_claude(tmp_path):
    from llm_router.proxy.backends import HedgeTimeout

    class Stall:
        calls = []

        async def complete(self, body, timeout_s):
            self.calls.append(1)
            raise HedgeTimeout("no first token within 30.0s")

    rig = Rig(tmp_path, backend=Stall())
    r = rig.post(opus(turn_body(["rename the helper"])))
    (row,) = rig.rows()
    assert r.status_code == 200 and len(rig.up.requests) == 1 and row["decision"] != "served"
    assert row["reason"] == "hedge_timeout" and row["local_mode"]["pin_change"] == "local->claude"   # D-442


def test_combined_mode_defaults_to_a_bounded_first_token_deadline_and_old_mode_to_none(monkeypatch, tmp_path):
    import uvicorn
    seen = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    monkeypatch.setattr(ps, "_local_agent_preflight", lambda cfg: [])
    real = ps.build_app
    monkeypatch.setattr(ps, "build_app", lambda cfg, *a, **k: seen.append(cfg.hedge_s) or real(cfg, *a, **k))
    base = ["--serve", "local-agent", "--model", MODEL, "--no-warm-up"]
    ps.cmd_proxy(base + ["--local-with-tiers", "--tiers", "conversation", "--ledger", str(tmp_path / "a.jsonl")])
    ps.cmd_proxy(base + ["--ledger", str(tmp_path / "b.jsonl")])
    ps.cmd_proxy(base + ["--local-with-tiers", "--tiers", "conversation", "--hedge-s", "off",
                         "--ledger", str(tmp_path / "c.jsonl")])
    assert seen == [local_mode.COMBINED_FIRST_TOKEN_S, None, None]


def test_the_old_tiers_off_mode_also_refuses_every_session_when_the_overrides_file_is_missing(tmp_path, clean_overrides):
    clean_overrides.unlink()
    rig = Rig(tmp_path, tiers=ps.TIERS_OFF, tier_policy=None, local_with_tiers=False)
    rig.post(turn_body(["fix the bug"]))
    (row,) = rig.rows()
    assert rig.backend.calls == [] and len(rig.up.requests) == 1
    assert row["reason"] == "exempt_session" and row["local_mode"]["detail"] == "missing"
    assert row["local_mode"]["overrides_unreadable"] == "missing"


def test_decide_local_orders_the_exemption_after_the_kill_switch_and_before_the_pin():
    body = _first_call()
    assert local_mode.decide_local(body, local_mode.LocalSession(exempt="coordinator_session")).reason == "exempt_session"
    both = local_mode.LocalSession(exempt="subagent_thread", pinned=local_mode.PIN_CLAUDE)
    assert local_mode.decide_local(body, both).reason == "exempt_session"
    killed = local_mode.LocalSession(exempt="subagent_thread", kill_switch=True)
    assert local_mode.decide_local(body, killed).reason == "kill_switch"
    assert local_mode.decide_local(body, local_mode.LocalSession()).local is True


# -- preflight, selection and startup ---------------------------------------------------------------------------


def test_preflight_drops_the_tiers_off_requirement_only_for_the_combined_mode(tmp_path):
    assert _pre(tmp_path, tiers_off=False, with_tiers=True) == []
    assert any("--tiers must be off" in p for p in _pre(tmp_path, tiers_off=False))
    assert any("--local-with-tiers needs --tiers" in p for p in _pre(tmp_path, tiers_off=True, with_tiers=True))
    assert _pre(tmp_path, tiers_off=True) == []
    # everything else is still required in the combined mode
    assert any("trim must be 'none'" in p for p in _pre(tmp_path, tiers_off=False, with_tiers=True, trim="fast"))
    assert any("num_ctx 8192" in p for p in _pre(tmp_path, tiers_off=False, with_tiers=True,
                                                  get=lambda u: {"models": [{"name": "fake:1", "context_length": 8192}]}))


def test_the_preflight_resident_check_and_np_check_use_the_ollama_url_flag(monkeypatch, tmp_path):
    """D-449: the plist passes --ollama-url http://127.0.0.1:11435; every Ollama question must go there."""
    asked = []

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"models": [{"name": "qwen3.8-uncensored:latest", "context_length": 32768}]}

    monkeypatch.setattr(httpx, "get", lambda url, **k: (asked.append(url), Resp())[1])
    monkeypatch.setattr(local_mode, "server_num_parallel", lambda url, *a, **k: (asked.append(("np", url)), (1, "ok"))[1])
    monkeypatch.setattr(local_mode, "runner_np_values", lambda *a, **k: [])
    cfg = ps.ProxyConfig(model="ollama/qwen3.8-uncensored:latest", trim="none", serve="local-agent",
                         tiers=ps.TIERS_CONVERSATION, local_with_tiers=True, ollama_url="http://127.0.0.1:11435",
                         ledger_path=tmp_path / "l.jsonl", kill_switch=tmp_path / "kill")
    assert ps._local_agent_preflight(cfg) == []
    assert asked and set(asked) == {"http://127.0.0.1:11435/api/ps", ("np", "http://127.0.0.1:11435")}


def _cmd(monkeypatch, argv, preflight=()):
    import uvicorn

    started = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: started.append(1))
    monkeypatch.setattr(ps, "_local_agent_preflight", lambda cfg: list(preflight))
    return ps.cmd_proxy(argv), started


def test_cmd_proxy_selects_the_combined_mode_with_the_flag_and_states_it(monkeypatch, capsys, tmp_path):
    rc, started = _cmd(monkeypatch, ["--serve", "local-agent", "--local-with-tiers", "--tiers", "conversation",
                                     "--model", MODEL, "--no-warm-up", "--ledger", str(tmp_path / "l.jsonl")])
    out = capsys.readouterr().out
    assert rc == 0 and started and "local_with_tiers=on" in out and "COMBINED with --tiers" in out
    assert "tiers=conversation" in out and "SERVE MODE = local-agent" in out


@pytest.mark.parametrize("argv,needle", [
    (["--local-with-tiers", "--tiers", "conversation"], "needs --serve local-agent"),
    (["--serve", "local-agent", "--local-with-tiers"], "needs --serve local-agent and --tiers"),
])
def test_cmd_proxy_refuses_the_flag_without_its_partners(monkeypatch, capsys, argv, needle):
    rc, started = _cmd(monkeypatch, argv + ["--model", MODEL, "--no-warm-up"])
    assert rc == 2 and not started and needle in capsys.readouterr().err


def test_combined_mode_with_a_failing_preflight_starts_and_says_local_is_disabled(monkeypatch, capsys, tmp_path):
    rc, started = _cmd(monkeypatch, ["--serve", "local-agent", "--local-with-tiers", "--tiers", "conversation",
                                     "--model", MODEL, "--no-warm-up", "--ledger", str(tmp_path / "l.jsonl")],
                       preflight=["model not resident", "NUM_PARALLEL unreadable"])
    err = capsys.readouterr().err
    assert rc == 0 and started
    assert "llm-router proxy: local-agent DISABLED (tier path only): model not resident" in err
    assert "llm-router proxy: local-agent DISABLED (tier path only): NUM_PARALLEL unreadable" in err


def test_the_old_tiers_off_mode_with_a_failing_preflight_still_refuses_to_start(monkeypatch, capsys, tmp_path):
    rc, started = _cmd(monkeypatch, ["--serve", "local-agent", "--model", MODEL, "--no-warm-up",
                                     "--ledger", str(tmp_path / "l.jsonl")], preflight=["model not resident"])
    assert rc == 2 and not started and "REFUSING to start" in capsys.readouterr().err


def test_a_disabled_combined_app_takes_the_tier_path_and_never_calls_the_classifier(tmp_path, monkeypatch):
    decision_model_on(monkeypatch)
    fake = FakeClassifier(monkeypatch, tier="haiku")
    rig = Rig(tmp_path, local_disabled="model not resident")
    r = rig.post(opus(turn_body(["rename the helper"])))
    (row,) = rig.rows()
    assert r.status_code == 200 and rig.backend.calls == [] and len(rig.up.requests) == 1
    # The per-turn local/Claude classifier (cls_decide) never ran: source "none", reason not_local_agent. The one
    # fake call is the tier path's own tier decision, identical to a tiers-only proxy (asserted below).
    assert row["cls_turn_source"] == "none" and row["cls_turn_reason"] == "not_local_agent"
    assert row["cls_turn_decided"] is False and row["cls_applied"] is False
    n = len(fake.calls)
    Rig(tmp_path / "plain", combined=False).post(opus(turn_body(["rename the helper"])))
    assert len(fake.calls) - n == n == 1
    assert row["local_mode"]["reason"] == "preflight_refused" and row["local_mode"]["detail"] == "model not resident"
    assert row["reason"] == "preflight_refused" and row["decision"] == "forwarded"


def test_a_raising_preflight_in_combined_mode_still_builds_and_starts(monkeypatch, capsys, tmp_path):
    import uvicorn
    started = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: started.append(1))

    def boom(cfg):
        raise ValueError("bad url")
    monkeypatch.setattr(ps, "_local_agent_preflight", boom)
    rc = ps.cmd_proxy(["--serve", "local-agent", "--local-with-tiers", "--tiers", "conversation", "--model", MODEL,
                       "--no-warm-up", "--ledger", str(tmp_path / "l.jsonl")])
    assert rc == 0 and started
    assert "local-agent DISABLED (tier path only): preflight error: ValueError: bad url" in capsys.readouterr().err


def test_combined_preflight_never_loads_the_model_inline(monkeypatch, tmp_path):
    posts = []
    monkeypatch.setattr(httpx, "post", lambda *a, **k: posts.append(a) or None)
    monkeypatch.setattr(local_mode, "resident_context", lambda *a, **k: (None, "not loaded"))
    cfg = ps.ProxyConfig(model=MODEL, trim="none", serve="local-agent", local_with_tiers=True,
                         tiers=ps.TIERS_CONVERSATION, tier_policy=_policy_file(tmp_path),
                         ollama_url="http://127.0.0.1:11435", ledger_path=tmp_path / "l.jsonl")
    out = ps._local_agent_preflight(cfg)
    assert posts == [] and out and any("resident" in r or "loaded" in r for r in out)


def test_tiers_with_local_agent_is_still_refused_without_the_flag(tmp_path):
    with pytest.raises(ValueError, match="--serve local-agent needs"):
        Rig(tmp_path, local_with_tiers=False)


# -- the old mode and the kill switch ---------------------------------------------------------------------------


def test_the_old_tiers_off_local_agent_still_serves_and_writes_no_tier_fields(tmp_path):
    rig = Rig(tmp_path, tiers=ps.TIERS_OFF, tier_policy=None, local_with_tiers=False,
              backend=Backend(_ollama("", [_call("Bash", {"command": "ls"})])))
    r = rig.post(_first_call())
    assert r.status_code == 200 and rig.up.requests == []
    (row,) = rig.rows()
    assert row["decision"] == "served" and row["tier_mode"] == "off" and row.get("tier_reason") is None


def test_the_kill_switch_file_toggles_local_serving_per_turn_without_a_restart(tmp_path, monkeypatch):
    """The documented runtime switch: touch $LLM_ROUTER_HOME/local_agent_kill (the default path, no --kill-switch
    override) and the NEXT request goes to the tier path; remove it and the same conversation is local again."""
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "home"))
    expected = paths.state_path(local_mode.KILL_SWITCH_NAME)
    assert expected == tmp_path / "home" / "local_agent_kill" and local_mode.kill_switch_path() == expected
    expected.parent.mkdir(parents=True, exist_ok=True)
    rig = Rig(tmp_path / "rig", kill=None, backend=Backend(
        _ollama("", [_call("Bash", {"command": "ls"})]), _ollama("", [_call("Bash", {"command": "pwd"})]),
        _ollama("", [_call("Bash", {"command": "id"})])))
    first = rig.post(opus(_first_call()))
    assert rig.up.requests == [] and len(rig.backend.calls) == 1
    step2 = opus(dict(_next_turn(_first_call(), {"content": _served_blocks(first)}), stream=False))
    expected.write_text("")
    rig.post(step2)
    assert len(rig.backend.calls) == 1 and len(rig.up.requests) == 1
    assert rig.rows()[1]["reason"] == "kill_switch" and rig.rows()[1]["tier_reason"]
    expected.unlink()
    rig.post(step2)
    assert len(rig.backend.calls) == 2 and rig.rows()[2]["decision"] == "served"
