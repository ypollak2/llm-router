"""``--serve local-agent`` (llm_router.proxy.local_mode): P1-P3, pinning, the
eligibility stub, the preflight, and "never silent".

Anthropic is a mocked upstream and the serving model a fake backend; nothing
here reaches the network or a real Ollama (the preflight takes injected
``get`` / ``runner`` functions). Off mode is pinned by test_proxy_off_golden.py.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import os
import types
from pathlib import Path

import httpx
import pytest

from llm_router import failopen
from llm_router.local_agent import capability as cap
from llm_router.proxy import history_mask, ledger, local_mode
from llm_router.proxy import server as ps
from llm_router.proxy.translate import from_ollama

FIXTURE = Path(__file__).parent / "fixtures" / "proxy" / "continuation_request.json"
MODEL = "ollama/fake:1"


def _req() -> dict:
    return json.loads(FIXTURE.read_text())


def _main_thread(body: dict) -> dict:
    """A real main session holds the Agent launcher; without it steps.is_main_thread reads False and rv40's
    exemption would refuse the fixture turn as a sub-agent's."""
    body["tools"] = list(body["tools"]) + [{"name": "Agent", "description": "launch",
                                            "input_schema": {"type": "object", "properties": {}}}]
    return body


def _first_call() -> dict:
    body = _req()
    body["messages"] = body["messages"][:2]
    return _main_thread(body)


def _ollama(content="", calls=None):
    return {"message": {"role": "assistant", "content": content, "tool_calls": calls or []},
            "done_reason": "stop", "prompt_eval_count": 100, "eval_count": 5}


def _call(name, args):
    return {"function": {"name": name, "arguments": args}}


class Backend:
    def __init__(self, *replies, exc=None):
        self.replies, self.exc, self.calls = list(replies), exc, []

    async def complete(self, body, timeout_s):
        self.calls.append(body)
        if self.exc:
            raise self.exc
        reply = self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]
        m, err = from_ollama(reply, body)
        return m, err, {"prompt_tokens": 10, "output_tokens": 2}


class Upstream:
    def __init__(self):
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return httpx.Response(200, stream=httpx.ByteStream(b'{"type":"message"}'),
                              headers={"content-type": "application/json"})


class Breaker:
    def __init__(self, allowed=True):
        self.allowed = allowed

    def __call__(self, lever, task_type):
        return type("D", (), {"allowed": self.allowed, "reason": "test breaker"})()


@pytest.fixture(autouse=True)
def _overrides_readable(monkeypatch):
    """NS-L fail-closed: an unreadable session_kind_overrides.json refuses every local step, so a test that
    serves locally starts with a readable (empty) file."""
    from llm_router import session_kind

    monkeypatch.setattr(session_kind, "_OVERRIDES_CACHE", {})
    monkeypatch.setattr(session_kind, "_OVERRIDES_PROBLEM", {})
    path = session_kind.overrides_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}")
    yield
    path.unlink(missing_ok=True)


@pytest.fixture
def env(tmp_path, monkeypatch):
    async def _boom(*a, **k):
        raise AssertionError("local-agent mode must not consult the routing policy (P2)")

    monkeypatch.setattr(ps, "choose_model", _boom)

    class Env:
        up = Upstream()
        kill = tmp_path / "kill"
        ledger = tmp_path / "proxy_calls.jsonl"

        def app(self, backend, *, breaker=None, **cfg):
            cfg = dict(dict(model=MODEL, trim="none", serve="local-agent", hedge_s=None, warm_up=False), **cfg)
            config = ps.ProxyConfig(upstream="http://127.0.0.1:9", ledger_path=self.ledger, kill_switch=self.kill, **cfg)
            client = httpx.AsyncClient(transport=httpx.MockTransport(self.up))
            return ps.build_app(config, client=client, backend_factory=lambda m: backend,
                                breaker_fn=breaker or Breaker())

        def rows(self):
            return ledger.read_rows(self.ledger)

    return Env()


async def _post(app, body):
    headers = {"authorization": "Bearer sk-ant-oat01-" + "Zq9" * 20, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8787") as c:
        return await c.post("/v1/messages?beta=true", content=json.dumps(body), headers=headers)


def _served_blocks(resp) -> list[dict]:
    blocks = []
    for line in resp.text.splitlines():
        if line.startswith("data: "):
            d = json.loads(line[6:])
            if d.get("type") == "content_block_start":
                blocks.append(d["content_block"])
    return blocks


def _next_turn(body, message, result_text="ok", is_error=False):
    """The request Claude Code would send after running ``message``'s tool calls."""
    body = copy.deepcopy(body)
    body["messages"] = [m for m in body["messages"] if m["role"] != "system"]
    body["messages"].append({"role": "assistant", "content": message["content"]})
    body["messages"].append({"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": b["id"], "content": result_text, **({"is_error": True} if is_error else {})}
        for b in message["content"] if b["type"] == "tool_use"]})
    return body


def _with_image(body):
    body = copy.deepcopy(body)
    body["messages"][0]["content"] = [{"type": "text", "text": "what is in this image?"},
                                      {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                                   "data": "AA"}}]
    return body


# ── config surface ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw,mode", [(None, "off"), ("", "off"), ("off", "off"), ("0", "off"),
                                      ("local-agent", "local-agent"), ("LOCAL_AGENT", "local-agent")])
def test_parse_serve_mode(raw, mode):
    assert local_mode.parse_serve_mode(raw) == mode


def test_parse_serve_mode_rejects_unknown():
    with pytest.raises(ValueError):
        local_mode.parse_serve_mode("on")


def test_serve_mode_comes_from_the_env_var(monkeypatch):
    monkeypatch.setenv(local_mode.ENV_MODE, "local-agent")
    assert ps.ProxyConfig.from_env().serve == "local-agent"
    monkeypatch.delenv(local_mode.ENV_MODE)
    assert ps.ProxyConfig.from_env().serve == "off"


@pytest.mark.parametrize("cfg", [
    dict(trim=None), dict(trim="fast"), dict(model=None), dict(model="anthropic/x"), dict(tiers="conversation"),
])
def test_build_app_refuses_a_config_the_mode_cannot_honour(env, cfg):
    with pytest.raises(ValueError, match="--serve local-agent needs"):
        env.app(Backend(_ollama("x")), **cfg)


# ── P1 + P2: the first call is served, the policy is not asked ───────────────


async def test_first_call_of_a_conversation_is_served_locally(env):
    backend = Backend(_ollama("", [_call("Bash", {"command": "ls"})]))
    r = await _post(env.app(backend), _first_call())
    assert r.status_code == 200 and env.up.requests == []
    assert [b["name"] for b in _served_blocks(r)] == ["Bash"]
    (row,) = env.rows()
    assert row["decision"] == "served" and row["serve_mode"] == "local-agent"
    assert row["local_mode"]["pin"] == "local" and row["model"] == MODEL
    assert row["task_type"] == "local_agent"  # the policy was bypassed (P2)


async def test_continuation_is_served_and_the_trim_is_none(env):
    backend = Backend(_ollama("", [_call("Read", {"file_path": "/work/repo/a.py"})]), _ollama("Done."))
    app = env.app(backend)
    first = await _post(app, _first_call())
    r = await _post(app, dict(_next_turn(_first_call(), {"content": _served_blocks(first)}), stream=False))
    assert r.json()["content"] == [{"type": "text", "text": "Done."}]
    sent = backend.calls[1]
    assert sent["system"] == _req()["system"] and [t["name"] for t in sent["tools"]] == ["Bash", "Edit", "Read", "Write", "Agent"]
    assert len([m for m in sent["messages"] if m["role"] != "system"]) == 3  # no history cap


async def test_side_calls_without_tools_are_forwarded_with_a_reason(env):
    r = await _post(env.app(Backend(_ollama("x"))), dict(_first_call(), tools=[]))
    assert r.status_code == 200 and len(env.up.requests) == 1
    (row,) = env.rows()
    assert row["decision"] == "forwarded" and row["reason"] == "not_eligible"


# ── pinning ──────────────────────────────────────────────────────────────────


async def test_a_conversation_the_proxy_did_not_see_start_is_never_taken_local(env):
    backend = Backend(_ollama("x"))
    app = env.app(backend)
    r = await _post(app, _main_thread(_req()))  # 3+ turns, never seen the first call
    assert r.status_code == 200 and backend.calls == [] and len(env.up.requests) == 1
    (row,) = env.rows()
    assert row["reason"] == "conversation_started_on_claude" and row["egress"] is True
    await _post(app, _main_thread(_req()))  # still Claude's
    assert backend.calls == [] and env.rows()[1]["reason"] == "pinned_claude"


async def test_a_local_conversation_stays_local_across_steps(env):
    backend = Backend(_ollama("", [_call("Bash", {"command": "ls"})]), _ollama("Done."))
    app = env.app(backend)
    first = await _post(app, _first_call())
    msg = {"content": _served_blocks(first)}
    await _post(app, _next_turn(_first_call(), msg))
    assert len(backend.calls) == 2 and env.up.requests == []
    assert [r["local_mode"]["pin"] for r in env.rows()] == ["local", "local"]


async def test_a_failed_local_step_moves_the_conversation_to_claude_once(env):
    backend = Backend(_ollama("", [_call("Nope", {})]))
    app = env.app(backend)
    r = await _post(app, _first_call())
    assert r.status_code == 200 and len(env.up.requests) == 1
    row = env.rows()[0]
    assert row["decision"] == "fallback" and row["reason"] == "validation" and row["egress"] is True
    assert row["local_mode"]["pin_change"] == "local->claude"
    await _post(app, _first_call())
    assert len(backend.calls) == 1  # not retried locally: pinned to Claude
    assert env.rows()[1]["reason"] == "pinned_claude"


# ── eligibility: media, size, health, breaker, kill switch ───────────────────


async def test_media_in_the_first_turn_goes_to_claude_and_no_placeholder_is_ever_built(env):
    backend = Backend(_ollama("x"))
    r = await _post(env.app(backend), _with_image(_first_call()))
    assert r.status_code == 200 and backend.calls == [] and len(env.up.requests) == 1
    assert "image omitted" not in env.up.requests[0].content.decode()
    row = env.rows()[0]
    assert row["reason"] == "media_present" and row["local_mode"]["pin_change"] == "new->claude"


async def test_media_arriving_in_a_local_conversation_escalates_explicitly_and_for_good(env):
    backend = Backend(_ollama("", [_call("Bash", {"command": "ls"})]))
    app = env.app(backend)
    first = await _post(app, _first_call())
    img = _next_turn(_first_call(), {"content": _served_blocks(first)})
    img["messages"][-1]["content"].append({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA"}})
    await _post(app, img)
    assert len(backend.calls) == 1 and len(env.up.requests) == 1
    assert env.rows()[1]["reason"] == "media_present"
    await _post(app, _next_turn(_first_call(), {"content": _served_blocks(first)}))  # no media now: still Claude's
    assert len(backend.calls) == 1 and env.rows()[2]["reason"] == "pinned_claude"


async def test_a_prompt_over_the_cap_is_escalated_not_truncated(env):
    big = _first_call()
    big["messages"][0]["content"] = "x " * 60_000
    backend = Backend(_ollama("x"))
    await _post(env.app(backend), big)
    assert backend.calls == []
    row = env.rows()[0]
    assert row["reason"] == "prompt_over_cap" and row["egress"] is True
    assert row["local_mode"]["est_prompt_tokens"] > local_mode.PROMPT_CAP_TOKENS


async def test_kill_switch_takes_effect_without_a_restart_and_is_not_sticky(env):
    backend = Backend(_ollama("", [_call("Bash", {"command": "ls"})]), _ollama("", [_call("Bash", {"command": "pwd"})]),
                      _ollama("", [_call("Bash", {"command": "id"})]))
    app = env.app(backend)
    first = await _post(app, _first_call())
    step2 = _next_turn(_first_call(), {"content": _served_blocks(first)})
    env.kill.write_text("")
    await _post(app, step2)
    assert len(backend.calls) == 1 and env.rows()[1]["reason"] == "kill_switch"
    env.kill.unlink()
    await _post(app, step2)
    assert len(backend.calls) == 2 and env.rows()[2]["decision"] == "served"  # same conversation, local again


async def test_early_exit_rows_carry_a_size_bucket_and_an_unmeasurable_payload_is_unknown(env, monkeypatch):
    backend = Backend(_ollama("x"))
    app = env.app(backend)
    env.kill.write_text("")
    await _post(app, _first_call())
    row = env.rows()[0]
    assert row["reason"] == "kill_switch" and row["size_bucket"] == "<=8k" and row["est_prompt_tokens"] > 0

    def deep(_payload):
        raise RecursionError("too deep")

    monkeypatch.setattr(local_mode, "estimate_prompt_tokens", deep)
    await _post(app, _first_call())
    row = env.rows()[1]
    assert row["reason"] == "kill_switch" and row["size_bucket"] == "unknown" and row["est_prompt_tokens"] is None
    assert backend.calls == []


async def test_open_quality_breaker_forwards_with_its_reason(env):
    backend = Backend(_ollama("x"))
    await _post(env.app(backend, breaker=Breaker(allowed=False)), _first_call())
    row = env.rows()[0]
    assert backend.calls == [] and row["reason"] == "breaker_open" and row["detail"] == "test breaker"


async def test_a_crashed_backend_falls_back_with_its_reason_and_stays_on_claude(env):
    backend = Backend(_ollama("x"), exc=RuntimeError("runner process has terminated"))
    app = env.app(backend)
    await _post(app, _first_call())
    await _post(app, _first_call())
    assert len(backend.calls) == 1
    assert [r["reason"] for r in env.rows()] == ["backend_error", "pinned_claude"]


async def test_every_non_served_step_has_a_reason_and_an_egress_flag(env, capsys):
    app = env.app(Backend(_ollama("x"), exc=RuntimeError("boom")))
    for body in (_req(), _with_image(_first_call()), dict(_first_call(), tools=[]), _first_call()):
        await _post(app, body)
    rows = env.rows()
    assert len(rows) == 4
    for row in rows:
        assert row["decision"] != "served" and row["reason"], row
    assert [r.get("egress") for r in rows] == [True, True, None, True]
    err = capsys.readouterr().err
    assert err.count("SENT TO ANTHROPIC") == 3 and "media_present" in err


# ── P3: edits ────────────────────────────────────────────────────────────────


def _edit_args(path, old="x = 1", new="x = 2"):
    return {"file_path": str(path), "old_string": old, "new_string": new}


async def test_a_raw_edit_is_served_and_checked_after_it_is_applied(env, tmp_path):
    target = tmp_path / "a.py"
    target.write_text("x = 1\n")
    backend = Backend(_ollama("", [_call("Edit", _edit_args(target))]), _ollama("Done."))
    app = env.app(backend)
    first = await _post(app, _first_call())
    assert [b["name"] for b in _served_blocks(first)] == ["Edit"] and env.up.requests == []
    row = env.rows()[0]
    assert row["served_via"] == "raw_edit_checked" and row["decision"] == "served"
    # The client reports success, but the file does not hold the new text.
    msg = {"content": _served_blocks(first)}
    await _post(app, _next_turn(_first_call(), msg, "The file has been updated."))
    sent = json.dumps(backend.calls[1]["messages"])
    assert "post-apply check FAILED" in sent and "does not contain the new text" in sent
    assert env.rows()[1]["post_apply_checks"][0]["ok"] is False


async def test_a_correct_edit_passes_its_post_apply_check_silently(env, tmp_path):
    target = tmp_path / "a.py"
    target.write_text("x = 2\n")
    backend = Backend(_ollama("", [_call("Edit", _edit_args(target))]), _ollama("Done."))
    app = env.app(backend)
    first = await _post(app, _first_call())
    await _post(app, _next_turn(_first_call(), {"content": _served_blocks(first)}, "updated"))
    assert "post-apply check FAILED" not in json.dumps(backend.calls[1]["messages"])
    assert env.rows()[1]["post_apply_checks"] == [{"tool": "Edit", "ok": True}]


async def test_an_edit_that_breaks_syntax_is_flagged(env, tmp_path):
    target = tmp_path / "a.py"
    target.write_text("def f(:\n")
    backend = Backend(_ollama("", [_call("Edit", _edit_args(target, "x", "def f(:"))]), _ollama("Done."))
    app = env.app(backend)
    first = await _post(app, _first_call())
    await _post(app, _next_turn(_first_call(), {"content": _served_blocks(first)}))
    assert "no longer valid py" in json.dumps(backend.calls[1]["messages"])


async def test_write_is_served_and_compared_with_the_file_on_disk(env, tmp_path):
    target = tmp_path / "n.txt"
    backend = Backend(_ollama("", [_call("Write", {"file_path": str(target), "content": "hello\n"})]),
                      _ollama("Done."), _ollama("Done."))
    app = env.app(backend)
    first = await _post(app, _first_call())
    assert [b["name"] for b in _served_blocks(first)] == ["Write"]
    target.write_text("something else\n")
    await _post(app, _next_turn(_first_call(), {"content": _served_blocks(first)}))
    assert "does not hold the content that was written" in json.dumps(backend.calls[1]["messages"])


async def test_a_write_to_a_relative_path_is_flagged_because_the_model_dropped_the_slash(env, tmp_path):
    rel = str(tmp_path / "n.txt").lstrip("/")  # "private/var/..." the way the probe saw it
    backend = Backend(_ollama("", [_call("Write", {"file_path": rel, "content": "hi\n"})]), _ollama("Done."))
    app = env.app(backend)
    first = await _post(app, _first_call())
    await _post(app, _next_turn(_first_call(), {"content": _served_blocks(first)}))
    assert "is not absolute" in json.dumps(backend.calls[1]["messages"])
    assert env.rows()[1]["post_apply_checks"][0]["ok"] is False


async def test_a_client_error_on_the_edit_is_not_a_check_failure(env, tmp_path):
    target = tmp_path / "a.py"
    target.write_text("x = 1\n")
    backend = Backend(_ollama("", [_call("Edit", _edit_args(target))]), _ollama("Done."))
    app = env.app(backend)
    first = await _post(app, _first_call())
    await _post(app, _next_turn(_first_call(), {"content": _served_blocks(first)}, "old_string not found", is_error=True))
    assert env.rows()[1]["post_apply_checks"] == [{"tool": "Edit", "ok": True}]


async def test_a_qualifying_edit_goes_through_the_validated_protocol(env, monkeypatch, tmp_path):
    target = str(tmp_path / "a.py")
    monkeypatch.setattr(ps.la_capability, "check_reply",
                        lambda message, body, **kw: cap.Decision(cap.ROUTE_EDIT, "edit_shaped", "Edit", targets=(target,)))
    seen = {}

    async def protocol(decision, message, body, generate, deadline):
        seen["called"] = True
        return {"id": "msg_lrx", "type": "message", "role": "assistant", "model": None, "stop_reason": "tool_use",
                "stop_sequence": None, "usage": {"output_tokens": 1}, "content": [
                    {"type": "tool_use", "id": "toolu_lrprotocol", "name": "Edit", "input": _edit_args(target)}]}, None, {"attempts": 1}

    monkeypatch.setattr(ps.la_capability, "run_edit_protocol", protocol)
    backend = Backend(_ollama("", [_call("Edit", _edit_args(target, "raw", "raw2"))]))
    r = await _post(env.app(backend), _first_call())
    assert seen["called"] and _served_blocks(r)[0]["id"] == "toolu_lrprotocol"
    assert env.rows()[0]["served_via"] == "edit_protocol"


async def test_a_failed_protocol_falls_back_to_the_raw_edit_with_a_check(env, monkeypatch, tmp_path):
    target = str(tmp_path / "a.py")
    monkeypatch.setattr(ps.la_capability, "check_reply",
                        lambda message, body, **kw: cap.Decision(cap.ROUTE_EDIT, "edit_shaped", "Edit", targets=(target,)))

    async def protocol(decision, message, body, generate, deadline):
        return None, "edit protocol: attempt 1: validation", {"attempts": 3, "rejections": ["x"]}

    monkeypatch.setattr(ps.la_capability, "run_edit_protocol", protocol)
    backend = Backend(_ollama("", [_call("Edit", _edit_args(target))]))
    r = await _post(env.app(backend), _first_call())
    assert [b["name"] for b in _served_blocks(r)] == ["Edit"] and env.up.requests == []
    row = env.rows()[0]
    assert row["served_via"] == "raw_edit_checked" and row["edit_protocol"]["fallback"] == "raw_edit_checked"


def test_the_hard_rule_is_unchanged_unless_the_mode_asks():
    msg = {"content": [{"type": "tool_use", "id": "t", "name": "Write", "input": {"file_path": "/x", "content": "y"}}]}
    assert cap.check_reply(msg, _req()).route == cap.ROUTE_CLAUDE
    assert cap.check_reply(msg, _req(), raw_edit_checked=True).reason == cap.REASON_RAW_EDIT_CHECKED


# ── decide_local (the rule stub) ─────────────────────────────────────────────


def _payload(n_chars=100):
    return {"model": "m", "messages": [{"role": "user", "content": "a" * n_chars}]}


def test_decide_local_rules_in_order():
    s = local_mode.LocalSession
    ok = local_mode.decide_local(_first_call(), s(payload=_payload()))
    assert ok.local and ok.est_tokens
    assert local_mode.decide_local(_first_call(), s(kill_switch=True, pinned="claude")).reason == "kill_switch"
    assert local_mode.decide_local(_first_call(), s(pinned="claude")).reason == "pinned_claude"
    assert local_mode.decide_local(_with_image(_first_call()), s()).reason == "media_present"
    assert local_mode.decide_local(_first_call(), s(payload=_payload(400_000))).reason == "prompt_over_cap"
    assert local_mode.decide_local(_first_call(), s(backend_healthy=False)).reason == "backend_unhealthy"
    assert local_mode.decide_local(_first_call(), s(breaker_closed=False)).reason == "breaker_open"


# (name, chars, digits, REAL prompt_eval_count) measured on qwen3.6:35b-a3b-coding,
# Ollama 0.32.13, 2026-10-05: three real Claude Code first-call bodies, then a
# continuation carrying logs / prose / code of different sizes.
CALIBRATION = [
    ("first, 15 tools", 51541, 490, 12229), ("first, 27 tools (no tool search + MCP)", 78492, 678, 18494),
    ("cont + 150-line log", 54127, 3645, 16157), ("cont + 300-line log", 65203, 6865, 22077),
    ("cont + prose", 63645, 435, 15246), ("cont + code", 52281, 1507, 15159),
    ("cont + 450-line log", 76281, 10085, 27997),
]


@pytest.mark.parametrize("name,chars,digits,real", CALIBRATION)
def test_the_cap_estimate_is_never_under_the_real_count_and_not_wildly_over(name, chars, digits, real):
    text = "1" * digits + "a" * (chars - digits)
    est = local_mode.estimate_prompt_tokens({"m": text})
    assert 1.0 <= est / real <= 1.3, (name, est, real)


def test_the_real_claude_code_mcp_body_is_under_the_cap_the_guards_own_estimate_refused_it():
    chars, digits = 78492, 678  # real count 18,494
    payload = {"m": "1" * digits + "a" * (chars - digits)}
    assert local_mode.over_prompt_cap(payload) is None
    from llm_router.local_context_guard import estimate_payload_tokens

    assert estimate_payload_tokens(payload) > local_mode.PROMPT_CAP_TOKENS - 2000  # 26k by chars/3.04: the reason


def test_the_cap_counts_digits_the_chars_per_token_guess_undercounts():
    digits = _payload(0)
    digits["messages"][0]["content"] = "1234567890" * 3_000  # 30,000 chars, 30,000 tokens in qwen
    assert local_mode.over_prompt_cap(digits) is not None
    prose = _payload(0)
    prose["messages"][0]["content"] = "word " * 4_000  # 20,000 chars
    assert local_mode.over_prompt_cap(prose) is None


def test_media_is_found_in_any_turn_and_in_tool_results():
    assert not local_mode.has_media(_req())
    body = _req()
    body["messages"][1]["content"] = [{"type": "tool_result", "tool_use_id": "x", "content": [
        {"type": "document", "source": {}}]}]
    assert local_mode.has_media(body)


# ── preflight and startup ────────────────────────────────────────────────────

GOOD_RUNNER = {
    ("lsof",): "123\n",
    ("ps", "eww"): "ollama serve OLLAMA_HOST=127.0.0.1:11500 OLLAMA_NUM_PARALLEL=1 OLLAMA_CONTEXT_LENGTH=32768",
    ("ps", "-axo"): "/x/llama-server --model /m -c 32768 -np 1 --flash-attn\n/bin/zsh",
}


def _runner(table):
    def run(cmd):
        for prefix, out in table.items():
            if tuple(cmd[:len(prefix)]) == prefix:
                return out
        return ""
    return run


def _ps(ctx=32768, name="fake:1"):
    return lambda url: {"models": [{"name": name, "context_length": ctx}]}


def _pre(tmp_path, **over):
    kw = dict(model=MODEL, trim="none", ollama_url="http://127.0.0.1:11500", num_ctx=32768,
              ledger_path=tmp_path / "led" / "x.jsonl", kill_path=tmp_path / "led" / "kill", tiers_off=True,
              compaction_off=True, get=_ps(), runner=_runner(GOOD_RUNNER))
    kw.update(over)
    return local_mode.preflight(**kw)


def test_preflight_passes_on_a_good_setup(tmp_path):
    assert _pre(tmp_path) == []


@pytest.mark.parametrize("over,needle", [
    (dict(get=_ps(ctx=8192)), "num_ctx 8192"),
    (dict(get=_ps(name="other:1")), "not resident"),
    (dict(get=lambda u: (_ for _ in ()).throw(OSError("down"))), "cannot reach"),
    (dict(trim="fast"), "trim must be 'none'"),
    (dict(trim=None), "trim must be 'none'"),
    (dict(tiers_off=False), "--tiers must be off"),
    (dict(compaction_off=False), "compaction must be off"),
    (dict(model=None), "--model ollama/<tag> is required"),
    (dict(num_ctx=16384), "< 32768"),
    (dict(ollama_url="http://10.0.0.5:11434"), "not loopback"),
    (dict(runner=_runner({**GOOD_RUNNER, ("ps", "eww"): "ollama serve OLLAMA_NUM_PARALLEL=4"})), "OLLAMA_NUM_PARALLEL=4"),
    (dict(runner=_runner({**GOOD_RUNNER, ("ps", "eww"): "ollama serve"})), "not set"),
    (dict(runner=_runner({**GOOD_RUNNER, ("lsof",): ""})), "no process found"),
    (dict(runner=_runner({**GOOD_RUNNER, ("ps", "-axo"): "/x/llama-server -c 131072 -np 4"})), "-np 4"),
])
def test_preflight_refuses_each_unsafe_setup_with_its_reason(tmp_path, over, needle):
    problems = _pre(tmp_path, **over)
    assert any(needle in p for p in problems), problems


def test_preflight_refuses_when_the_ledger_or_kill_switch_is_unwritable(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    problems = _pre(tmp_path, ledger_path=blocker / "x.jsonl", kill_path=blocker / "kill")
    assert any("ledger not writable" in p for p in problems) and any("kill switch not writable" in p for p in problems)


def test_preflight_overflow_guard_is_a_real_self_test(tmp_path, monkeypatch):
    assert local_mode.overflow_guard_active(32768) is None
    monkeypatch.setattr(local_mode, "estimate_payload_tokens", lambda payload: 1)
    assert "did not exceed the window" in local_mode.overflow_guard_active(32768)
    assert any("overflow guard not active" in p for p in _pre(tmp_path))


def test_cmd_proxy_refuses_to_start_with_a_clear_reason_and_exits_nonzero(monkeypatch, capsys):
    import uvicorn

    started = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: started.append(1))
    monkeypatch.setattr(ps, "_local_agent_preflight", lambda cfg: ["OLLAMA_NUM_PARALLEL=4 on the server"])
    rc = ps.cmd_proxy(["--serve", "local-agent", "--model", MODEL, "--no-warm-up"])
    err = capsys.readouterr().err
    assert rc == 2 and not started and "REFUSING to start" in err and "OLLAMA_NUM_PARALLEL=4" in err


def test_cmd_proxy_banner_states_the_mode_the_egress_rule_and_the_kill_switch(monkeypatch, capsys, tmp_path):
    import uvicorn

    started = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: started.append(1))
    monkeypatch.setattr(ps, "_local_agent_preflight", lambda cfg: [])
    rc = ps.cmd_proxy(["--serve", "local-agent", "--model", MODEL, "--no-warm-up", "--ledger", str(tmp_path / "l.jsonl")])
    out = capsys.readouterr().out
    assert rc == 0 and started
    assert "SERVE MODE = local-agent" in out and "egress:" in out and "kill switch: touch" in out
    assert "trim=none" in out and "hedge=None" in out


def test_cmd_proxy_rejects_a_bad_serve_value(capsys):
    assert ps.cmd_proxy(["--serve", "banana"]) == 2
    assert "expected one of" in capsys.readouterr().err


def test_env_var_is_the_default_for_the_flag(monkeypatch):
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    monkeypatch.setenv(local_mode.ENV_MODE, "local-agent")
    monkeypatch.setattr(ps, "_local_agent_preflight", lambda cfg: ["nope"])
    assert ps.cmd_proxy(["--model", MODEL, "--no-warm-up"]) == 2
    assert os.environ[local_mode.ENV_MODE] == "local-agent"


# ── output cap ───────────────────────────────────────────────────────────────


async def test_local_mode_sends_the_larger_output_cap_to_ollama_and_off_mode_keeps_its_own_cap(tmp_path):
    seen = []

    def handler(request):
        if request.url.port == 11999:
            seen.append(json.loads(request.content)["options"]["num_predict"])
            lines = [{"message": {"role": "assistant", "content": "Done."}, "done": False},
                     {"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop",
                      "prompt_eval_count": 10, "eval_count": 2}]
            return httpx.Response(200, stream=httpx.ByteStream("".join(json.dumps(x) + "\n" for x in lines).encode()))
        return httpx.Response(200, stream=httpx.ByteStream(b"{}"), headers={"content-type": "application/json"})

    for serve, expected in (("local-agent", local_mode.LOCAL_NUM_PREDICT), ("off", 700)):
        cfg = ps.ProxyConfig(upstream="http://127.0.0.1:9", ledger_path=tmp_path / f"{serve}.jsonl", model=MODEL,
                             trim="none", serve=serve, hedge_s=None, warm_up=False,
                             kill_switch=tmp_path / "kill", ollama_url="http://127.0.0.1:11999")

        async def policy(text, pinned, **_kw):
            return {"task_type": "code", "complexity": "moderate", "chain_head": [MODEL], "model": MODEL}

        orig, ps.choose_model = ps.choose_model, policy
        try:
            app = ps.build_app(cfg, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                               breaker_fn=Breaker())
            await _post(app, _first_call() if serve == "local-agent" else _req())
        finally:
            ps.choose_model = orig
        assert seen[-1] == expected, (serve, seen)


def test_cmd_proxy_without_a_model_refuses_cleanly_with_the_table_default(monkeypatch, capsys):
    """M3.4: no --model and no --num-ctx leaves ProxyConfig.num_ctx None. The
    preflight runs before build_app and must not compare None to an int (it
    raised TypeError on 690a850); it refuses for the missing model instead."""
    import uvicorn

    monkeypatch.delenv("LLM_ROUTER_PROXY_NUM_CTX", raising=False)
    monkeypatch.delenv("LLM_ROUTER_PROXY_MODEL", raising=False)
    started = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: started.append(1))
    monkeypatch.setattr(local_mode, "server_num_parallel", lambda *a, **k: (1, "ok"))
    monkeypatch.setattr(local_mode, "runner_np_values", lambda *a, **k: [])
    rc = ps.cmd_proxy(["--serve", "local-agent", "--trim", "none", "--tiers", "off"])
    err = capsys.readouterr().err
    assert rc == 2 and not started
    assert "REFUSING to start" in err and "--model ollama/<tag> is required" in err
    assert "TypeError" not in err and "--num-ctx" not in err


def test_preflight_and_banner_resolve_a_missing_window_from_the_table(tmp_path):
    assert local_mode.resolve_num_ctx(None, None) == 32768
    assert local_mode.resolve_num_ctx("ollama/qwen3.5:latest", None) == 32768
    assert local_mode.resolve_num_ctx("ollama/qwen3.5:latest", 40000) == 40000
    pre = _pre(tmp_path, num_ctx=None)
    assert not any("--num-ctx" in p for p in pre)
    lines = local_mode.banner_lines(model=MODEL, num_ctx=None, kill_path=tmp_path / "k", killed=False)
    assert lines and "local-agent" in lines[0]


# ── D-445: a sub-agent is not pinned by the main thread sharing its long preamble ──

_LONG_PREAMBLE = "<system-reminder>shared claude.md and memory index line. " * 400  # > 8000 chars


def _sub_first_call() -> dict:
    """A sub-agent's first call: one user turn, the shared preamble, its own brief, no launcher tool."""
    body = _first_call()
    body["tools"] = [t for t in body["tools"] if t["name"] != "Agent"]
    body["messages"] = [{"role": "user", "content": [{"type": "text", "text": _LONG_PREAMBLE},
                                                      {"type": "text", "text": "sub-agent brief B"}]}]
    return body


async def test_a_pinned_main_thread_does_not_pin_a_new_subagents_first_call(env, monkeypatch):
    monkeypatch.setenv(local_mode.EXEMPT_POLICY_ENV, "none")
    main = _main_thread(_req())
    main["messages"][0]["content"] = [{"type": "text", "text": _LONG_PREAMBLE}, {"type": "text", "text": "main brief"}]
    backend = Backend(_ollama("x"), _ollama("x"))
    app = env.app(backend)
    await _post(app, main)  # started elsewhere: the main thread is pinned claude
    await _post(app, json.loads(json.dumps(main)))
    await _post(app, _sub_first_call())
    rows = env.rows()
    assert rows[0]["reason"] == "conversation_started_on_claude" and rows[1]["reason"] == "pinned_claude"
    assert rows[2]["local_mode"]["pin"] is None or rows[2]["local_mode"]["pin"] == "local"
    assert rows[2]["reason"] != "pinned_claude" and rows[2]["local_mode"]["reason"] != "pinned_claude"


# -- P5: LLM_ROUTER_PROXY_MASK wires history_mask into the local path (default off) ------------------------
# The masked copy feeds the cap estimate, the pin decision, the backend payload and the ledger; Claude gets the original.

PLACEHOLDER = history_mask.PLACEHOLDER_PREFIX


def _long_history(result_chars: int = 20_000, pairs: int = 15) -> dict:
    """A main-session conversation of ``pairs`` tool round trips ending on a user tool_result (31 messages:
    mask boundary 20, so tool_results at messages 2..18 (9 of them) are masked and the last 11 messages stay verbatim)."""
    body = _first_call()
    first = body["messages"][0]
    msgs = [first]
    for i in range(pairs):
        msgs.append({"role": "assistant", "content": [
            {"type": "tool_use", "id": f"toolu_{i}", "name": "Bash", "input": {"command": f"cat f{i}.py"}}]})
        msgs.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"toolu_{i}",
             # only the results below the mask boundary are big, so masking is what brings it under the cap
             "content": ("line of output %d\n" % i) * ((result_chars if 2 * i + 2 < 20 else 20) // 20)}]})
    body["messages"] = msgs
    return body


def _texts(body: dict) -> list[str]:
    out = []
    for m in body["messages"]:
        if isinstance(m["content"], list):
            out += [str(b.get("content")) for b in m["content"] if b.get("type") == "tool_result"]
    return out


def _backend():
    # distinct replies: the loop guard refuses an identical repeated step
    return Backend(*[_ollama("", [_call("Bash", {"command": f"ls {i}"})]) for i in range(4)])


class _FailsAfterFirst(Backend):
    async def complete(self, body, timeout_s):
        if self.calls:
            self.calls.append(body)
            raise RuntimeError("backend down")
        return await super().complete(body, timeout_s)


async def _warm(app):
    """The conversation's first call, served locally: later calls of it are not 'started elsewhere'."""
    await _post(app, _first_call())


async def test_flag_off_payload_and_ledger_are_unchanged(env, monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_PROXY_MASK", raising=False)
    body = _long_history(result_chars=500)  # under the cap, so served locally
    backend = _backend()
    app = env.app(backend)
    await _warm(app)
    await _post(app, copy.deepcopy(body))
    assert backend.calls[-1]["messages"] == body["messages"]
    row = env.rows()[-1]
    assert "history_mask" not in row and "raw_est_prompt_tokens" not in row["local_mode"]


async def test_flag_off_over_cap_stays_on_claude(env, monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_PROXY_MASK", raising=False)
    backend = _backend()
    app = env.app(backend)
    await _warm(app)
    await _post(app, _long_history())
    assert len(backend.calls) == 1 and len(env.up.requests) == 1  # only the warm-up was local
    assert env.rows()[-1]["reason"] == "prompt_over_cap"


async def test_flag_on_masks_the_ollama_payload_and_the_cap_uses_the_masked_estimate(env, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_PROXY_CAP_AFTER_REDUCE", "1")
    monkeypatch.setenv("LLM_ROUTER_PROXY_MASK", "1")
    body = _long_history()
    assert local_mode.estimate_prompt_tokens(body) > local_mode.PROMPT_CAP_TOKENS  # raw would be refused
    backend = _backend()
    app = env.app(backend)
    await _warm(app)
    await _post(app, copy.deepcopy(body))
    assert len(backend.calls) == 2 and env.up.requests == []  # served locally: the cap saw the masked body
    sent = backend.calls[-1]
    texts = _texts(sent)
    assert sum(t.startswith(PLACEHOLDER) for t in texts) == 9
    assert texts[-1] == _texts(body)[-1]  # the verbatim window is untouched
    row = env.rows()[-1]
    hm = row["history_mask"]
    assert hm["messages_masked"] == 9 and hm["chars_saved"] > 0
    assert hm["raw_est_prompt_tokens"] > local_mode.PROMPT_CAP_TOKENS > hm["est_prompt_tokens"]
    lm = row["local_mode"]
    assert lm["raw_est_prompt_tokens"] == hm["raw_est_prompt_tokens"] > lm["est_prompt_tokens"]
    assert lm["est_prompt_tokens"] <= local_mode.PROMPT_CAP_TOKENS  # the decision saw the masked estimate
    assert "line of output" not in json.dumps(row)  # counts only, never text


async def test_claude_fallback_receives_the_original_unmasked_body(env, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_PROXY_CAP_AFTER_REDUCE", "1")
    monkeypatch.setenv("LLM_ROUTER_PROXY_MASK", "1")
    body = _long_history()
    backend = _FailsAfterFirst(_ollama("", [_call("Bash", {"command": "ls"})]))
    app = env.app(backend)
    await _warm(app)
    await _post(app, copy.deepcopy(body))
    assert len(backend.calls) == 2 and len(backend.calls[1]["messages"]) == len(body["messages"])
    assert any(t.startswith(PLACEHOLDER) for t in _texts(backend.calls[1]))  # local saw the masked copy
    (req,) = env.up.requests
    sent = json.loads(req.content)
    assert sent["messages"] == body["messages"]
    assert PLACEHOLDER not in req.content.decode()
    assert env.rows()[-1]["decision"] == "fallback"


async def test_masking_raising_serves_unmasked_and_records_failopen(env, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_PROXY_MASK", "1")
    seen = []
    monkeypatch.setattr(failopen, "record", lambda code, exc=None, **k: seen.append((code, type(exc).__name__)))

    def boom(*a, **k):
        raise ValueError("mask broke")

    monkeypatch.setattr(history_mask, "mask_body", boom)
    body = _long_history(result_chars=500)
    backend = _backend()
    app = env.app(backend)
    await _warm(app)
    r = await _post(app, copy.deepcopy(body))
    assert r.status_code == 200
    assert backend.calls[-1]["messages"] == body["messages"]
    assert ("LR-FO-PROXY-HISTORY-MASK", "ValueError") in seen
    assert "history_mask" not in env.rows()[-1]


async def test_the_pin_decision_uses_the_masked_estimate(env, monkeypatch):
    body = _long_history()
    monkeypatch.delenv("LLM_ROUTER_PROXY_MASK", raising=False)
    off = env.app(_backend())
    await _warm(off)
    await _post(off, copy.deepcopy(body))
    off_lm = env.rows()[-1]["local_mode"]
    assert off_lm["reason"] == "prompt_over_cap" and off_lm["pin_change"] == "local->claude"  # structural pin
    monkeypatch.setenv("LLM_ROUTER_PROXY_MASK", "1")
    monkeypatch.setenv("LLM_ROUTER_PROXY_CAP_AFTER_REDUCE", "1")
    env.ledger.unlink()
    on = env.app(_backend())  # a fresh app: its pins start empty
    await _warm(on)
    await _post(on, copy.deepcopy(body))
    row = env.rows()[-1]
    assert row["local_mode"]["local"] is True and "pin_change" not in row["local_mode"]
    await _post(on, copy.deepcopy(body))  # and the conversation is still local on the next turn
    assert env.rows()[-1]["local_mode"]["reason"] != "pinned_claude"


async def test_local_prefix_path_also_sends_the_masked_payload(env, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_PROXY_CAP_AFTER_REDUCE", "1")
    monkeypatch.setenv("LLM_ROUTER_PROXY_MASK", "1")
    body = _long_history()
    body["messages"][0]["content"] = [{"type": "text", "text": "local: continue the task"}]
    backend = _backend()
    await _post(env.app(backend), copy.deepcopy(body))
    assert len(backend.calls) == 1 and env.up.requests == []  # forced local, no warm-up needed
    texts = _texts(backend.calls[0])
    assert sum(t.startswith(PLACEHOLDER) for t in texts) == 9
    row = env.rows()[-1]
    assert row["local_mode"].get("forced") is True
    assert row["history_mask"]["messages_masked"] == 9
    assert row["local_mode"]["raw_est_prompt_tokens"] > local_mode.PROMPT_CAP_TOKENS


# -- P6: LLM_ROUTER_PROXY_CAP_AFTER_REDUCE tests the cap on the body that is sent locally (default off) -----


async def test_mask_on_cap_flag_off_still_caps_on_the_raw_estimate(env, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_PROXY_MASK", "1")
    monkeypatch.delenv("LLM_ROUTER_PROXY_CAP_AFTER_REDUCE", raising=False)
    backend = _backend()
    app = env.app(backend)
    await _warm(app)
    await _post(app, _long_history())
    assert len(backend.calls) == 1 and len(env.up.requests) == 1  # raw is over the cap: Claude, though masked fits
    row = env.rows()[-1]
    assert row["reason"] == "prompt_over_cap"
    lm = row["local_mode"]
    assert lm["cap_estimate"] == "raw" and lm["pin_change"] == "local->claude"
    assert lm["est_prompt_tokens"] > local_mode.PROMPT_CAP_TOKENS  # the decision saw the raw body
    assert lm["raw_est_prompt_tokens"] > local_mode.PROMPT_CAP_TOKENS
    assert lm["reduced_est_prompt_tokens"] <= local_mode.PROMPT_CAP_TOKENS


async def test_cap_flag_on_decides_on_the_reduced_estimate_and_records_both(env, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_PROXY_MASK", "1")
    monkeypatch.setenv("LLM_ROUTER_PROXY_CAP_AFTER_REDUCE", "1")
    backend = _backend()
    app = env.app(backend)
    await _warm(app)
    await _post(app, _long_history())
    assert len(backend.calls) == 2 and env.up.requests == []
    lm = env.rows()[-1]["local_mode"]
    assert lm["local"] is True and lm["cap_estimate"] == "reduced"
    assert lm["raw_est_prompt_tokens"] > local_mode.PROMPT_CAP_TOKENS >= lm["reduced_est_prompt_tokens"]
    assert lm["est_prompt_tokens"] <= local_mode.PROMPT_CAP_TOKENS


async def test_cap_flag_on_reduced_still_over_the_cap_goes_to_claude(env, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_PROXY_MASK", "1")
    monkeypatch.setenv("LLM_ROUTER_PROXY_CAP_AFTER_REDUCE", "1")
    backend = _backend()
    app = env.app(backend)
    await _warm(app)
    monkeypatch.setattr(local_mode, "PROMPT_CAP_TOKENS", 200)  # the masked body no longer fits either
    await _post(app, _long_history())
    assert len(backend.calls) == 1 and len(env.up.requests) == 1
    row = env.rows()[-1]
    assert row["reason"] == "prompt_over_cap" and row["local_mode"]["cap_estimate"] == "reduced"


@pytest.mark.parametrize("result_chars", [500, 20_000])  # one body under the cap, one over
async def test_cap_flag_is_inert_without_masking(env, monkeypatch, result_chars):
    """Mask off: the cap flag must change nothing (served payload, decision row) whatever its value."""
    monkeypatch.delenv("LLM_ROUTER_PROXY_MASK", raising=False)
    body = _long_history(result_chars=result_chars)
    seen = []
    for value in (None, "1"):
        if value is None:
            monkeypatch.delenv("LLM_ROUTER_PROXY_CAP_AFTER_REDUCE", raising=False)
        else:
            monkeypatch.setenv("LLM_ROUTER_PROXY_CAP_AFTER_REDUCE", value)
        env.ledger.unlink(missing_ok=True)
        env.up.requests.clear()
        backend = _backend()
        app = env.app(backend)
        await _warm(app)
        await _post(app, copy.deepcopy(body))
        row = env.rows()[-1]
        seen.append((backend.calls, len(env.up.requests), row["reason"], row["decision"], row["local_mode"],
                     row["est_prompt_tokens"], sorted(set(row) & {"history_mask", "cap_estimate"})))
    assert seen[0] == seen[1]
    assert "cap_estimate" not in seen[0][4] and "history_mask" not in seen[0][6]


async def test_cap_flag_on_overflow_guard_still_falls_back_to_claude(env, monkeypatch):
    """The reduced estimate fits, the real local payload does not: the backend's local_context_guard
    raises ContextOverflow and the step goes to Claude with the unmasked original."""
    from llm_router.local_context_guard import ContextOverflow

    monkeypatch.setenv("LLM_ROUTER_PROXY_MASK", "1")
    monkeypatch.setenv("LLM_ROUTER_PROXY_CAP_AFTER_REDUCE", "1")
    body = _long_history()
    assert local_mode.estimate_prompt_tokens(body) > local_mode.PROMPT_CAP_TOKENS
    backend = Backend(_ollama("", [_call("Bash", {"command": "ls"})]))
    app = env.app(backend)
    await _warm(app)
    backend.exc = ContextOverflow("prompt ~99999 tokens > window")
    await _post(app, copy.deepcopy(body))
    assert len(backend.calls) == 2  # the cap let it through (reduced fits); the guard refused it
    row = env.rows()[-1]
    assert row["local_mode"]["cap_estimate"] == "reduced" and row["local_mode"]["local"] is True
    assert row["decision"] == "fallback" and row["reason"] == "local_context_overflow"
    (req,) = env.up.requests
    assert json.loads(req.content)["messages"] == body["messages"] and PLACEHOLDER not in req.content.decode()


@pytest.mark.parametrize("value", [None, "0", "false", "off", ""])
def test_flag_not_on_never_imports_history_mask(value):
    """A fresh interpreter (this module imports history_mask at the top): an off-looking value must not import it."""
    import subprocess
    import sys
    code = ("import sys\n"
            "from llm_router.proxy.server import mask_local_body\n"
            "body = {'messages': [{'role': 'user', 'content': 'hi'}]}\n"
            "row = {}\n"
            "assert mask_local_body(body, row) is body and row == {}\n"
            "print('llm_router.proxy.history_mask' in sys.modules)\n")
    env = {k: v for k, v in os.environ.items() if k != "LLM_ROUTER_PROXY_MASK"}
    if value is not None:
        env["LLM_ROUTER_PROXY_MASK"] = value
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    assert out.returncode == 0, out.stderr[-500:]
    assert out.stdout.strip().splitlines()[-1] == "False"


# -- P7: LLM_ROUTER_UNPIN lifts a prompt_over_cap pin once the reduced estimate fits (default off) ---------


async def _pin_over_cap(env, monkeypatch, backend):
    """Warm, then an over-cap turn with masking on and CAP_AFTER_REDUCE off: the structural pin."""
    monkeypatch.setenv("LLM_ROUTER_PROXY_MASK", "1")
    monkeypatch.delenv("LLM_ROUTER_PROXY_CAP_AFTER_REDUCE", raising=False)
    app = env.app(backend)
    await _warm(app)
    await _post(app, _long_history())
    row = env.rows()[-1]
    assert row["reason"] == "prompt_over_cap" and row["local_mode"]["pin_change"] == "local->claude"
    return app


async def test_unpin_lifts_prompt_over_cap_pin_when_reduced_fits(env, monkeypatch):
    backend = _backend()
    app = await _pin_over_cap(env, monkeypatch, backend)
    n_local = len(backend.calls)
    monkeypatch.setenv("LLM_ROUTER_PROXY_CAP_AFTER_REDUCE", "1")
    monkeypatch.setenv("LLM_ROUTER_UNPIN", "1")
    await _post(app, _long_history())
    assert len(backend.calls) == n_local + 1  # served locally again
    lm = env.rows()[-1]["local_mode"]
    assert lm["local"] is True and lm["pin"] == "local"  # unpinned, then pinned local again
    assert lm["unpin"] == {"reason": "reduced_fits", "reduced_est": lm["est_prompt_tokens"],
                           "cap": local_mode.prompt_cap_tokens()}
    assert lm["unpin"]["reduced_est"] <= lm["unpin"]["cap"]
    assert "line of output" not in json.dumps(env.rows()[-1])  # counts only


@pytest.mark.parametrize("flags", [
    {"LLM_ROUTER_UNPIN": None, "LLM_ROUTER_PROXY_CAP_AFTER_REDUCE": "1", "LLM_ROUTER_PROXY_MASK": "1"},
    {"LLM_ROUTER_UNPIN": "1", "LLM_ROUTER_PROXY_CAP_AFTER_REDUCE": None, "LLM_ROUTER_PROXY_MASK": "1"},
    {"LLM_ROUTER_UNPIN": "1", "LLM_ROUTER_PROXY_CAP_AFTER_REDUCE": "1", "LLM_ROUTER_PROXY_MASK": None},
], ids=["unpin_off", "cap_after_reduce_off", "masking_not_applied"])
async def test_unpin_not_applied_when_a_condition_is_missing(env, monkeypatch, flags):
    backend = _backend()
    app = await _pin_over_cap(env, monkeypatch, backend)
    n_local = len(backend.calls)
    for name, value in flags.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    await _post(app, _long_history())
    assert len(backend.calls) == n_local
    lm = env.rows()[-1]["local_mode"]
    assert lm["reason"] == "pinned_claude" and "unpin" not in lm


def test_unpin_never_lifts_a_media_or_unreasoned_pin():
    lm = local_mode.LocalModeState(kill_path=Path("/nonexistent/kill"))
    lm.set_pin("m", local_mode.PIN_CLAUDE, local_mode.REASON_MEDIA)
    lm.set_pin("f", local_mode.PIN_CLAUDE)  # e.g. a failed local step
    lm.set_pin("l", local_mode.PIN_LOCAL)
    assert not any(lm.unpin_over_cap(k) for k in ("m", "f", "l", "absent"))
    assert lm.pin_of("m") == lm.pin_of("f") == local_mode.PIN_CLAUDE
    lm.set_pin("c", local_mode.PIN_CLAUDE, local_mode.REASON_PROMPT_CAP)
    lm.set_pin("c", local_mode.PIN_CLAUDE)  # re-pinned for another reason: the cap reason is gone
    assert not lm.unpin_over_cap("c")


async def test_unpin_at_most_once_per_session(env, monkeypatch):
    backend = _backend()
    app = await _pin_over_cap(env, monkeypatch, backend)
    monkeypatch.setenv("LLM_ROUTER_PROXY_CAP_AFTER_REDUCE", "1")
    monkeypatch.setenv("LLM_ROUTER_UNPIN", "1")
    await _post(app, _long_history())
    assert "unpin" in env.rows()[-1]["local_mode"]
    real_cap = local_mode.PROMPT_CAP_TOKENS
    monkeypatch.setattr(local_mode, "PROMPT_CAP_TOKENS", 200)  # over the cap again, even reduced
    await _post(app, _long_history())
    row = env.rows()[-1]
    assert row["reason"] == "prompt_over_cap" and row["local_mode"]["pin_change"] == "local->claude"
    monkeypatch.setattr(local_mode, "PROMPT_CAP_TOKENS", real_cap)
    n_local = len(backend.calls)
    await _post(app, _long_history())  # now it would fit, but the one unpin is spent
    assert len(backend.calls) == n_local
    lm = env.rows()[-1]["local_mode"]
    assert lm["reason"] == "pinned_claude" and "unpin" not in lm


async def test_flag_off_ledger_rows_identical_whatever_the_unpin_state(env, monkeypatch):
    """UNPIN unset vs explicitly off: the same decision rows (no unpin key), same pin store."""
    rows = []
    for value in (None, "0"):
        env.ledger.unlink(missing_ok=True)
        backend = _backend()
        app = await _pin_over_cap(env, monkeypatch, backend)
        if value is None:
            monkeypatch.delenv("LLM_ROUTER_UNPIN", raising=False)
        else:
            monkeypatch.setenv("LLM_ROUTER_UNPIN", value)
        monkeypatch.setenv("LLM_ROUTER_PROXY_CAP_AFTER_REDUCE", "1")
        await _post(app, _long_history())
        r = env.rows()[-1]["local_mode"]
        assert "unpin" not in r
        rows.append(r)
    assert rows[0] == rows[1]


async def _unpin_ready(env, monkeypatch, breaker=None):
    backend = _backend()
    monkeypatch.setenv("LLM_ROUTER_PROXY_MASK", "1")
    monkeypatch.delenv("LLM_ROUTER_PROXY_CAP_AFTER_REDUCE", raising=False)
    app = env.app(backend, breaker=breaker)
    await _warm(app)
    await _post(app, _long_history())
    assert env.rows()[-1]["local_mode"]["pin_change"] == "local->claude"
    monkeypatch.setenv("LLM_ROUTER_PROXY_CAP_AFTER_REDUCE", "1")
    monkeypatch.setenv("LLM_ROUTER_UNPIN", "1")
    return app, backend


async def test_transient_not_local_turn_leaves_the_unpin_unspent(env, monkeypatch):
    app, backend = await _unpin_ready(env, monkeypatch)
    n = len(backend.calls)
    real = local_mode.decide_local
    monkeypatch.setattr(local_mode, "decide_local", lambda body, s: real(
        body, dataclasses.replace(s, backend_healthy=False)))  # the backend is unhealthy for one turn
    await _post(app, _long_history())
    lm = env.rows()[-1]["local_mode"]
    assert len(backend.calls) == n and lm["reason"] == "backend_unhealthy" and "unpin" not in lm
    monkeypatch.setattr(local_mode, "decide_local", real)
    await _post(app, _long_history())  # the lift is still available
    lm = env.rows()[-1]["local_mode"]
    assert len(backend.calls) == n + 1 and lm["local"] is True and "unpin" in lm


async def test_kill_switch_and_exempt_turns_leave_the_unpin_unspent(env, monkeypatch):
    app, backend = await _unpin_ready(env, monkeypatch)
    n = len(backend.calls)
    env.kill.write_text("")
    await _post(app, _long_history())
    lm = env.rows()[-1]["local_mode"]
    assert lm["reason"] == "kill_switch" and "unpin" not in lm
    env.kill.unlink()
    real = local_mode.exempt_reason
    monkeypatch.setattr(local_mode, "exempt_reason", lambda *a, **k: (local_mode.EXEMPT_COORDINATOR, None))
    await _post(app, _long_history())
    lm = env.rows()[-1]["local_mode"]
    assert lm["reason"] == "exempt_session" and "unpin" not in lm
    monkeypatch.setattr(local_mode, "exempt_reason", real)
    await _post(app, _long_history())
    assert len(backend.calls) == n + 1 and "unpin" in env.rows()[-1]["local_mode"]


async def test_unpin_needs_messages_actually_masked(env, monkeypatch):
    app, backend = await _unpin_ready(env, monkeypatch)
    n = len(backend.calls)
    monkeypatch.setattr(local_mode, "PROMPT_CAP_TOKENS", 10**9)  # the body fits, so only the mask gate decides
    stats = types.SimpleNamespace(masked=0, chars_saved=0, est_tokens_before=1, est_tokens_after=1)
    monkeypatch.setattr(history_mask, "mask_body", lambda body: (body, stats))
    await _post(app, _long_history())
    lm = env.rows()[-1]["local_mode"]
    assert env.rows()[-1]["history_mask"]["messages_masked"] == 0
    assert len(backend.calls) == n and lm["reason"] == "pinned_claude" and "unpin" not in lm


def _raw_with_deep_tool_schema(levels: int) -> str:
    body = _first_call()
    body["tools"][0]["input_schema"]["x_deep"] = "@@"
    return json.dumps(body).replace('"@@"', '{"k":' * levels + "1" + "}" * levels)


@pytest.mark.parametrize("levels", [100_000, 5000, 500])
async def test_a_deeply_nested_request_goes_to_claude_and_never_crashes(env, levels):
    """ESTREC-1 through the whole proxy: 5000 deep fails json.loads (was a 500); 500 deep parses and failed in
    the estimators (was reason backend_error). Both are forwarded to Claude, none is served locally."""
    headers = {"authorization": "Bearer sk-ant-oat01-" + "Zq9" * 20, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    app = env.app(Backend(_ollama("", [_call("Bash", {"command": "ls"})])))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                                 base_url="http://127.0.0.1:8787") as c:
        r = await c.post("/v1/messages?beta=true", content=_raw_with_deep_tool_schema(levels), headers=headers)
    assert r.status_code == 200 and len(env.up.requests) == 1
    if levels == 500:
        (row,) = env.rows()
        assert row["decision"] == "fallback" and row["reason"] == local_mode.REASON_PROMPT_UNMEASURABLE
        assert "1000000000" not in json.dumps(row)


async def test_an_id_less_backend_reply_is_served_with_an_id_and_a_ledger_row(env):
    """BUG-IDLESS-1: message["id"] raised KeyError (500, no row) when a backend returned a message without one."""
    class IdLess(Backend):
        async def complete(self, body, timeout_s):
            m, err, usage = await super().complete(body, timeout_s)
            m.pop("id")
            return m, err, usage

    r = await _post(env.app(IdLess(_ollama("", [_call("Bash", {"command": "ls"})]))), _first_call())
    assert r.status_code == 200
    (start,) = [json.loads(ln[6:]) for ln in r.text.splitlines()
                if ln.startswith("data: ") and '"message_start"' in ln]
    sent_id = start["message"]["id"]
    assert sent_id.startswith("msg_lr")
    (row,) = env.rows()
    assert row["decision"] == "served" and row["msg_id"] == sent_id

