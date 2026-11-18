"""Permission-check capture (D-532): off by default, never alters traffic, captures exactly the D-525 bucket.

A fake upstream (httpx.MockTransport) plays Anthropic; no network and no model. The synthetic request
text below is invented for this test; no captured data is read and none is printed.
"""

from __future__ import annotations

import dataclasses
import json
import os
import stat

import httpx
import pytest

from llm_router.proxy import ledger
from llm_router.proxy import permcheck_capture as pc
from llm_router.proxy import server as ps

UPSTREAM = "http://127.0.0.1:9"
HEADERS = {"authorization": "Bearer sk-ant-oat01-" + "Gx7" * 20, "anthropic-version": "2023-06-01",
           "content-type": "application/json"}
REPLY = {"id": "msg_up01", "type": "message", "role": "assistant", "model": "claude-test-1",
         "content": [{"type": "text", "text": "<block>no</block>"}], "stop_reason": "end_turn",
         "usage": {"input_tokens": 90, "output_tokens": 9, "cache_read_input_tokens": 46000}}
TOOL = {"name": "Bash", "description": "run", "input_schema": {"type": "object", "properties": {}}}


def side_call(**extra) -> dict:
    return {"model": "claude-test-1", "max_tokens": 64, "stream": False,
            "system": [{"type": "text", "text": "classifier instructions mentioning <block>"}],
            "messages": [{"role": "user", "content": "synthetic transcript number 1"}], **extra}


class Upstream:
    def __init__(self, reject_thinking: bool = False):
        self.sent: list[bytes] = []
        self.reject_thinking = reject_thinking

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(request.content)
        if self.reject_thinking and len(self.sent) == 1:
            return httpx.Response(400, content=b'{"error":{"message":"invalid thinking block"}}',
                                  headers={"content-type": "application/json"})
        return httpx.Response(200, stream=httpx.ByteStream(json.dumps(REPLY).encode()),
                              headers={"content-type": "application/json"})


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith("LLM_ROUTER_PROXY_") or key.startswith("LLM_ROUTER_LOCAL_AGENT"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv(pc.ENV, raising=False)
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "lr"))
    pc.reset_for_tests()


async def run(tmp_path, bodies: list[dict], up: Upstream | None = None):
    up = up or Upstream()
    cfg = dataclasses.replace(ps.ProxyConfig.from_env(), upstream=UPSTREAM, ledger_path=tmp_path / "ledger.jsonl",
                              warm_up=False)
    app = ps.build_app(cfg, client=httpx.AsyncClient(transport=httpx.MockTransport(up)))
    out = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as c:
        for body in bodies:
            r = await c.post("/v1/messages", headers=HEADERS, content=json.dumps(body))
            out.append((r.status_code, r.content))
            await pc.drain()  # the capture and its ledger row are written off the request path (S1)
    return up, out, ledger.read_rows(tmp_path / "ledger.jsonl")


def cap_dir(tmp_path):
    return tmp_path / "lr" / pc.DIR_NAME


def records(tmp_path) -> list[dict]:
    lines = []
    for f in sorted(cap_dir(tmp_path).glob("*.jsonl")):
        lines += [json.loads(x) for x in f.read_text().splitlines()]
    return lines


@pytest.mark.parametrize("value", [None, "", "0", "abc", "-3", "1.5"])
def test_flag_values_that_mean_off(monkeypatch, value):
    if value is not None:
        monkeypatch.setenv(pc.ENV, value)
    assert pc.limit() == 0


def test_flag_on_reads_n(monkeypatch):
    monkeypatch.setenv(pc.ENV, "300")
    assert pc.limit() == 300


async def test_off_creates_nothing_and_matches_on_traffic(tmp_path, monkeypatch):
    (tmp_path / "off").mkdir()
    up_off, out_off, rows_off = await run(tmp_path / "off", [side_call()])
    assert not cap_dir(tmp_path).exists()
    assert "permcheck_capture" not in rows_off[0]
    monkeypatch.setenv(pc.ENV, "5")
    (tmp_path / "on").mkdir()
    up_on, out_on, rows_on = await run(tmp_path / "on", [side_call()])
    # Same bytes upstream, same response to the client, same ledger row apart from the new enum.
    assert up_on.sent == up_off.sent
    assert out_on == out_off
    strip = lambda r: {k: v for k, v in r.items() if k not in {"ts", "upstream_latency_s", "added_latency_s",  # noqa: E731
                                                              "task_id", "trace_id", "route_latency_s",
                                                              "tier_decision_s", "tier_phases_ms", "permcheck_capture"}}
    assert strip(rows_on[0]) == strip(rows_off[0])
    assert rows_on[0]["permcheck_capture"] == "written"


async def test_on_captures_exactly_the_side_calls(tmp_path, monkeypatch):
    monkeypatch.setenv(pc.ENV, "10")
    main_thread = {"model": "claude-test-1", "max_tokens": 64, "stream": False, "tools": [TOOL],
                   "messages": [{"role": "user", "content": "synthetic human prompt"}]}
    streaming = side_call(stream=True)
    server_tool = side_call(tools=[{"type": "web_search_20250305", "name": "web_search"}])
    bodies = [side_call(), main_thread, streaming, server_tool, side_call()]
    up, out, rows = await run(tmp_path, bodies)
    assert [s for s, _ in out] == [200] * 5
    assert [r.get("permcheck_capture") for r in rows] == ["written", None, None, None, "written"]
    recs = records(tmp_path)
    assert len(recs) == 2
    first = recs[0]
    assert first["request_body"].encode() == up.sent[0]          # exactly the bytes forwarded upstream
    assert first["reply_content"] == REPLY["content"] and first["upstream_model"] == "claude-test-1"
    assert first["usage"]["cache_read_input_tokens"] == 46000
    assert isinstance(first["upstream_latency_ms"], int) and first["conversation"] and first["ts"]


async def test_permissions_and_symlink_refusal(tmp_path, monkeypatch):
    monkeypatch.setenv(pc.ENV, "10")
    old = os.umask(0)  # a permissive umask must not widen the modes
    try:
        _, out, rows = await run(tmp_path, [side_call()])
    finally:
        os.umask(old)
    assert rows[0]["permcheck_capture"] == "written"
    d = cap_dir(tmp_path)
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    assert [stat.S_IMODE(f.stat().st_mode) for f in d.glob("*.jsonl")] == [0o600]
    # a symlinked capture dir is refused; the request still succeeds
    other = tmp_path / "elsewhere"
    other.mkdir()
    link_home = tmp_path / "lr2"
    link_home.mkdir()
    (link_home / pc.DIR_NAME).symlink_to(other)
    monkeypatch.setenv("LLM_ROUTER_HOME", str(link_home))
    (tmp_path / "b").mkdir()
    _, out, rows = await run(tmp_path / "b", [side_call()])
    assert out[0][0] == 200 and rows[0]["permcheck_capture"] == "error"
    assert list(other.iterdir()) == []


async def test_dir_not_owner_only_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv(pc.ENV, "10")
    d = cap_dir(tmp_path)
    d.mkdir(parents=True)
    d.chmod(0o755)
    _, out, rows = await run(tmp_path, [side_call()])
    assert out[0][0] == 200 and rows[0]["permcheck_capture"] == "error"
    assert list(d.iterdir()) == []


async def test_count_cap(tmp_path, monkeypatch):
    monkeypatch.setenv(pc.ENV, "2")
    _, out, rows = await run(tmp_path, [side_call() for _ in range(3)])
    assert [r["permcheck_capture"] for r in rows] == ["written", "written", "cap"]
    assert len(records(tmp_path)) == 2


async def test_size_cap(tmp_path, monkeypatch):
    monkeypatch.setenv(pc.ENV, "10")
    monkeypatch.setattr(pc, "MAX_DIR_BYTES", 10)
    _, out, rows = await run(tmp_path, [side_call()])
    assert out[0][0] == 200 and rows[0]["permcheck_capture"] == "cap"
    assert records(tmp_path) == []


async def test_write_error_never_fails_the_request(tmp_path, monkeypatch):
    monkeypatch.setenv(pc.ENV, "10")

    def boom():
        raise OSError("disk full")

    monkeypatch.setattr(pc, "_capture_dir", boom)
    up, out, rows = await run(tmp_path, [side_call()])
    assert out[0] == (200, json.dumps(REPLY).encode())
    assert rows[0]["permcheck_capture"] == "error"
    assert "disk full" not in json.dumps(rows[0])          # an enum, never text


async def test_spawn_failure_still_writes_the_row(tmp_path, monkeypatch):
    """Review S-B: if the background task cannot be created (loop closing), the row is written now with 'error'."""
    monkeypatch.setenv(pc.ENV, "10")

    def no_loop(coro):
        raise RuntimeError("event loop is closing")

    monkeypatch.setattr(pc, "spawn", no_loop)
    up, out, rows = await run(tmp_path, [side_call()])
    assert out[0] == (200, json.dumps(REPLY).encode())
    assert len(rows) == 1 and rows[0]["permcheck_capture"] == "error"
    assert not cap_dir(tmp_path).exists()


async def test_shutdown_drains_pending_captures(tmp_path, monkeypatch):
    """Review S-A: a graceful shutdown (lifespan exit) waits for pending capture tasks."""
    import asyncio

    cfg = dataclasses.replace(ps.ProxyConfig.from_env(), upstream=UPSTREAM, ledger_path=tmp_path / "ledger.jsonl",
                              warm_up=False)
    app = ps.build_app(cfg, client=httpx.AsyncClient(transport=httpx.MockTransport(Upstream())))
    done = []

    async def slow_capture():
        await asyncio.sleep(0.2)
        done.append(1)

    async with app.router.lifespan_context(app):
        pc.spawn(slow_capture())
        assert done == []
    assert done == [1]


async def test_shutdown_drain_error_is_recorded_not_raised(tmp_path, monkeypatch):
    """Review 2026-11-18 SHOULD 1: a drain failure goes to failopen; lifespan exit must not raise (UnboundLocalError)."""
    cfg = dataclasses.replace(ps.ProxyConfig.from_env(), upstream=UPSTREAM, ledger_path=tmp_path / "ledger.jsonl",
                              warm_up=False)
    app = ps.build_app(cfg, client=httpx.AsyncClient(transport=httpx.MockTransport(Upstream())))
    recorded = []

    async def broken_drain():
        raise RuntimeError("drain broke")

    monkeypatch.setattr(pc, "drain", broken_drain)
    monkeypatch.setattr(ps.failopen, "record", lambda code, exc, **kw: recorded.append((code, kw.get("detail"))))
    async with app.router.lifespan_context(app):
        pass
    assert ("LR-FO-PROXY-PERMCHECK-CAPTURE", "drain") in recorded


async def test_thinking_retry_captures_the_retried_body(tmp_path, monkeypatch):
    monkeypatch.setenv(pc.ENV, "10")
    body = side_call(thinking={"type": "enabled", "budget_tokens": 1024})
    body["messages"] = [{"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_lr_synthetic1",
                                                           "name": "Bash", "input": {}}]},
                        {"role": "user", "content": "synthetic transcript number 2"}]
    up, out, rows = await run(tmp_path, [body], Upstream(reject_thinking=True))
    assert out[0][0] == 200 and rows[0]["thinking_retry"] is True and rows[0]["permcheck_capture"] == "written"
    assert len(up.sent) == 2 and up.sent[0] != up.sent[1] and b'"thinking"' not in up.sent[1]
    assert records(tmp_path)[0]["request_body"].encode() == up.sent[1]   # the final send, not the stale first body


async def test_client_disconnect_neither_captures_nor_errors(tmp_path, monkeypatch):
    monkeypatch.setenv(pc.ENV, "10")
    calls: list[str] = []
    monkeypatch.setattr(pc.failopen, "record", lambda *a, **k: calls.append("failopen"))
    real = pc.capture
    monkeypatch.setattr(pc, "capture", lambda *a, **k: calls.append("capture") or real(*a, **k))
    cfg = dataclasses.replace(ps.ProxyConfig.from_env(), upstream=UPSTREAM, ledger_path=tmp_path / "ledger.jsonl",
                              warm_up=False)
    up = Upstream()
    app = ps.build_app(cfg, client=httpx.AsyncClient(transport=httpx.MockTransport(up)))
    payload = json.dumps(side_call()).encode()
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http",
             "path": "/v1/messages", "raw_path": b"/v1/messages", "query_string": b"", "server": ("127.0.0.1", 80),
             "client": ("127.0.0.1", 1), "root_path": "",
             "headers": [(k.encode(), v.encode()) for k, v in HEADERS.items()] + [(b"content-length", str(len(payload)).encode())]}
    sent_body = 0

    import asyncio

    gone = asyncio.Event()
    first = True

    async def receive():
        nonlocal first
        if first:
            first = False
            return {"type": "http.request", "body": payload, "more_body": False}
        await gone.wait()                          # later reads: the disconnect, once the send failed
        return {"type": "http.disconnect"}

    async def send(message):
        nonlocal sent_body
        if message["type"] == "http.response.body":
            sent_body += 1
            gone.set()
            raise OSError("client went away")      # the socket died while the reply was being relayed

    try:
        await asyncio.wait_for(app(scope, receive, send), 10)
    except OSError:
        pass
    import gc

    for _ in range(5):                             # the abandoned relay generator is closed by the loop's finalizer
        gc.collect()
        await asyncio.sleep(0.05)
    await pc.drain()
    assert sent_body >= 1 and calls == [] and not cap_dir(tmp_path).exists()
    rows = ledger.read_rows(tmp_path / "ledger.jsonl")
    # review S-D: the relay finally DID run (the row exists, written the main way), and it carries no capture enum.
    assert len(rows) == 1 and rows[0].get("reason") == "not_eligible"
    assert "permcheck_capture" not in rows[0]
