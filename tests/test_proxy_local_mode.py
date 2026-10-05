"""``--serve local-agent`` (llm_router.proxy.local_mode): P1-P3, pinning, the
eligibility stub, the preflight, and "never silent".

Anthropic is a mocked upstream and the serving model a fake backend; nothing
here reaches the network or a real Ollama (the preflight takes injected
``get`` / ``runner`` functions). Off mode is pinned by test_proxy_off_golden.py.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import httpx
import pytest

from llm_router.local_agent import capability as cap
from llm_router.proxy import ledger, local_mode
from llm_router.proxy import server as ps
from llm_router.proxy.translate import from_ollama

FIXTURE = Path(__file__).parent / "fixtures" / "proxy" / "continuation_request.json"
MODEL = "ollama/fake:1"


def _req() -> dict:
    return json.loads(FIXTURE.read_text())


def _first_call() -> dict:
    body = _req()
    body["messages"] = body["messages"][:2]
    return body


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
    assert sent["system"] == _req()["system"] and [t["name"] for t in sent["tools"]] == ["Bash", "Edit", "Read", "Write"]
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
    r = await _post(app, _req())  # 3+ turns, never seen the first call
    assert r.status_code == 200 and backend.calls == [] and len(env.up.requests) == 1
    (row,) = env.rows()
    assert row["reason"] == "conversation_started_on_claude" and row["egress"] is True
    await _post(app, _req())  # still Claude's
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
