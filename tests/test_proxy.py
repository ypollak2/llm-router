"""Per-call proxy (llm_router.proxy): translation, fallback, redaction, ledger.

The request fixture is a real Claude Code 2.1.283 continuation request captured
by the 2026-09-28 spike, with the system prompt, tool descriptions, paths and
identity fields replaced (tests/fixtures/proxy/continuation_request.json).
Anthropic is a mocked upstream (httpx.MockTransport); the serving model is a
fake backend. No test makes a network call.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from pathlib import Path

import httpx
import pytest

from llm_router.proxy import backends as pb
from llm_router.proxy import ledger
from llm_router.proxy import server as ps
from llm_router.proxy.steps import STEP_CONTINUATION, classify_text, session_id_of, step_class
from llm_router.proxy.translate import (
    SERVED_TOOL_ID_PREFIX,
    from_ollama,
    has_served_turn,
    parse_sse_usage,
    sse_from_message,
    to_ollama,
    without_thinking,
)

FIXTURE = Path(__file__).parent / "fixtures" / "proxy" / "continuation_request.json"
TOKEN = "sk-ant-oat01-" + "Zq9" * 20  # fake, shaped like a Max OAuth access token
SID = "11111111-2222-3333-4444-555555555555"


def _req() -> dict:
    return json.loads(FIXTURE.read_text())


def _first_call() -> dict:
    body = _req()
    body["messages"] = [m for m in body["messages"][:2]]
    return body


# ── steps ────────────────────────────────────────────────────────────────────


def test_fixture_is_a_continuation_even_with_trailing_system_message():
    body = _req()
    assert body["messages"][-1]["role"] == "system"  # reminder AFTER the tool_result
    assert step_class(body, {STEP_CONTINUATION}) == STEP_CONTINUATION


def test_first_call_is_never_a_continuation():
    assert step_class(_first_call(), {STEP_CONTINUATION}) is None


def test_not_enabled_forced_tool_choice_media_and_no_tools_are_ineligible():
    body = _req()
    assert step_class(body, set()) is None
    forced = dict(body, tool_choice={"type": "any"})
    assert step_class(forced, {STEP_CONTINUATION}) is None
    no_tools = dict(body, tools=[])
    assert step_class(no_tools, {STEP_CONTINUATION}) is None
    media = copy.deepcopy(body)
    tr = [m for m in media["messages"] if m["role"] == "user"][-1]["content"][0]
    tr["content"] = [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA"}}]
    assert step_class(media, {STEP_CONTINUATION}) is None


def test_session_id_is_the_only_identity_field_read():
    assert session_id_of(_req()) == SID
    assert session_id_of({"metadata": {"user_id": "not json"}}) is None


def test_classify_text_is_ask_plus_newest_tool_output():
    text = classify_text(_req())
    assert "add_one" in text
    assert "has been updated" in text
    assert "truncated for fixture" not in text  # the system prompt is not classified


# ── request translation ─────────────────────────────────────────────────────


def test_to_ollama_system_blocks_tools_tool_use_and_tool_result():
    body = _req()
    payload = to_ollama(body, "qwen3-coder:30b", num_ctx=32768)
    msgs = payload["messages"]
    assert msgs[0]["role"] == "system"
    assert "You are Claude Code" in msgs[0]["content"] and "truncated for fixture" in msgs[0]["content"]
    # role:system messages inside `messages` stay system messages, in order
    assert sum(1 for m in msgs if m["role"] == "system") == 1 + sum(
        1 for m in body["messages"] if m["role"] == "system")
    assistants = [m for m in msgs if m["role"] == "assistant"]
    assert [c["function"]["name"] for c in assistants[0]["tool_calls"]] == ["Read"]
    assert isinstance(assistants[0]["tool_calls"][0]["function"]["arguments"], dict)
    tools_msgs = [m for m in msgs if m["role"] == "tool"]
    assert [m["tool_name"] for m in tools_msgs] == ["Read", "Edit"]
    # thinking blocks and their signatures never leave for another model
    assert "SIG-REDACTED" not in json.dumps(payload)
    for key in ("thinking", "context_management", "output_config", "metadata"):
        assert key not in payload
    assert {t["function"]["name"] for t in payload["tools"]} == {t["name"] for t in body["tools"]}
    assert payload["think"] is False and payload["stream"] is False
    assert payload["options"]["num_ctx"] == 32768


def test_to_ollama_marks_errors_and_replaces_media():
    body = _req()
    last_user = [m for m in body["messages"] if m["role"] == "user"][-1]
    last_user["content"][0]["is_error"] = True
    body["messages"][0]["content"].append({"type": "image", "source": {}})
    msgs = to_ollama(body, "m", num_ctx=1024)["messages"]
    assert [m for m in msgs if m["role"] == "tool"][-1]["content"].startswith("ERROR: ")
    assert "[image omitted by llm-router proxy]" in json.dumps(msgs)


def test_server_tools_without_input_schema_are_not_offered():
    body = dict(_req(), tools=_req()["tools"] + [{"type": "web_search_20250305", "name": "web_search"}])
    names = [t["function"]["name"] for t in to_ollama(body, "m", num_ctx=1024)["tools"]]
    assert "web_search" not in names


# ── reply translation and validation ────────────────────────────────────────


def _ollama(content="", calls=None, done_reason="stop"):
    return {"message": {"role": "assistant", "content": content, "tool_calls": calls or []},
            "done_reason": done_reason, "prompt_eval_count": 1234, "eval_count": 56}


def _call(name, args):
    return {"function": {"name": name, "arguments": args}}


def test_from_ollama_valid_tool_use():
    m, err = from_ollama(_ollama("Running the test.", [_call("Bash", {"command": "python3 tests/test_mod01.py"})]),
                         _req())
    assert err is None
    assert m["stop_reason"] == "tool_use"
    assert [b["type"] for b in m["content"]] == ["text", "tool_use"]
    tu = m["content"][1]
    assert tu["id"].startswith(SERVED_TOOL_ID_PREFIX) and tu["name"] == "Bash"
    assert m["model"] == "claude-sonnet-5"  # echoes the requested model
    assert m["usage"]["cache_read_input_tokens"] == 0


def test_from_ollama_string_args_and_think_tags():
    m, err = from_ollama(_ollama("<think>hmm</think>Done.",
                                 [_call("Read", json.dumps({"file_path": "/work/repo/pkg/mod01.py"}))]), _req())
    assert err is None
    assert m["content"][0] == {"type": "text", "text": "Done."}
    assert m["content"][1]["input"] == {"file_path": "/work/repo/pkg/mod01.py"}


@pytest.mark.parametrize("calls,reason", [
    ([_call("Nope", {})], "unknown tool 'Nope'"),
    ([_call("Read", {})], "Read: missing required 'file_path'"),
    ([_call("Read", {"file_path": 7})], "Read: 'file_path' should be string"),
    ([_call("Read", "{not json")], "tool args not JSON for Read"),
    ([_call("Read", ["x"])], "Read: input not an object"),
])
def test_from_ollama_rejects_invalid_calls(calls, reason):
    m, err = from_ollama(_ollama("", calls), _req())
    assert m is None and err == reason


def test_from_ollama_empty_reply_is_rejected_and_text_reply_ends_turn():
    assert from_ollama(_ollama(""), _req()) == (None, "empty response")
    m, _ = from_ollama(_ollama("All tests pass."), _req())
    assert m["stop_reason"] == "end_turn"
    m, _ = from_ollama(_ollama("cut", done_reason="length"), _req())
    assert m["stop_reason"] == "max_tokens"


def _replay_sse(raw: bytes) -> dict:
    """Assemble a message from SSE the way an Anthropic SDK client does."""
    msg, blocks, partial = None, {}, {}
    for chunk in raw.decode().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in chunk.splitlines() if ": " in line)
        if not lines:
            continue
        d = json.loads(lines["data"])
        assert lines["event"] == d["type"]
        if d["type"] == "message_start":
            msg = d["message"]
        elif d["type"] == "content_block_start":
            blocks[d["index"]] = dict(d["content_block"])
        elif d["type"] == "content_block_delta":
            delta = d["delta"]
            if delta["type"] == "text_delta":
                blocks[d["index"]]["text"] += delta["text"]
            else:
                partial[d["index"]] = partial.get(d["index"], "") + delta["partial_json"]
        elif d["type"] == "content_block_stop" and d["index"] in partial:
            blocks[d["index"]]["input"] = json.loads(partial[d["index"]])
        elif d["type"] == "message_delta":
            msg["stop_reason"] = d["delta"]["stop_reason"]
            msg["usage"]["output_tokens"] = d["usage"]["output_tokens"]
    msg["content"] = [blocks[i] for i in sorted(blocks)]
    return msg


def test_sse_round_trip_matches_the_message():
    m, _ = from_ollama(_ollama("Now the test.", [_call("Bash", {"command": "python3 t.py"})]), _req())
    raw = sse_from_message(m)
    assert raw.decode().startswith("event: message_start")
    assert raw.decode().rstrip().endswith('data: {"type": "message_stop"}')
    rebuilt = _replay_sse(raw)
    assert rebuilt["content"] == m["content"]
    assert rebuilt["stop_reason"] == "tool_use"
    usage, stop, msg_id = parse_sse_usage(raw)
    assert (stop, msg_id) == ("tool_use", m["id"])


# ── thinking / context_management on mixed histories ─────────────────────────


def test_served_turn_is_detected_in_history():
    body = _req()
    assert not has_served_turn(body)
    served, _ = from_ollama(_ollama("", [_call("Bash", {"command": "ls"})]), body)
    body["messages"].append({"role": "assistant", "content": served["content"]})
    assert has_served_turn(body)


def test_without_thinking_drops_only_thinking_and_clear_thinking_edits():
    body = _req()
    body["context_management"]["edits"].append({"type": "clear_tool_uses_20250919"})
    out = without_thinking(body)
    assert "thinking" not in out
    assert out["context_management"]["edits"] == [{"type": "clear_tool_uses_20250919"}]
    assert "thinking" in body  # input untouched
    only_thinking = without_thinking(_req())
    assert "context_management" not in only_thinking
    # prior thinking blocks stay in history untouched
    assert json.dumps(only_thinking["messages"]) == json.dumps(_req()["messages"])


# ── server: pass-through, served, fallback ──────────────────────────────────


def _sse_reply(msg_id="msg_upstream01", usage=None):
    usage = usage or {"input_tokens": 3, "cache_read_input_tokens": 43_500,
                      "cache_creation_input_tokens": 800,
                      "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 800},
                      "output_tokens": 1}
    events = [
        ("message_start", {"type": "message_start", "message": {
            "id": msg_id, "type": "message", "role": "assistant", "model": "claude-sonnet-5",
            "content": [], "stop_reason": None, "usage": usage}}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": "ok"}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                           "usage": {"output_tokens": 42}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    return "".join(f"event: {n}\ndata: {json.dumps(d)}\n\n" for n, d in events).encode()


class Upstream:
    """Mocked Anthropic. Records every request it receives."""

    def __init__(self, responses=None):
        self.requests: list[httpx.Request] = []
        self.responses = list(responses or [])

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.responses:
            status, body = self.responses.pop(0)
        else:
            status, body = 200, _sse_reply()
        ctype = "text/event-stream" if body.startswith(b"event:") else "application/json"
        return httpx.Response(status, stream=httpx.ByteStream(body), headers={"content-type": ctype, "request-id": "req_x"})


class FakeBackend:
    def __init__(self, reply=None, *, delay=0.0, exc=None):
        self.reply, self.delay, self.exc, self.calls = reply, delay, exc, []

    async def complete(self, body, timeout_s):
        self.calls.append(body)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc:
            raise self.exc
        m, err = from_ollama(self.reply, body)
        return m, err, {"prompt_tokens": 10, "output_tokens": 2}


@pytest.fixture
def policy(monkeypatch):
    """Pin the policy decision: route to a fake tool-capable model."""
    decision = {"task_type": "code", "complexity": "moderate",
                "chain_head": ["ollama/fake:1"], "model": "ollama/fake:1"}

    async def _choose(text, pinned):
        return dict(decision)

    monkeypatch.setattr(ps, "choose_model", _choose)
    return decision


def _app(tmp_path, upstream, backend=None, **cfg):
    config = ps.ProxyConfig(upstream="http://127.0.0.1:9", ledger_path=tmp_path / "proxy_calls.jsonl", **cfg)
    client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    return ps.build_app(config, client=client, backend_factory=lambda model: backend)


async def _post(app, body, headers=None):
    h = {"authorization": f"Bearer {TOKEN}", "anthropic-version": "2023-06-01",
         "anthropic-beta": "oauth-2025-04-20,claude-code-20250219", "content-type": "application/json",
         "accept-encoding": "gzip, br"}
    h.update(headers or {})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8787") as c:
        return await c.post("/v1/messages?beta=true", content=json.dumps(body), headers=h)


def _rows(tmp_path):
    return ledger.read_rows(tmp_path / "proxy_calls.jsonl")


async def test_pass_through_forwards_auth_unchanged_and_relays_stream(tmp_path):
    up = Upstream()
    app = _app(tmp_path, up, steps=frozenset())
    body = _first_call()
    r = await _post(app, body)
    assert r.status_code == 200
    assert r.content == _sse_reply()  # relayed byte-for-byte
    sent = up.requests[0]
    assert sent.headers["authorization"] == f"Bearer {TOKEN}"
    assert sent.headers["anthropic-beta"] == "oauth-2025-04-20,claude-code-20250219"
    assert sent.headers["accept-encoding"] == "identity"
    assert str(sent.url) == "http://127.0.0.1:9/v1/messages?beta=true"
    assert json.loads(sent.content) == body
    (row,) = _rows(tmp_path)
    assert row["decision"] == "forwarded" and row["reason"] == "routing_off"
    assert row["msg_id"] == "msg_upstream01" and row["session_id"] == SID and row["auth"] == "oauth"
    assert row["usage"]["cache_read_input_tokens"] == 43_500
    assert row["usage"]["cache_creation_1h"] == 800 and row["usage"]["cache_creation_5m"] == 0
    assert row["usage"]["output_tokens"] == 42


async def test_other_paths_pass_through_without_a_ledger_row(tmp_path):
    up = Upstream([(200, b'{"ok": true}')])
    app = _app(tmp_path, up)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8787") as c:
        r = await c.head("/api/hello", headers={"authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200 and up.requests[0].method == "HEAD"
    assert _rows(tmp_path) == []


async def test_continuation_is_served_as_sse_without_calling_anthropic(tmp_path, policy):
    up = Upstream()
    backend = FakeBackend(_ollama("Running it.", [_call("Bash", {"command": "python3 tests/test_mod01.py"})]))
    app = _app(tmp_path, up, backend)
    r = await _post(app, _req())
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    msg = _replay_sse(r.content)
    assert msg["content"][1]["name"] == "Bash" and msg["stop_reason"] == "tool_use"
    assert up.requests == []
    (row,) = _rows(tmp_path)
    assert row["decision"] == "served" and row["msg_id"] == msg["id"]
    assert row["model"] == "ollama/fake:1" and row["served_blocks"] == ["text", "tool_use:Bash"]
    assert row["step_class"] == "continuation" and row["added_latency_s"] == 0.0


async def test_non_streaming_request_gets_json(tmp_path, policy):
    backend = FakeBackend(_ollama("Done."))
    r = await _post(_app(tmp_path, Upstream(), backend), dict(_req(), stream=False))
    assert r.json()["content"] == [{"type": "text", "text": "Done."}]


async def test_default_trim_cuts_tool_descriptions_for_the_backend_only(tmp_path, policy):
    backend = FakeBackend(_ollama("Done."))
    body = _req()
    body["tools"][0]["description"] = "x" * 5000
    await _post(_app(tmp_path, Upstream(), backend), body)
    assert len(backend.calls[0]["tools"][0]["description"]) == 2000


async def test_validation_failure_falls_back_to_anthropic(tmp_path, policy):
    up = Upstream()
    backend = FakeBackend(_ollama("", [_call("Read", {})]))
    r = await _post(_app(tmp_path, up, backend), _req())
    assert r.content == _sse_reply()
    assert json.loads(up.requests[0].content) == _req()  # the ORIGINAL request, untrimmed
    (row,) = _rows(tmp_path)
    assert row["decision"] == "fallback" and row["reason"] == "validation"
    assert row["detail"] == "Read: missing required 'file_path'"
    assert row["msg_id"] == "msg_upstream01"


async def test_budget_exceeded_falls_back_and_records_added_latency(tmp_path, policy):
    up = Upstream()
    backend = FakeBackend(_ollama("late"), delay=1.0)
    await _post(_app(tmp_path, up, backend, step_budget_s=0.05), _req())
    assert len(up.requests) == 1
    (row,) = _rows(tmp_path)
    assert row["reason"] == "budget_exceeded"
    assert 0.04 <= row["added_latency_s"] < 1.0


async def test_backend_error_falls_back(tmp_path, policy):
    up = Upstream()
    await _post(_app(tmp_path, up, FakeBackend(exc=RuntimeError("ollama down"))), _req())
    (row,) = _rows(tmp_path)
    assert row["decision"] == "fallback" and row["reason"] == "backend_error"
    assert "ollama down" in row["detail"] and len(up.requests) == 1


async def test_policy_keeping_the_step_on_claude_forwards(tmp_path, policy):
    policy["model"] = None
    up = Upstream()
    backend = FakeBackend(_ollama("never"))
    await _post(_app(tmp_path, up, backend), _req())
    assert backend.calls == [] and len(up.requests) == 1
    (row,) = _rows(tmp_path)
    assert row["decision"] == "forwarded" and row["reason"] == "policy_kept"


async def test_pin_never_overrides_a_policy_keep(monkeypatch):
    async def _chain(text):
        return "code", "complex", ["codex/gpt-5.5", "anthropic/claude-sonnet-5"]

    monkeypatch.setattr(pb, "policy_chain", _chain)
    assert (await pb.choose_model("x", "ollama/pinned:1"))["model"] is None

    async def _chain2(text):
        return "code", "moderate", ["ollama/qwen3-coder:30b"]

    monkeypatch.setattr(pb, "policy_chain", _chain2)
    assert (await pb.choose_model("x", "ollama/pinned:1"))["model"] == "ollama/pinned:1"
    assert (await pb.choose_model("x", None))["model"] == "ollama/qwen3-coder:30b"


async def test_mixed_history_thinking_rejection_retries_once_without_thinking(tmp_path):
    rejection = json.dumps({"type": "error", "error": {"type": "invalid_request_error", "message":
                            "messages.3.content.0: Expected `thinking` or `redacted_thinking`"}}).encode()
    up = Upstream([(400, rejection), (200, _sse_reply())])
    body = _req()
    served, _ = from_ollama(_ollama("", [_call("Bash", {"command": "ls"})]), body)
    body["messages"].insert(-1, {"role": "assistant", "content": served["content"]})
    body["messages"].insert(-1, {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": served["content"][0]["id"], "content": "a.py"}]})
    r = await _post(_app(tmp_path, up, steps=frozenset()), body)
    assert r.status_code == 200 and len(up.requests) == 2
    retried = json.loads(up.requests[1].content)
    assert "thinking" not in retried and "context_management" not in retried
    assert retried["messages"] == body["messages"]
    (row,) = _rows(tmp_path)
    assert row["mixed_history"] is True and row["thinking_retry"] is True


async def test_other_upstream_errors_are_relayed_not_retried(tmp_path):
    up = Upstream([(400, b'{"type":"error","error":{"type":"invalid_request_error","message":"bad"}}')])
    r = await _post(_app(tmp_path, up, steps=frozenset()), _req())
    assert r.status_code == 400 and len(up.requests) == 1


def test_upstream_must_be_anthropic_or_loopback():
    assert ps.validate_upstream("https://api.anthropic.com/") == "https://api.anthropic.com"
    assert ps.validate_upstream("http://127.0.0.1:9999") == "http://127.0.0.1:9999"
    for bad in ("https://evil.example", "http://api.anthropic.com", "https://api.anthropic.com.evil.io"):
        with pytest.raises(ValueError):
            ps.validate_upstream(bad)


async def test_cross_origin_browser_request_is_refused(tmp_path):
    up = Upstream()
    r = await _post(_app(tmp_path, up), _first_call(), headers={"origin": "https://evil.example"})
    assert r.status_code == 403 and up.requests == []


def test_parse_steps():
    assert ps.parse_steps("off") == frozenset()
    assert ps.parse_steps("continuation") == frozenset({"continuation"})
    with pytest.raises(ValueError):
        ps.parse_steps("everything")


def test_unknown_trim_fails_at_startup():
    with pytest.raises(ValueError):
        pb.resolve_trims("nope")
    assert len(pb.resolve_trims("none,unused-tools")) == 2


# ── redaction ───────────────────────────────────────────────────────────────


async def test_auth_token_never_reaches_ledger_or_logs(tmp_path, policy, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    # Every row-writing path: served, validation fallback, backend error that
    # echoes the token, upstream error, unreachable upstream.
    cases = [
        (FakeBackend(_ollama("ok")), Upstream()),
        (FakeBackend(_ollama("", [_call("Read", {})])), Upstream()),
        (FakeBackend(exc=RuntimeError(f"auth failed for Bearer {TOKEN}")), Upstream()),
        (FakeBackend(exc=RuntimeError("x")), Upstream([(401, json.dumps({"error": TOKEN}).encode())])),
    ]
    for backend, up in cases:
        await _post(_app(tmp_path, up, backend), _req())

    def _boom(request):
        raise httpx.ConnectError(f"cannot reach with {TOKEN}")

    await _post(_app(tmp_path, _boom, steps=frozenset()), _req(), headers={"x-api-key": TOKEN})

    text = (tmp_path / "proxy_calls.jsonl").read_text()
    assert len(text.splitlines()) == 5  # anti-vacuity: every case wrote a row
    assert "[REDACTED" in text  # the echoed token was seen and scrubbed
    out = capsys.readouterr()
    for haystack in (text, caplog.text, out.out, out.err):
        assert TOKEN not in haystack
        assert TOKEN[:24] not in haystack


# ── ledger metrics ──────────────────────────────────────────────────────────


def test_stats_keeps_share_fallbacks_latency_and_cost_separate():
    u = {"input_tokens": 2, "output_tokens": 100, "cache_read_input_tokens": 40_000,
         "cache_creation_input_tokens": 1000, "cache_creation": {"ephemeral_1h_input_tokens": 1000}}
    rows = [
        {"decision": "forwarded", "reason": "not_eligible", "step_class": None, "session_id": "s",
         "requested_model": "claude-sonnet-5", "usage": u, "upstream_latency_s": 2.0},
        {"decision": "served", "step_class": "continuation", "session_id": "s", "route_latency_s": 8.0,
         "requested_model": "claude-sonnet-5"},
        {"decision": "fallback", "reason": "budget_exceeded", "step_class": "continuation", "session_id": "s",
         "requested_model": "claude-sonnet-5", "usage": u, "upstream_latency_s": 3.0,
         "added_latency_s": 30.0, "mixed_history": True},
        {"decision": "forwarded", "reason": "policy_kept", "step_class": "continuation", "session_id": "s",
         "requested_model": "claude-sonnet-5", "usage": u, "upstream_latency_s": 2.5},
    ]
    s = ledger.stats(rows)
    assert s["routed_share"] == {"served": 1, "calls": 4, "share": 0.25}
    assert s["fallbacks"]["n"] == 1 and s["fallbacks"]["attempted"] == 2
    assert s["fallbacks"]["not_served_by_reason"] == {"budget_exceeded": 1, "not_eligible": 1, "policy_kept": 1}
    assert s["latency"]["added_total_s"] == 30.0 and s["latency"]["served_median_s"] == 8.0
    an = s["anthropic"]
    assert an["calls"] == 3 and an["tokens"]["cache_read_input_tokens"] == 120_000
    assert an["tokens"]["cache_creation_1h"] == 3000 and an["tokens"]["cache_creation_5m"] == 0
    assert an["unpriced_calls"] == 0 and an["est_cost_usd"] > 0
    assert an["est_avoided_n"] == 1
    assert an["after_served_n"] == 1 and an["clean_n"] == 1
    assert "routed share: 25.0%" in ledger.format_stats(s)


def test_unpriced_model_is_counted_not_zeroed():
    row = {"decision": "forwarded", "requested_model": "no-such-model", "usage": {"input_tokens": 5}}
    assert ledger.anthropic_cost(row) is None
    assert ledger.stats([row])["anthropic"]["unpriced_calls"] == 1


def test_cli_stats_reads_the_state_ledger(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    ledger.write_row({"ts": 1e12, "decision": "served", "step_class": "continuation"})
    assert ps.cmd_proxy(["stats", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["routed_share"]["served"] == 1


# ── northstar: proxy-served turns count as routed claude_main_call units ─────


def _transcript(tmp_path, records):
    from datetime import datetime, timezone

    proj = tmp_path / "claude_projects" / "-Users-x-proj"
    proj.mkdir(parents=True)
    with (proj / f"{SID}.jsonl").open("w") as fh:
        for i, (kind, content, msg_id) in enumerate(records):
            ts = datetime.fromtimestamp(1_800_000_000 + i, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            message = {"role": kind, "content": content}
            if msg_id:
                message["id"] = msg_id
            fh.write(json.dumps({"type": kind, "timestamp": ts, "sessionId": SID, "uuid": str(i),
                                 "isSidechain": False, "message": message}) + "\n")
    return proj.parent


def _northstar_units(tmp_path, monkeypatch, records, served_ids):
    from llm_router import northstar as ns

    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "home"))
    for mid in served_ids:
        ledger.write_row({"ts": 1_800_000_000, "decision": "served", "msg_id": mid, "session_id": SID,
                          "task_type": "code", "model": "ollama/qwen3-coder:30b"})
    ledger.write_row({"ts": 1_800_000_000, "decision": "forwarded", "msg_id": "msg_claude1", "session_id": SID})
    root = _transcript(tmp_path, records)
    return list(ns.units(days=None, session_id=SID, root=root)), ns.report(days=None, session_id=SID, root=root)


def _tu(i):
    return {"type": "tool_use", "id": i, "name": "Bash", "input": {"command": "ls"}}


def _tr(i, text="ok", err=False):
    return {"type": "tool_result", "tool_use_id": i, "content": text, "is_error": err}


def test_served_turn_with_ok_tool_result_is_routed_and_used(tmp_path, monkeypatch):
    units, report = _northstar_units(tmp_path, monkeypatch, [
        ("user", "Fix the bug in pkg/mod01.py please", None),
        ("assistant", [_tu("toolu_c1")], "msg_claude1"),
        ("user", [_tr("toolu_c1")], None),
        ("assistant", [{"type": "text", "text": "Running tests."}], "msg_lrA"),
        ("assistant", [_tu("toolu_lrA")], "msg_lrA"),  # one message, two records
        ("user", [_tr("toolu_lrA")], None),
    ], ["msg_lrA"])
    main = [u for u in units if u["kind"] == "claude_main_call"]
    assert len(main) == 3  # denominator unchanged: one unit per assistant record
    proxy = [u for u in main if u["lever"] == "proxy"]
    assert len(proxy) == 1  # one served call = one routed unit, however many records
    assert proxy[0]["outcome"] == "used" and proxy[0]["signal"] == "proxy_tool_result_ok"
    assert proxy[0]["model"] == "ollama/qwen3-coder:30b" and proxy[0]["task_type"] == "code"
    claude = [u for u in main if u["lever"] == "none"]
    assert len(claude) == 2 and all(u["outcome"] == "not_routed" for u in claude)
    kind = report["by_kind"]["claude_main_call"]
    assert (kind["units"], kind["attempted"], kind["used"]) == (3, 1, 1)


@pytest.mark.parametrize("result,outcome,signal", [
    (_tr("toolu_lrB", "boom", err=True), "redo", "proxy_tool_error"),
    (_tr("toolu_lrB", "The user doesn't want to proceed with this tool use."), "redo", "proxy_tool_rejected"),
    (None, "unknown", "proxy_tool_result_missing"),
])
def test_served_turn_outcomes(tmp_path, monkeypatch, result, outcome, signal):
    records = [("user", "Fix it", None), ("assistant", [_tu("toolu_lrB")], "msg_lrB")]
    if result:
        records.append(("user", [result], None))
    units, _ = _northstar_units(tmp_path, monkeypatch, records, ["msg_lrB"])
    (u,) = [u for u in units if u["lever"] == "proxy"]
    assert (u["outcome"], u["signal"]) == (outcome, signal)


def test_text_turn_followed_by_explicit_claude_redo(tmp_path, monkeypatch):
    units, _ = _northstar_units(tmp_path, monkeypatch, [
        ("user", "Summarise the test output", None),
        ("assistant", [{"type": "text", "text": "All tests pass."}], "msg_lrC"),
        ("user", "claude: that is wrong, run them again", None),
    ], ["msg_lrC"])
    (u,) = [u for u in units if u["lever"] == "proxy"]
    assert (u["outcome"], u["signal"]) == ("redo", "proxy_explicit_claude_redo")


def test_bad_env_setting_exits_2_without_traceback(monkeypatch, capsys):
    monkeypatch.setenv("LLM_ROUTER_PROXY_STEPS", "everything")
    assert ps.cmd_proxy([]) == 2
    assert "bad LLM_ROUTER_PROXY_* setting" in capsys.readouterr().err


def test_upstream_env_pointing_off_machine_is_refused_at_startup(monkeypatch, capsys):
    monkeypatch.setenv("LLM_ROUTER_PROXY_UPSTREAM", "https://collector.example/v1")
    assert ps.cmd_proxy(["--port", "0"]) == 2
    assert "refusing upstream" in capsys.readouterr().err
