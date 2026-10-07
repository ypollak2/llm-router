"""Off-mode golden: ``--serve off`` (the default) is byte-identical to the
proxy as it was before the local-agent serve mode existed.

The golden file (``tests/fixtures/proxy/golden_off.json``) was recorded from
``origin/main`` at 47e49e1, BEFORE any local-agent code was written. Each case
drives the real app (real ``OllamaBackend``, real translation, real ledger)
against one mocked transport that plays both Anthropic and Ollama, and records:

  * every request the proxy sent to Anthropic (method, URL, headers, body bytes);
  * every request the proxy sent to Ollama (URL and body bytes);
  * every response the client got (status, content type, body bytes);
  * every ledger row, minus wall-clock fields;
  * the startup banner ``llm-router proxy`` prints.

Proxy-generated ids (``toolu_lr…``, ``msg_lr…``) are random per run and are
replaced by their order of first appearance; nothing else is normalised. Any
other change to what off mode sends, returns or records fails this test.

Regenerate ONLY when an off-mode change is intended and reviewed:
``PROXY_GOLDEN_UPDATE=1 uv run pytest tests/test_proxy_off_golden.py``.
No test here makes a network call.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import dataclasses
import io
import json
import os
import re
from pathlib import Path

import httpx
import pytest

from llm_router.proxy import backends as pb
from llm_router.proxy import ledger
from llm_router.proxy import server as ps

FIXTURES = Path(__file__).parent / "fixtures" / "proxy"
GOLDEN = FIXTURES / "golden_off.json"
UPDATE = os.environ.get("PROXY_GOLDEN_UPDATE") == "1"

UPSTREAM = "http://127.0.0.1:9"
OLLAMA = "http://127.0.0.1:11999"
TOKEN = "sk-ant-oat01-" + "Gx7" * 20  # fake, OAuth-shaped
CLIENT_HEADERS = {"authorization": f"Bearer {TOKEN}", "anthropic-version": "2023-06-01",
                  "anthropic-beta": "oauth-2025-04-20,claude-code-20250219",
                  "content-type": "application/json", "accept-encoding": "gzip, br"}

# Wall-clock fields: the only ledger values a correct run may change.
# tier_phases_ms (P0.9-e) is per-phase wall time, volatile like tier_decision_s.
_VOLATILE = {"ts", "route_latency_s", "upstream_latency_s", "added_latency_s", "tier_decision_s",
             "tier_phases_ms"}
_VOLATILE_USAGE = {"first_token_s"}
_ID_RE = re.compile(r"(toolu_lr|msg_lr)[0-9a-f]{22}")


def _req() -> dict:
    return json.loads((FIXTURES / "continuation_request.json").read_text())


def _first_call() -> dict:
    body = _req()
    body["messages"] = body["messages"][:2]
    return body


def _sse(msg_id="msg_upstream01", text="ok"):
    usage = {"input_tokens": 3, "cache_read_input_tokens": 43_500, "cache_creation_input_tokens": 800,
             "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 800},
             "output_tokens": 1}
    events = [
        ("message_start", {"type": "message_start", "message": {
            "id": msg_id, "type": "message", "role": "assistant", "model": "claude-sonnet-5",
            "content": [], "stop_reason": None, "usage": usage}}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": text}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                           "usage": {"output_tokens": 42}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    return "".join(f"event: {n}\ndata: {json.dumps(d)}\n\n" for n, d in events).encode()


def _call(name, args):
    return {"function": {"name": name, "arguments": args}}


def _ollama_chunks(content="", calls=None, done_reason="stop"):
    return [
        {"message": {"role": "assistant", "content": content}, "done": False},
        {"message": {"role": "assistant", "content": "", "tool_calls": calls or []}, "done": False},
        {"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": done_reason,
         "prompt_eval_count": 3100, "eval_count": 24, "prompt_eval_duration": 1_200_000_000,
         "eval_duration": 400_000_000, "load_duration": 10_000_000},
    ]


class World:
    """One mock transport: requests to OLLAMA go to the scripted Ollama, the
    rest to the scripted Anthropic. Both record exactly what they received."""

    def __init__(self, upstream=None, ollama=None):
        self.upstream = list(upstream or [])
        self.ollama = list(ollama or [])
        self.sent: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if f"{request.url.scheme}://{request.url.host}:{request.url.port}" == OLLAMA:
            self.sent.append({"to": "ollama", "method": request.method, "url": str(request.url),
                              "body": request.content.decode()})
            reply = self.ollama.pop(0) if self.ollama else _ollama_chunks("Done.")
            if isinstance(reply, tuple):
                status, text = reply
                return httpx.Response(status, content=text.encode())
            body = "".join(json.dumps(c) + "\n" for c in reply).encode()
            return httpx.Response(200, stream=httpx.ByteStream(body),
                                  headers={"content-type": "application/x-ndjson"})
        headers = sorted((k, v) for k, v in request.headers.items() if k.lower() != "user-agent")
        self.sent.append({"to": "anthropic", "method": request.method, "url": str(request.url),
                          "headers": headers, "body": request.content.decode()})
        reply = self.upstream.pop(0) if self.upstream else (200, _sse())
        if reply == "connect_error":
            raise httpx.ConnectError("refused", request=request)
        status, body = reply
        ctype = "text/event-stream" if body.startswith(b"event:") else "application/json"
        return httpx.Response(status, stream=httpx.ByteStream(body),
                              headers={"content-type": ctype, "request-id": "req_golden"})


def _with_image_in_newest_result(body):
    body = copy.deepcopy(body)
    tr = [m for m in body["messages"] if m["role"] == "user"][-1]["content"][0]
    tr["content"] = [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA"}}]
    return body


def _with_image_in_first_turn(body):
    body = copy.deepcopy(body)
    first = body["messages"][0]
    if isinstance(first["content"], str):
        first["content"] = [{"type": "text", "text": first["content"]}]
    first["content"].append({"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                         "data": "AA"}})
    return body


def _two_turn_tool_result(body):
    """A newest tool_result turn, but only two turns in all: not a continuation."""
    body = copy.deepcopy(body)
    turns = [m for m in body["messages"] if m["role"] != "system"]
    body["messages"] = [turns[1], turns[2]]
    return body


def _with_served_history(body):
    body = copy.deepcopy(body)
    for m in body["messages"]:
        for b in m["content"] if isinstance(m["content"], list) else []:
            if b.get("type") == "tool_use":
                b["id"] = "toolu_lr" + "0" * 22
            if b.get("type") == "tool_result":
                b["tool_use_id"] = "toolu_lr" + "0" * 22
    return body


_THINKING_400 = (400, json.dumps({"type": "error", "error": {
    "type": "invalid_request_error", "message": "messages.1.content.0: thinking blocks cannot be modified"}}).encode())

CHAIN_LOCAL = ("code", "moderate", ["ollama/qwen-golden:1", "anthropic/claude-sonnet-5"])
CHAIN_CLAUDE = ("code", "complex", ["anthropic/claude-sonnet-5"])

# name -> (config overrides, chain, upstream script, ollama script, requests)
# A request is (method, path, body-or-None).
CASES: dict[str, tuple] = {
    "first_call_forwarded": ({}, CHAIN_LOCAL, [], [], [("POST", "/v1/messages?beta=true", _first_call())]),
    "continuation_served_sse": ({}, CHAIN_LOCAL, [], [_ollama_chunks("Running it.", [_call("Bash", {"command": "ls"})])],
                                [("POST", "/v1/messages?beta=true", _req())]),
    "continuation_served_json": ({}, CHAIN_LOCAL, [], [_ollama_chunks("Done.")],
                                 [("POST", "/v1/messages", dict(_req(), stream=False))]),
    "continuation_unknown_tool_fallback": ({}, CHAIN_LOCAL, [], [_ollama_chunks("", [_call("Nope", {})])],
                                           [("POST", "/v1/messages?beta=true", _req())]),
    "continuation_edit_reply_hard_rule": (
        {}, CHAIN_LOCAL, [],
        [_ollama_chunks("", [_call("Edit", {"file_path": "/work/repo/a.py", "old_string": "a", "new_string": "b"})])],
        [("POST", "/v1/messages?beta=true", _req())]),
    "continuation_length_truncated": ({}, CHAIN_LOCAL, [], [_ollama_chunks("partial", done_reason="length")],
                                      [("POST", "/v1/messages?beta=true", _req())]),
    "continuation_ollama_crash": ({}, CHAIN_LOCAL, [], [(500, '{"error":"llama runner process has terminated"}')],
                                  [("POST", "/v1/messages?beta=true", _req())]),
    "continuation_context_overflow": ({"num_ctx": 1024}, CHAIN_LOCAL, [], [],
                                      [("POST", "/v1/messages?beta=true", _req())]),
    "policy_kept": ({}, CHAIN_CLAUDE, [], [], [("POST", "/v1/messages?beta=true", _req())]),
    "newest_turn_image_not_eligible": ({}, CHAIN_LOCAL, [], [],
                                       [("POST", "/v1/messages?beta=true", _with_image_in_newest_result(_req()))]),
    "older_turn_image_placeholder_served": ({}, CHAIN_LOCAL, [], [_ollama_chunks("Done.")],
                                            [("POST", "/v1/messages?beta=true", _with_image_in_first_turn(_req()))]),
    "two_turn_tool_result_not_a_continuation": (
        {}, CHAIN_LOCAL, [], [], [("POST", "/v1/messages?beta=true", _two_turn_tool_result(_req()))]),
    "forced_tool_choice_not_eligible": ({}, CHAIN_LOCAL, [], [],
                                        [("POST", "/v1/messages?beta=true", dict(_req(), tool_choice={"type": "any"}))]),
    "no_tools_side_call": ({}, CHAIN_LOCAL, [], [], [("POST", "/v1/messages?beta=true", dict(_req(), tools=[]))]),
    "steps_off_routing_off": ({"steps": frozenset()}, CHAIN_LOCAL, [], [],
                              [("POST", "/v1/messages?beta=true", _req())]),
    "mixed_history_thinking_retry": ({"steps": frozenset()}, CHAIN_LOCAL, [_THINKING_400, (200, _sse())], [],
                                     [("POST", "/v1/messages?beta=true", _with_served_history(_req()))]),
    "upstream_unreachable": ({"steps": frozenset()}, CHAIN_LOCAL, ["connect_error"], [],
                             [("POST", "/v1/messages?beta=true", _first_call())]),
    "loop_guard_repeat": ({}, CHAIN_LOCAL, [],
                          [_ollama_chunks("", [_call("Bash", {"command": "ls"})]),
                           _ollama_chunks("", [_call("Bash", {"command": "ls"})])],
                          [("POST", "/v1/messages?beta=true", _req()), ("POST", "/v1/messages?beta=true", _req())]),
    "other_paths_no_row": ({}, CHAIN_LOCAL, [(200, b'{"data": []}'), (200, b'{"input_tokens": 12}')], [],
                           [("GET", "/v1/models", None),
                            ("POST", "/v1/messages/count_tokens?beta=true", _first_call())]),
}


def _clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("LLM_ROUTER_PROXY_") or key.startswith("LLM_ROUTER_LOCAL_AGENT"):
            monkeypatch.delenv(key, raising=False)


class _Ids:
    def __init__(self):
        self.seen: dict[str, str] = {}

    def __call__(self, text: str) -> str:
        def sub(m):
            return self.seen.setdefault(m.group(0), f"{m.group(1)}<{len(self.seen) + 1}>")
        return _ID_RE.sub(sub, text)


def _clean_row(row: dict) -> dict:
    out = {k: v for k, v in row.items() if k not in _VOLATILE}
    if isinstance(out.get("backend_usage"), dict):
        out["backend_usage"] = {k: v for k, v in out["backend_usage"].items() if k not in _VOLATILE_USAGE}
    return out


async def _run_case(tmp_path, monkeypatch, name, serve_env):
    overrides, chain, upstream, ollama, requests = CASES[name]
    _clean_env(monkeypatch)
    if serve_env is not None:
        monkeypatch.setenv("LLM_ROUTER_PROXY_LOCAL_AGENT_MODE", serve_env)

    async def _chain(text):
        return chain

    monkeypatch.setattr(pb, "policy_chain", _chain)
    world = World(upstream, ollama)
    ledger_path = tmp_path / f"{name}.jsonl"
    cfg = dataclasses.replace(ps.ProxyConfig.from_env(), upstream=UPSTREAM, ollama_url=OLLAMA,
                              ledger_path=ledger_path, warm_up=False, **overrides)
    app = ps.build_app(cfg, client=httpx.AsyncClient(transport=httpx.MockTransport(world)))
    responses = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8787") as c:
        for method, path, body in requests:
            kwargs = {"headers": CLIENT_HEADERS}
            if body is not None:
                kwargs["content"] = json.dumps(body)
            r = await c.request(method, path, **kwargs)
            responses.append({"status": r.status_code, "content_type": r.headers.get("content-type"),
                              "body": r.content.decode()})
    await asyncio.sleep(0)
    ids = _Ids()
    record = {
        "sent": json.loads(ids(json.dumps(world.sent))),
        "responses": json.loads(ids(json.dumps(responses))),
        "ledger": json.loads(ids(json.dumps([_clean_row(r) for r in ledger.read_rows(ledger_path)]))),
    }
    return record


def _banner(monkeypatch, serve_env) -> str:
    _clean_env(monkeypatch)
    if serve_env is not None:
        monkeypatch.setenv("LLM_ROUTER_PROXY_LOCAL_AGENT_MODE", serve_env)
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = ps.cmd_proxy(["--port", "18000", "--no-warm-up"])
    return f"rc={rc}\n" + buf.getvalue()


def _load() -> dict:
    return json.loads(GOLDEN.read_text()) if GOLDEN.exists() else {}


def _save(name: str, record) -> None:
    data = _load()
    data[name] = record
    GOLDEN.write_text(json.dumps(dict(sorted(data.items())), indent=1, sort_keys=True) + "\n")


# serve_env: unset (the default) and an explicit "off" must both be today's behaviour.
@pytest.mark.parametrize("serve_env", [None, "off"])
@pytest.mark.parametrize("name", sorted(CASES))
async def test_off_mode_matches_golden(tmp_path, monkeypatch, name, serve_env):
    record = await _run_case(tmp_path, monkeypatch, name, serve_env)
    if UPDATE and serve_env is None:
        _save(name, record)
        return
    golden = _load()
    assert name in golden, f"no golden for {name}; regenerate with PROXY_GOLDEN_UPDATE=1 (see module doc)"
    assert record == golden[name]


@pytest.mark.parametrize("serve_env", [None, "off"])
def test_off_mode_banner_matches_golden(monkeypatch, serve_env):
    banner = _banner(monkeypatch, serve_env)
    if UPDATE and serve_env is None:
        _save("_banner", banner)
        return
    assert banner == _load()["_banner"]


def test_golden_covers_every_case_and_is_not_empty():
    golden = _load()
    assert set(CASES) | {"_banner"} == set(golden)
    # Not vacuous: every case recorded at least one exchange, and the served
    # cases really reached Ollama.
    for name in CASES:
        assert golden[name]["responses"], name
    assert any(s["to"] == "ollama" for s in golden["continuation_served_sse"]["sent"])
    assert any(s["to"] == "anthropic" for s in golden["first_call_forwarded"]["sent"])
    served = golden["continuation_served_sse"]["ledger"][0]
    assert served["decision"] == "served" and served["msg_id"] == "msg_lr<1>"
