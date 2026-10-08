"""P0.10 (D-17 = A): the fail-open shim in front of the main proxy.

Bug this closes (docs/BUGS.md P010-1): with proxy-default installed,
``~/.claude/settings.json`` points every Claude Code session at
127.0.0.1:8787. When the main proxy is down that port refuses connections and
every new session's first API call fails; nothing in the router can change the
base URL of a session that has already started.

The shim owns the port instead. It forwards bytes to the main proxy, and when
the main proxy cannot be reached (refused, connect timeout, disconnected before
a response) or answers 5xx before any byte was sent, it sends the same request
straight to Anthropic once and records a ``proxy_down`` fail-open event.

Every test runs real sockets on loopback: a fake main proxy, a fake "direct"
upstream that stands in for api.anthropic.com, and the shim between them. No
test reaches the network.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import socket

import aiohttp
import pytest
from aiohttp import web

from llm_router.proxy import failopen_shim as shim

pytestmark = pytest.mark.xdist_group("proxy_failopen_shim")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Upstream:
    """A loopback HTTP server that records every request it receives."""

    def __init__(self, handler):
        self.handler = handler
        self.hits: list[dict] = []
        self.runner: web.AppRunner | None = None
        self.port = 0

    async def __aenter__(self):
        async def _record(request: web.Request):
            body = await request.read()
            self.hits.append({
                "method": request.method, "path_qs": request.path_qs,
                "headers": dict(request.headers), "body": body,
            })
            return await self.handler(request, body)

        app = web.Application(client_max_size=64 * 1024 * 1024)
        app.router.add_route("*", "/{tail:.*}", _record)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        await self.runner.cleanup()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


class _Shim:
    def __init__(self, cfg: shim.ShimConfig):
        self.cfg = cfg
        self.runner: web.AppRunner | None = None
        self.port = 0

    async def __aenter__(self):
        self.runner = web.AppRunner(shim.build_app(self.cfg), access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        await self.runner.cleanup()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


async def _ok_json(request, body):
    return web.json_response({"served_by": request.app.get("name", "x"), "echo_len": len(body)})


def _named(name: str):
    async def handler(request, body):
        return web.json_response({"served_by": name, "echo_len": len(body)})
    return handler


def _status(code: int, name: str):
    async def handler(request, body):
        return web.json_response({"served_by": name, "status": code}, status=code)
    return handler


@pytest.fixture
def state(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "state"))
    from llm_router import failopen
    failopen.reset_cache()
    return tmp_path / "state"


def _failopen_rows(state_dir) -> list[dict]:
    p = state_dir / "fail_open.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


_BODY = json.dumps({"model": "claude-sonnet-5-5", "messages": [{"role": "user", "content": "x" * 3000}]}).encode()
_HEADERS = {"x-api-key": "sk-test-not-real", "anthropic-version": "2023-06-01",
            "content-type": "application/json"}


async def _post(url: str, *, raw: bool = False, body: bytes = _BODY):
    async with aiohttp.ClientSession(auto_decompress=not raw) as s:
        async with s.post(url + "/v1/messages?beta=true", data=body, headers=_HEADERS) as r:
            return r.status, dict(r.headers), await r.read()


def _cfg(main_port: int, direct_url: str, **kw) -> shim.ShimConfig:
    # A generous connect budget: under a loaded CI runner a loopback connect
    # can exceed the production 200 ms and send a test request direct.
    # test_connect_timeout_goes_direct covers the budget itself.
    kw.setdefault("connect_timeout_s", 5.0)
    return shim.ShimConfig(upstream_port=main_port, direct_url=direct_url, **kw)


# ── the happy path: main proxy up ────────────────────────────────────────────

def test_main_up_passes_through_with_method_path_body_and_auth(state):
    async def run():
        async with _Upstream(_named("main")) as main, _Upstream(_named("direct")) as direct:
            async with _Shim(_cfg(main.port, direct.url)) as s:
                status, _, body = await _post(s.url)
        return status, json.loads(body), main.hits, direct.hits

    status, data, main_hits, direct_hits = asyncio.run(run())
    assert status == 200 and data["served_by"] == "main"
    assert len(main_hits) == 1 and direct_hits == []
    hit = main_hits[0]
    assert hit["method"] == "POST" and hit["path_qs"] == "/v1/messages?beta=true"
    assert hit["body"] == _BODY
    assert hit["headers"]["x-api-key"] == "sk-test-not-real"
    assert hit["headers"]["anthropic-version"] == "2023-06-01"
    assert _failopen_rows(state) == []


# ── the bug: main proxy down ─────────────────────────────────────────────────

def test_main_down_goes_direct_and_records_proxy_down(state):
    dead = _free_port()

    async def run():
        async with _Upstream(_named("direct")) as direct:
            async with _Shim(_cfg(dead, direct.url)) as s:
                status, _, body = await _post(s.url)
        return status, json.loads(body), direct.hits

    status, data, direct_hits = asyncio.run(run())
    assert status == 200 and data["served_by"] == "direct"
    assert len(direct_hits) == 1
    assert direct_hits[0]["body"] == _BODY
    assert direct_hits[0]["path_qs"] == "/v1/messages?beta=true"
    assert direct_hits[0]["headers"]["x-api-key"] == "sk-test-not-real"
    rows = _failopen_rows(state)
    assert len(rows) == 1 and rows[0]["c"] == "proxy_down"
    assert rows[0]["d"].startswith("connect")
    assert isinstance(rows[0]["ts"], float)
    # The row never carries headers or content.
    assert "sk-test" not in json.dumps(rows) and "xxxx" not in json.dumps(rows)


def test_main_disconnects_before_responding_goes_direct(state):
    async def run():
        async def _slam(reader, writer):
            await reader.read(100)
            writer.close()

        server = await asyncio.start_server(_slam, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            async with _Upstream(_named("direct")) as direct:
                async with _Shim(_cfg(port, direct.url)) as s:
                    status, _, body = await _post(s.url)
            return status, json.loads(body), direct.hits
        finally:
            server.close()
            await server.wait_closed()

    status, data, direct_hits = asyncio.run(run())
    assert status == 200 and data["served_by"] == "direct" and len(direct_hits) == 1
    rows = _failopen_rows(state)
    assert [r["c"] for r in rows] == ["proxy_down"] and rows[0]["d"] == "disconnected"


def test_connect_timeout_goes_direct(state, monkeypatch):
    """A connect that exceeds the budget is treated as down. A real
    slow loopback connect cannot be produced reliably, so the connector's
    timeout error is injected for the main-proxy URL only."""
    import httpx

    real_send = httpx.AsyncClient.send

    async def _slow_main(self, request, *a, **kw):
        if request.url.port == main_port:
            raise httpx.ConnectTimeout("timed out", request=request)
        return await real_send(self, request, *a, **kw)

    main_port = _free_port()
    monkeypatch.setattr(httpx.AsyncClient, "send", _slow_main)

    async def run():
        async with _Upstream(_named("direct")) as direct:
            async with _Shim(_cfg(main_port, direct.url)) as s:
                status, _, body = await _post(s.url)
        return status, json.loads(body)

    status, data = asyncio.run(run())
    assert status == 200 and data["served_by"] == "direct"
    rows = _failopen_rows(state)
    assert [r["c"] for r in rows] == ["proxy_down"] and rows[0]["d"] == "connect_timeout"


def test_connect_budget_is_1s_by_default():
    # 200 ms fail-opened 58 times in 4 minutes at load ~25 with a healthy main
    # proxy (docs/BUGS.md P010-2).
    assert shim.ShimConfig().connect_timeout_s == pytest.approx(1.0)
    assert shim.ShimConfig().upstream_port == 8797
    assert shim.ShimConfig().port == 8787


# ── 5xx before any byte: one direct retry, never two ─────────────────────────

def test_main_5xx_before_bytes_retries_direct_once(state):
    async def run():
        async with _Upstream(_status(503, "main")) as main, _Upstream(_named("direct")) as direct:
            async with _Shim(_cfg(main.port, direct.url)) as s:
                status, _, body = await _post(s.url)
        return status, json.loads(body), main.hits, direct.hits

    status, data, main_hits, direct_hits = asyncio.run(run())
    assert status == 200 and data["served_by"] == "direct"
    assert len(main_hits) == 1 and len(direct_hits) == 1
    assert direct_hits[0]["body"] == _BODY
    rows = _failopen_rows(state)
    assert [r["c"] for r in rows] == ["proxy_down"] and rows[0]["d"] == "upstream_5xx:503"


def test_direct_retry_also_5xx_is_returned_not_retried_again(state):
    async def run():
        async with _Upstream(_status(500, "main")) as main, _Upstream(_status(529, "direct")) as direct:
            async with _Shim(_cfg(main.port, direct.url)) as s:
                status, _, body = await _post(s.url)
        return status, json.loads(body), main.hits, direct.hits

    status, data, main_hits, direct_hits = asyncio.run(run())
    assert status == 529 and data["served_by"] == "direct"
    assert len(main_hits) == 1 and len(direct_hits) == 1


def test_anthropic_5xx_relayed_by_a_healthy_main_proxy_is_not_proxy_down(state):
    """A 5xx that carries Anthropic's ``request-id`` was produced by Anthropic
    and relayed by a working main proxy (e.g. 529 overloaded). Retrying it
    direct would double the load on an overloaded API and count a healthy
    proxy as ``proxy_down`` in G2, so it is passed through as is."""
    async def anthropic_overloaded(request, body):
        return web.json_response({"served_by": "main", "type": "error",
                                  "error": {"type": "overloaded_error"}},
                                 status=529, headers={"request-id": "req_synthetic"})

    async def run():
        async with _Upstream(anthropic_overloaded) as main, _Upstream(_named("direct")) as direct:
            async with _Shim(_cfg(main.port, direct.url)) as s:
                status, headers, body = await _post(s.url)
        return status, headers, json.loads(body), main.hits, direct.hits

    status, headers, data, main_hits, direct_hits = asyncio.run(run())
    assert status == 529 and data["served_by"] == "main"
    assert {k.lower(): v for k, v in headers.items()}.get("request-id") == "req_synthetic"
    assert len(main_hits) == 1 and direct_hits == []
    assert _failopen_rows(state) == []


def test_main_4xx_is_returned_as_is_without_retry(state):
    async def run():
        async with _Upstream(_status(400, "main")) as main, _Upstream(_named("direct")) as direct:
            async with _Shim(_cfg(main.port, direct.url)) as s:
                status, _, body = await _post(s.url)
        return status, json.loads(body), direct.hits

    status, data, direct_hits = asyncio.run(run())
    assert status == 400 and data["served_by"] == "main" and direct_hits == []
    assert _failopen_rows(state) == []


def test_both_down_returns_anthropic_shaped_502(state):
    dead_main, dead_direct = _free_port(), _free_port()

    async def run():
        async with _Shim(_cfg(dead_main, f"http://127.0.0.1:{dead_direct}")) as s:
            return await _post(s.url)

    status, headers, body = asyncio.run(run())
    assert status == 502
    data = json.loads(body)
    assert data["type"] == "error" and data["error"]["type"] == "api_error"
    assert "proxy" in data["error"]["message"]
    assert [r["c"] for r in _failopen_rows(state)] == ["proxy_down"]


# ── streaming: byte-for-byte, and never a retry once bytes went out ──────────

_SSE_EVENTS = [
    b'event: message_start\ndata: {"type":"message_start","message":{"id":"m1"}}\n\n',
    b'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"text":"o"}}\n\n',
    b'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"text":"k \xc3\xa9"}}\n\n',
    b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
]


def test_sse_passes_through_byte_for_byte_and_incrementally(state):
    async def _sse(request, body):
        resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream",
                                                       "request-id": "req_123"})
        await resp.prepare(request)
        for ev in _SSE_EVENTS:
            await resp.write(ev)
            await asyncio.sleep(0.02)
        await resp.write_eof()
        return resp

    async def run():
        async with _Upstream(_sse) as main, _Upstream(_named("direct")) as direct:
            async with _Shim(_cfg(main.port, direct.url)) as s:
                async with aiohttp.ClientSession() as c:
                    async with c.post(s.url + "/v1/messages", data=_BODY, headers=_HEADERS) as r:
                        first = await r.content.readuntil(b"\n\n")
                        rest = await r.read()
                        return r.status, dict(r.headers), first, rest, direct.hits

    status, headers, first, rest, direct_hits = asyncio.run(run())
    assert status == 200
    assert headers["Content-Type"].startswith("text/event-stream")
    assert headers["request-id"] == "req_123"
    assert first == _SSE_EVENTS[0]
    assert first + rest == b"".join(_SSE_EVENTS)
    assert direct_hits == []


def test_compressed_body_is_not_decoded_or_altered(state):
    raw = gzip.compress(b'{"served_by":"main","pad":"' + b"z" * 5000 + b'"}')

    async def _gz(request, body):
        return web.Response(body=raw, headers={"Content-Encoding": "gzip",
                                               "Content-Type": "application/json"})

    async def run():
        async with _Upstream(_gz) as main, _Upstream(_named("direct")) as direct:
            async with _Shim(_cfg(main.port, direct.url)) as s:
                return await _post(s.url, raw=True)

    status, headers, body = asyncio.run(run())
    assert status == 200 and headers.get("Content-Encoding") == "gzip"
    assert body == raw


def test_no_retry_after_bytes_were_sent(state):
    """The main proxy sends headers and one event, then dies mid-stream. The
    client must see the truncated stream, and the shim must NOT replay the
    request direct (the model already started answering)."""
    async def _dies_mid_stream(request, body):
        resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        await resp.write(_SSE_EVENTS[0])
        await asyncio.sleep(0.05)
        request.transport.close()
        return resp

    async def run():
        async with _Upstream(_dies_mid_stream) as main, _Upstream(_named("direct")) as direct:
            async with _Shim(_cfg(main.port, direct.url)) as s:
                got = b""
                async with aiohttp.ClientSession() as c:
                    async with c.post(s.url + "/v1/messages", data=_BODY, headers=_HEADERS) as r:
                        status = r.status
                        try:
                            async for chunk in r.content.iter_any():
                                got += chunk
                        except aiohttp.ClientPayloadError:
                            pass
                await asyncio.sleep(0.05)
                return status, got, direct.hits

    status, got, direct_hits = asyncio.run(run())
    assert status == 200
    assert got.startswith(_SSE_EVENTS[0])
    assert direct_hits == [], "a request whose answer already started must never be replayed"


def test_large_request_body_is_accepted(state):
    big = json.dumps({"messages": [{"role": "user", "content": "y" * (8 * 1024 * 1024)}]}).encode()

    async def run():
        async with _Upstream(_named("main")) as main, _Upstream(_named("direct")) as direct:
            async with _Shim(_cfg(main.port, direct.url)) as s:
                status, _, body = await _post(s.url, body=big)
        return status, json.loads(body), main.hits

    status, data, main_hits = asyncio.run(run())
    assert status == 200 and data["echo_len"] == len(big)
    assert main_hits[0]["body"] == big


# ── safety of the direct target ──────────────────────────────────────────────

@pytest.mark.parametrize("url,ok", [
    ("https://api.anthropic.com", True),
    ("https://api.anthropic.com/", True),
    ("http://127.0.0.1:9", True),
    ("http://localhost:9", True),
    ("https://evil.example.com", False),
    ("http://api.anthropic.com", False),
    ("https://api.anthropic.com.evil.example", False),
])
def test_direct_url_only_anthropic_or_loopback(url, ok):
    if ok:
        assert shim.validate_direct_url(url) == url.rstrip("/")
    else:
        with pytest.raises(ValueError):
            shim.validate_direct_url(url)


def test_direct_url_rule_matches_the_main_proxy_rule():
    """The shim keeps its own copy of the rule so a broken main-proxy module
    cannot take the shim down with it; this pins the two copies together."""
    from llm_router.proxy.server import validate_upstream

    for url in ("https://api.anthropic.com", "http://127.0.0.1:9", "http://localhost:1",
                "https://evil.example.com", "http://api.anthropic.com", "ftp://127.0.0.1"):
        try:
            a = validate_upstream(url)
        except ValueError:
            a = "refused"
        try:
            b = shim.validate_direct_url(url)
        except ValueError:
            b = "refused"
        assert a == b, url


def test_shim_does_not_import_the_main_proxy_module():
    """Fail-open must survive a broken main proxy: the shim's import graph
    stays clear of proxy/server.py (and its heavy dependencies)."""
    import subprocess
    import sys

    code = ("import sys, llm_router.proxy.failopen_shim as m; "
            "print('llm_router.proxy.server' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False"


def test_cli_refuses_a_public_bind(capsys):
    with pytest.raises(SystemExit):
        shim.main(["--host", "0.0.0.0", "--port", str(_free_port())])


def test_duplicate_server_header_from_main_proxy_is_passed_not_bypassed(state):
    """Live smoke 2026-10-07: the main proxy (uvicorn) answers with its own
    ``server`` header next to Anthropic's, so the response carries two. A
    strict client parser (aiohttp's) rejected that, and 2 of 4 real calls
    fell back to Anthropic directly even though the main proxy was up. The
    shim must relay whatever the main proxy sends, as Claude Code itself does."""
    async def run():
        async def _dup(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            body = b'{"served_by":"main"}'
            writer.write(b"HTTP/1.1 200 OK\r\nserver: uvicorn\r\nServer: cloudflare\r\n"
                         b"content-type: application/json\r\ncontent-length: "
                         + str(len(body)).encode() + b"\r\n\r\n" + body)
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(_dup, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            async with _Upstream(_named("direct")) as direct:
                async with _Shim(_cfg(port, direct.url)) as s:
                    # A lenient client, like Claude Code's own: aiohttp's
                    # client would reject the relayed response for the same reason.
                    import httpx

                    async with httpx.AsyncClient(trust_env=False) as c:
                        r = await c.post(s.url + "/v1/messages", content=_BODY, headers=_HEADERS)
            return r.status_code, r.headers.get_list("server"), r.json(), direct.hits
        finally:
            server.close()
            await server.wait_closed()

    status, servers, data, direct_hits = asyncio.run(run())
    assert status == 200 and data["served_by"] == "main"
    assert "uvicorn" in servers and "cloudflare" in servers
    assert direct_hits == []
    assert _failopen_rows(state) == []


def test_cli_without_a_flag_runs_with_the_1s_budget(monkeypatch):
    """`llm-router proxy-shim` as the LaunchAgent runs it (no --connect-timeout-ms)."""
    seen = []
    monkeypatch.setattr(shim, "build_app", lambda cfg: seen.append(cfg))
    monkeypatch.setattr(shim.web, "run_app", lambda *a, **k: None)
    assert shim.main(["--port", "18787", "--upstream-port", "18797"]) == 0
    assert shim.main(["--port", "18787", "--upstream-port", "18797", "--connect-timeout-ms", "50"]) == 0
    assert [c.connect_timeout_s for c in seen] == [pytest.approx(1.0), pytest.approx(0.05)]
