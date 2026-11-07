"""NS-A1: the per-turn classifier decision is RECORDED on proxy rows, and nothing else changes.

Record only. Rules under test: the supported-call predicate and its reasons; the human-turn
boundary (typed prompt = new key, tool continuations = same key and one classifier call, the
same words typed again = new key); every failopen source is recorded and is not decided; override
and restart rows say why; with the decision model unset there is no classifier call and the rows
equal the base rows except for the additive keys; with it set the serving decisions are still
the base ones and ``cls_applied`` is false.

No Ollama, no network: the classifier is injected (``local_classifier.classify_async``), Anthropic
is a mocked upstream and the serving model a fake backend.
"""

from __future__ import annotations

import asyncio
import copy
import json

import pytest

from llm_router import local_classifier as lc
from llm_router.proxy import cls_decide, ledger
from llm_router.proxy import server as ps
from llm_router.proxy.cls_decide import TURN_FIELD_DEFAULTS
from tests.test_proxy_local_mode import (
    MODEL, Backend, Breaker, Upstream, _call, _first_call, _ollama, _post, _with_image,
)

SID = "11111111-2222-3333-4444-555555555555"
ADDITIVE = set(TURN_FIELD_DEFAULTS)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("LLM_ROUTER_LOCAL_CLASSIFIER", "LLM_ROUTER_CLASSIFIER_BACKEND", "LLM_ROUTER_DECISION_MODEL",
              "LLM_ROUTER_CLASSIFIER_MODEL", "LLM_ROUTER_OLLAMA_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LLM_ROUTER_OLLAMA_URL", "http://127.0.0.1:9")  # an escaped call fails fast
    lc._reset_state()
    yield
    lc._reset_state()


def decision_model_on(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_CLASSIFIER_BACKEND", "systemone")
    monkeypatch.setenv("LLM_ROUTER_DECISION_MODEL", "nimble:9b")
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "on")


def verdict(source="llm", tier="opus") -> lc.Verdict:
    ok = source in ("llm", "cache")
    return lc.Verdict("code" if ok else None, tier if ok else None, None, None, None, None, None,
                      "direct", source, "nimble:9b", "v6", 1.0)


class FakeClassifier:
    def __init__(self, monkeypatch, *, source="llm", tier="opus"):
        self.calls, self.source, self.tier = [], source, tier
        monkeypatch.setattr(lc, "classify_async", self)

    async def __call__(self, assembled, *, session_id, text_sha, **kw):
        self.calls.append({"session_id": session_id, "text_sha": text_sha, "prompt": assembled.prompt})
        return verdict(self.source, self.tier)


def turn_body(prompts: list[str], loops: int = 0, *, sid: str = SID) -> dict:
    """The request Claude Code sends: one human prompt per entry of ``prompts``; the newest
    prompt is followed by ``loops`` tool-call/tool-result pairs. Each older prompt got one pair."""
    body = _first_call()
    body["metadata"] = {"user_id": json.dumps({"session_id": sid})}
    msgs, n = [], 0
    for i, text in enumerate(prompts):
        msgs.append({"role": "user", "content": [{"type": "text", "text": text}]})
        for _ in range(1 if i < len(prompts) - 1 else loops):
            n += 1
            tid = f"toolu_{n:04d}"
            msgs.append({"role": "assistant", "content": [
                {"type": "tool_use", "id": tid, "name": "Read", "input": {"file_path": f"/w/{n}.py"}}]})
            msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": "x"}]})
    body["messages"] = msgs
    return body


class Env:
    def __init__(self, tmp_path):
        self.up, self.backend = Upstream(), Backend(_ollama("done"))
        self.ledger = tmp_path / "proxy_calls.jsonl"
        self.kill = tmp_path / "kill"
        self.app = self.build()

    def build(self, backend=None):
        self.backend = backend or self.backend
        import httpx

        config = ps.ProxyConfig(upstream="http://127.0.0.1:9", ledger_path=self.ledger, kill_switch=self.kill,
                                model=MODEL, trim="none", serve="local-agent", hedge_s=None, warm_up=False)
        return ps.build_app(config, client=httpx.AsyncClient(transport=httpx.MockTransport(self.up)),
                            backend_factory=lambda m: self.backend, breaker_fn=Breaker())

    def post(self, body):
        return asyncio.run(_post(self.app, body))

    def rows(self):
        return ledger.read_rows(self.ledger)


@pytest.fixture
def env(tmp_path, monkeypatch):
    async def _boom(*a, **k):
        raise AssertionError("local-agent mode must not consult the routing policy")

    monkeypatch.setattr(ps, "choose_model", _boom)
    return Env(tmp_path)


# -- the predicate -----------------------------------------------------------------


def _mod(f):
    b = _first_call()
    f(b)
    return b


@pytest.mark.parametrize("body,expected", [
    (_first_call(), (True, None)),
    (_mod(lambda b: b.pop("tools")), (False, "no_tools")),
    (_mod(lambda b: b.update(tool_choice={"type": "tool", "name": "Read"})), (False, "forced_tool_choice")),
    (_mod(lambda b: b["messages"].append({"role": "assistant", "content": "hi"})), (False, "not_user_turn")),
    (_with_image(_first_call()), (False, "media")),
    (_mod(lambda b: b.update(thread={"type": "continue", "previous_message_id": "msg_1"})),
     (False, "thread_continue")),
], ids=["supported", "no_tools", "forced_tool_choice", "not_user_turn", "media", "thread_continue"])
def test_supported_predicate_and_reason(body, expected):
    assert cls_decide.supported(body) == expected


def test_predicate_agrees_with_is_agent_turn_apart_from_media_and_thread():
    from llm_router.proxy import local_mode

    for body in (_first_call(), _mod(lambda b: b.pop("tools")),
                 _mod(lambda b: b.update(tool_choice={"type": "any"}))):
        assert cls_decide.supported(body)[0] == local_mode.is_agent_turn(body)


# -- turn key and boundary -----------------------------------------------------------


def test_turn_idx_rises_at_typed_prompts_not_in_a_tool_loop():
    one = cls_decide.turn_key(turn_body(["fix the bug"], loops=0), SID)
    loop = cls_decide.turn_key(turn_body(["fix the bug"], loops=3), SID)
    two = cls_decide.turn_key(turn_body(["fix the bug", "now the docs"], loops=0), SID)
    again = cls_decide.turn_key(turn_body(["fix the bug", "fix the bug"], loops=0), SID)
    assert one == loop and one[1] == 1
    assert two[1] == 2 and two[2] != one[2]
    assert again[1] == 2 and again[2] == one[2]       # same words, new turn: new idx, so a new key
    assert again != one


def test_one_human_turn_is_classified_once_and_its_continuations_inherit(env, monkeypatch):
    decision_model_on(monkeypatch)
    fake = FakeClassifier(monkeypatch, tier="opus")
    for loops in (0, 1, 2, 3):
        env.post(turn_body(["design a multi-file change"], loops=loops))
    rows = env.rows()
    assert len(rows) == 4 and len(fake.calls) == 1
    assert len({r["cls_decision_id"] for r in rows}) == 1 and rows[0]["cls_decision_id"]
    assert [r["cls_turn_source"] for r in rows] == ["llm", "inherit", "inherit", "inherit"]
    assert all(r["cls_turn_reason"] == "veto_tier" and r["cls_turn_decided"] is True for r in rows)
    assert {r["cls_turn_idx"] for r in rows} == {1} and len({r["cls_turn_sha"] for r in rows}) == 1
    assert all(r["supported"] is True and r["unsupported_reason"] is None for r in rows)


def test_a_new_typed_prompt_and_a_repeated_prompt_each_get_their_own_decision(env, monkeypatch):
    decision_model_on(monkeypatch)
    fake = FakeClassifier(monkeypatch, tier="haiku")
    env.post(turn_body(["rename the helper"], loops=0))
    env.post(turn_body(["rename the helper", "now rename the other one"], loops=0))
    env.post(turn_body(["rename the helper", "now rename the other one", "rename the helper"], loops=0))
    rows = env.rows()
    assert len(fake.calls) == 3
    assert [r["cls_turn_idx"] for r in rows] == [1, 2, 3]
    assert len({r["cls_decision_id"] for r in rows}) == 3
    assert rows[0]["cls_turn_sha"] == rows[2]["cls_turn_sha"] != rows[1]["cls_turn_sha"]
    assert all(r["cls_turn_reason"] == "allow" and r["cls_turn_tier"] == "haiku" for r in rows)


@pytest.mark.parametrize("source", ["timeout", "parse_error", "cold", "off", "abstain", "refused_remote"])
def test_each_failopen_source_is_recorded_not_decided_and_serves_as_before(env, monkeypatch, source):
    decision_model_on(monkeypatch)
    FakeClassifier(monkeypatch, source=source)
    env.post(turn_body(["fix the bug"]))
    (row,) = env.rows()
    assert row["cls_turn_source"] == source and row["cls_turn_reason"] == f"failopen:{source}"
    assert row["cls_turn_decided"] is False and row["cls_turn_tier"] is None
    assert row["cls_decision_id"] and row["cls_applied"] is False
    assert row["decision"] == ledger.DECISION_SERVED          # served locally, exactly as with no classifier


def test_cache_source_counts_as_decided(env, monkeypatch):
    decision_model_on(monkeypatch)
    FakeClassifier(monkeypatch, source="cache", tier="sonnet")
    env.post(turn_body(["fix the bug"]))
    (row,) = env.rows()
    assert row["cls_turn_decided"] is True and row["cls_turn_reason"] == "veto_tier"


def test_a_prompt_holding_a_secret_is_not_sent_to_the_classifier(env, monkeypatch):
    decision_model_on(monkeypatch)
    fake = FakeClassifier(monkeypatch)
    env.post(turn_body(["use this key sk-ant-api03-" + "q8Zx3LmP9vRtY2wKcN5bH7dF1gJ4sAe6UoI0xW"]))
    (row,) = env.rows()
    assert fake.calls == [] and row["cls_turn_reason"] == "withheld_secret" and row["cls_turn_decided"] is False


# -- override rows, restart, unsupported ------------------------------------------------


def test_prefix_rows_say_why_and_are_never_decided(env, monkeypatch):
    decision_model_on(monkeypatch)
    fake = FakeClassifier(monkeypatch)
    env.post(turn_body(["claude: fix the bug"]))
    env.post(turn_body(["local: fix the bug"], sid="99999999-2222-3333-4444-555555555555"))
    claude, local = env.rows()
    assert fake.calls == []
    assert (claude["cls_turn_reason"], claude["cls_turn_source"]) == ("prefix_claude", "none")
    assert (local["cls_turn_reason"], local["cls_turn_source"]) == ("prefix_local", "none")
    assert not claude["cls_turn_decided"] and not local["cls_turn_decided"]


def test_a_restart_makes_the_continuation_started_elsewhere_not_decided(env, monkeypatch):
    decision_model_on(monkeypatch)
    fake = FakeClassifier(monkeypatch)
    env.post(turn_body(["design a multi-file change"], loops=0))
    env.app = env.build()                                     # the proxy restarts: the store is memory only
    env.post(turn_body(["design a multi-file change"], loops=2))
    first, after = env.rows()
    assert len(fake.calls) == 1
    assert after["cls_turn_reason"] == "started_elsewhere" and after["cls_turn_decided"] is False
    assert after["supported"] is True and first["cls_turn_decided"] is True


def test_unsupported_rows_carry_their_reason_and_make_no_classifier_call(env, monkeypatch):
    decision_model_on(monkeypatch)
    fake = FakeClassifier(monkeypatch)
    side = turn_body(["title this"])
    side.pop("tools")
    env.post(side)
    (row,) = env.rows()
    assert fake.calls == []
    assert (row["supported"], row["unsupported_reason"], row["cls_turn_reason"]) == (False, "no_tools", "unsupported")


def test_a_media_call_still_enters_local_step_and_keeps_its_structural_pin(env, monkeypatch):
    """A1 is behaviour-neutral: ``supported`` is a label, not a gate."""
    decision_model_on(monkeypatch)
    FakeClassifier(monkeypatch)
    env.post(_with_image(_first_call()))
    (row,) = env.rows()
    assert row["supported"] is False and row["unsupported_reason"] == "media"
    assert row["local_mode"]["reason"] == "media_present" and row["egress"] is True
    assert row["decision"] == ledger.DECISION_FORWARDED


# -- feature off, and "record only" ------------------------------------------------------

_STABLE_DROP = {"ts", "route_latency_s", "upstream_latency_s", "added_latency_s", "tier_decision_s", "msg_id",
                "tier_phases_ms", "trace_id", "task_id", "backend_usage", "served_latency_s", "cls_turn_ms"}


def _comparable(row: dict, *, drop_additive: bool) -> dict:
    return {k: v for k, v in row.items() if k not in _STABLE_DROP and not (drop_additive and k in ADDITIVE)}


def _script(env):
    env.post(turn_body(["fix the bug"], loops=0))
    env.post(turn_body(["fix the bug"], loops=2))
    env.post(turn_body(["fix the bug", "now the docs"], loops=0))
    side = turn_body(["title this"])
    side.pop("tools")
    env.post(side)
    env.post(_with_image(_first_call()))
    return env.rows()


def _base_rows(tmp_path, monkeypatch):
    """The rows the proxy wrote before NS-A1: the module's two hooks are made no-ops."""
    with monkeypatch.context() as mp:
        async def noop(*a, **k):
            return None

        mp.setattr(cls_decide, "static_fields", lambda body, sid: {})
        mp.setattr(cls_decide, "decide_turn", noop)

        async def _boom(*a, **k):
            raise AssertionError("no policy")

        mp.setattr(ps, "choose_model", _boom)
        return _script(Env(tmp_path / "base"))


def test_decision_model_unset_makes_no_classifier_call_and_rows_equal_base_plus_additive_keys(tmp_path, monkeypatch):
    (tmp_path / "base").mkdir()
    (tmp_path / "new").mkdir()
    base = _base_rows(tmp_path, monkeypatch)

    async def _boom(*a, **k):
        raise AssertionError("no policy")

    monkeypatch.setattr(ps, "choose_model", _boom)

    async def forbidden(*a, **k):
        raise AssertionError("the classifier must not be called with the decision model unset")

    monkeypatch.setattr(lc, "classify_async", forbidden)
    new = _script(Env(tmp_path / "new"))
    assert len(base) == len(new) == 5
    for b, n in zip(base, new):
        assert not (set(b) & ADDITIVE)                                       # the base never had the keys
        assert ADDITIVE <= set(n)                                            # every new row has all of them
        assert _comparable(n, drop_additive=True) == _comparable(b, drop_additive=False)
        assert n["decision"] == b["decision"] and n.get("reason") == b.get("reason")
        assert n["cls_applied"] is False and n["cls_turn_decided"] is False
    assert {r["cls_turn_reason"] for r in new} <= {"classifier_off", "unsupported", "started_elsewhere"}


def test_decision_model_set_is_record_only_serving_is_the_base_serving(tmp_path, monkeypatch):
    (tmp_path / "base").mkdir()
    (tmp_path / "new").mkdir()
    base = _base_rows(tmp_path, monkeypatch)

    async def _boom(*a, **k):
        raise AssertionError("no policy")

    monkeypatch.setattr(ps, "choose_model", _boom)
    decision_model_on(monkeypatch)
    fake = FakeClassifier(monkeypatch, tier="opus")                         # a veto verdict: A1 must not act on it
    new = _script(Env(tmp_path / "new"))
    assert fake.calls                                                       # the classifier really ran
    for b, n in zip(base, new):
        assert _comparable(n, drop_additive=True) == _comparable(b, drop_additive=False)
        assert n["decision"] == b["decision"] and n["cls_applied"] is False
    assert any(r["cls_turn_reason"] == "veto_tier" for r in new)            # recorded, not applied
    assert any(r["decision"] == ledger.DECISION_SERVED for r in new)        # a vetoed turn is still served locally


def test_shadow_mode_makes_no_inline_call(env, monkeypatch):
    decision_model_on(monkeypatch)
    monkeypatch.setenv("LLM_ROUTER_LOCAL_CLASSIFIER", "shadow")
    fake = FakeClassifier(monkeypatch)
    env.post(turn_body(["fix the bug"]))
    (row,) = env.rows()
    assert fake.calls == [] and row["cls_turn_reason"] == "classifier_off"


def test_without_local_agent_mode_rows_get_the_label_and_no_decision(tmp_path, monkeypatch):
    import httpx

    decision_model_on(monkeypatch)
    fake = FakeClassifier(monkeypatch)
    up, path = Upstream(), tmp_path / "proxy_calls.jsonl"
    config = ps.ProxyConfig(upstream="http://127.0.0.1:9", ledger_path=path, kill_switch=tmp_path / "k")
    app = ps.build_app(config, client=httpx.AsyncClient(transport=httpx.MockTransport(up)))
    asyncio.run(_post(app, turn_body(["fix the bug"])))
    (row,) = ledger.read_rows(path)
    assert fake.calls == [] and row["supported"] is True and row["cls_turn_reason"] == "not_local_agent"


def test_store_is_bounded():
    store = cls_decide.TurnDecisionStore(cap=3)
    for i in range(10):
        store.put(("c", i, "s"), {"decision_id": str(i)})
    assert len(store) == 3 and store.get(("c", 9, "s")) and store.get(("c", 0, "s")) is None


def test_decide_turn_never_raises(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(cls_decide, "_decide", boom)
    row = {"supported": True}
    asyncio.run(cls_decide.decide_turn(copy.deepcopy(_first_call()), row, cls_decide.TurnDecisionStore(),
                                       lm_on=True))
    assert row["cls_turn_reason"] == "error"
