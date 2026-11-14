"""R1 tool-call gate (llm_router.proxy.r1_gate, LLM_ROUTER_PROXY_R1_GATE): a SUB step's local reply is served
only if it is a well-formed tool call; everything else is discarded before delivery and goes to Claude."""

from __future__ import annotations

import json

import pytest

from llm_router.proxy import r1_gate
from tests.test_proxy_local_mode import (  # noqa: F401  (fixtures)
    Backend, _call, _next_turn, _ollama, _overrides_readable, _post, _req, _served_blocks, env)

PARSE_ERR = ("error parsing tool call: raw='{\"command\": \"ls', err=unexpected end of JSON input "
             "SECRET-PARSER-ERROR-TEXT")


@pytest.fixture(autouse=True)
def _sub_env(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_PROXY_R1_GATE", raising=False)
    monkeypatch.setenv("LLM_ROUTER_LOCAL_EXEMPT", "none")  # let a sub-agent thread reach the local path


def _sub_first() -> dict:
    body = _req()
    body["messages"] = body["messages"][:2]  # no Agent launcher in the tools: a sub-agent thread
    return body


def _on(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_PROXY_R1_GATE", "1")


async def _escalates(env, reply, reason):  # noqa: F811  # noqa: F811
    backend = Backend(reply)
    r = await _post(env.app(backend), _sub_first())
    assert r.status_code == 200 and len(backend.calls) == 1
    assert len(env.up.requests) == 1  # went to Claude
    assert b"SECRET-PARSER-ERROR-TEXT" not in r.content and "SECRET-PARSER-ERROR-TEXT" not in r.text
    (row,) = env.rows()
    assert row["is_main_thread"] is False
    assert row["decision"] == "fallback" and row["r1_gate"] == "escalated" and row["r1_reason"] == reason
    return row


def test_flag_default_off(monkeypatch):
    assert r1_gate.enabled() is False
    monkeypatch.setenv("LLM_ROUTER_PROXY_R1_GATE", "on")
    assert r1_gate.enabled() is True


# 1. parser error text is never forwarded
async def test_parser_error_text_is_never_forwarded(env, monkeypatch):  # noqa: F811
    _on(monkeypatch)
    await _escalates(env, _ollama(PARSE_ERR), "r1_parse_error")


async def test_parser_error_text_is_never_forwarded_streaming(env, monkeypatch):  # noqa: F811
    _on(monkeypatch)
    backend = Backend(_ollama(PARSE_ERR))
    r = await _post(env.app(backend), dict(_sub_first(), stream=True))
    assert "SECRET-PARSER-ERROR-TEXT" not in r.text and len(env.up.requests) == 1
    assert env.rows()[0]["r1_reason"] == "r1_parse_error"


# 2. empty reply
def test_empty_reply_is_escalated():
    body = _sub_first()
    assert r1_gate.check({"content": []}, body) == (False, "r1_empty")
    assert r1_gate.check({"content": [{"type": "text", "text": "  "}]}, body) == (False, "r1_empty")
    assert r1_gate.check(None, body) == (False, "r1_empty")


async def test_empty_reply_through_the_proxy_reaches_claude(env, monkeypatch):  # noqa: F811
    _on(monkeypatch)
    backend = Backend(_ollama(""))
    r = await _post(env.app(backend), _sub_first())
    assert r.status_code == 200 and len(env.up.requests) == 1
    (row,) = env.rows()
    assert row["decision"] == "fallback"


# 3. text-only reply
async def test_text_only_reply_is_escalated(env, monkeypatch):  # noqa: F811
    _on(monkeypatch)
    await _escalates(env, _ollama("All done, nothing to run."), "r1_text")


# 4. invalid calls
def test_unknown_tool_name_is_escalated():
    msg = {"content": [{"type": "tool_use", "id": "t", "name": "Nope", "input": {"x": 1}}]}
    assert r1_gate.check(msg, _sub_first()) == (False, "r1_unknown_tool")


async def test_unknown_tool_name_through_the_proxy_reaches_claude(env, monkeypatch):  # noqa: F811
    # the translate layer already refuses a tool name that was not offered, so the gate is not reached here;
    # the step still goes to Claude and the model's call is never delivered.
    _on(monkeypatch)
    backend = Backend(_ollama("", [_call("Nope", {"x": 1})]))
    r = await _post(env.app(backend), _sub_first())
    assert r.status_code == 200 and len(env.up.requests) == 1
    assert env.rows()[0]["decision"] == "fallback"


def test_input_that_is_not_a_json_object_is_escalated():
    body = _sub_first()
    for inp in ("{not json", "[1, 2]", 7, None):
        msg = {"content": [{"type": "tool_use", "id": "t", "name": "Bash", "input": inp}]}
        assert r1_gate.check(msg, body) == (False, "r1_invalid_input"), inp


def test_missing_required_key_is_escalated():
    body = _sub_first()
    required = next(t for t in body["tools"] if t["name"] == "Bash")["input_schema"].get("required")
    assert required, "fixture Bash tool must declare required keys"
    msg = {"content": [{"type": "tool_use", "id": "t", "name": "Bash", "input": {}}]}
    assert r1_gate.check(msg, body) == (False, "r1_invalid_input")


async def test_missing_required_key_through_the_proxy_reaches_claude(env, monkeypatch):  # noqa: F811
    _on(monkeypatch)  # the translate layer's own validation also refuses this; either way Claude gets the step
    r = await _post(env.app(Backend(_ollama("", [_call("Bash", {})]))), _sub_first())
    assert r.status_code == 200 and len(env.up.requests) == 1
    assert env.rows()[0]["decision"] == "fallback"


def test_one_good_call_among_bad_ones_serves():
    msg = {"content": [{"type": "tool_use", "id": "a", "name": "Nope", "input": {}},
                       {"type": "tool_use", "id": "b", "name": "Bash", "input": {"command": "ls"}}]}
    assert r1_gate.check(msg, _sub_first()) == (True, None)


# 5. well-formed call served unchanged
async def test_well_formed_call_is_served_and_bytes_equal_the_flag_off_path(env, monkeypatch):  # noqa: F811
    reply = _ollama("", [_call("Bash", {"command": "ls"})])
    off = Backend(reply)
    r_off = await _post(env.app(off), _sub_first())
    _on(monkeypatch)
    env2 = env  # same fixture object: second conversation id differs only by msg id, compare block content
    r_on = await _post(env2.app(Backend(reply)), _sub_first())
    assert env.up.requests == []
    assert [b["name"] for b in _served_blocks(r_on)] == ["Bash"]
    strip = lambda t: "\n".join(ln for ln in t.splitlines() if '"id"' not in ln and "msg_" not in ln)  # noqa: E731
    assert strip(r_on.text) == strip(r_off.text)
    off_row, on_row = env.rows()
    assert on_row["decision"] == "served" and on_row["r1_gate"] == "served" and on_row["r1_reason"] is None
    assert "r1_gate" not in off_row and "r1_reason" not in off_row


# 6. no fallback after a tool result was returned
async def test_a_step_after_a_returned_tool_result_is_gated_alone_and_nothing_re_executes(env, monkeypatch):  # noqa: F811
    _on(monkeypatch)
    backend = Backend(_ollama("", [_call("Bash", {"command": "ls"})]), _ollama("prose, no call"))
    app = env.app(backend)
    first = await _post(app, _sub_first())
    msg = {"content": _served_blocks(first)}
    assert [b["name"] for b in msg["content"]] == ["Bash"] and env.up.requests == []
    r = await _post(app, dict(_next_turn(_sub_first(), msg), stream=False))
    assert r.status_code == 200 and len(env.up.requests) == 1  # only step 2 reached Claude
    sent = json.loads(env.up.requests[0].content)
    # the already-executed Bash call is history in the request, never a fresh instruction: one tool_use, one result
    assert sum(1 for m in sent["messages"] for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_use") == 1
    rows = env.rows()
    assert [(x["decision"], x["r1_gate"]) for x in rows] == [("served", "served"), ("fallback", "escalated")]
    assert len(backend.calls) == 2  # the local model was not re-asked for step 1


# 7. flag off: no gate
async def test_flag_off_serves_a_text_reply_and_adds_no_fields(env):  # noqa: F811
    backend = Backend(_ollama(PARSE_ERR))
    r = await _post(env.app(backend), _sub_first())
    assert r.status_code == 200 and env.up.requests == []
    (row,) = env.rows()
    assert row["decision"] == "served" and not [k for k in row if k.startswith("r1_")]


async def test_main_thread_steps_are_not_gated(env, monkeypatch):  # noqa: F811
    from tests.test_proxy_local_mode import _first_call

    _on(monkeypatch)
    r = await _post(env.app(Backend(_ollama("plain answer"))), _first_call())
    assert r.status_code == 200 and env.up.requests == []
    (row,) = env.rows()
    assert row["decision"] == "served" and "r1_gate" not in row
