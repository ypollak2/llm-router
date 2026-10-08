"""Fail-open shim in front of the main proxy (P0.10, D-17 = A).

With proxy-default installed, ``~/.claude/settings.json`` points every Claude
Code session at ``http://127.0.0.1:8787``, and that setting beats the process
env, so nothing started later can change it for a running session. When the
main proxy is down, a port nobody listens on refuses the connection and every
session's API calls fail until the proxy is back (docs/BUGS.md P010-1).

The shim owns that port instead and stays deliberately small:

* every request is forwarded, bytes in and bytes out, to the main proxy on
  ``127.0.0.1:<upstream_port>`` (default 8797,
  ``LLM_ROUTER_PROXY_UPSTREAM_PORT``);
* when the main proxy cannot be reached (connection refused, connect slower
  than the connect budget (1 s, ``--connect-timeout-ms``), or the connection drops before a response arrives), or answers
  5xx of its own (no Anthropic ``request-id``; an Anthropic 5xx such as 529
  overloaded is passed through) before any byte went to the client, the same
  request goes once to
  ``https://api.anthropic.com`` with the same headers, and a ``proxy_down``
  event is recorded in the fail-open store (``failopen.record``; KPI G2);
* once a response byte has gone to the client nothing is retried: the model
  has already started answering, and a replay would duplicate the turn;
* a drop before response headers (``disconnected``) is still retried, although
  the main proxy may already have sent the request to Anthropic. The client
  has seen nothing, so its output is not duplicated, but that turn can be
  billed twice. This follows the plan's "before any byte was sent" rule.

It does not import ``proxy/server.py``. A bad deploy of the main proxy must not
take the shim down with it, so the shim depends on aiohttp (server side), httpx
(client side; already required by ``mcp``) and the standard library, plus
``failopen`` (imported lazily, and never allowed to raise).

Why httpx for the upstream leg and not aiohttp's client: aiohttp's response
parser rejects a duplicate ``Server`` header, and the main proxy sends two
(uvicorn's own next to Anthropic's). In the 2026-10-07 smoke that sent 2 of 4
calls direct while the main proxy was up. h11 (httpx) relays such a response,
as Claude Code's own client does.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
from aiohttp import web

ANTHROPIC_URL = "https://api.anthropic.com"
DEFAULT_PORT = 8787
DEFAULT_UPSTREAM_PORT = 8797
#: 1 s, not 200 ms: a refused connect (the usual "main proxy is down") fails at
#: once whatever the budget, so a larger budget costs nothing then. It only
#: decides how long a connect that is merely slow may take before the call
#: skips the router. 200 ms did that 58 times in 4 minutes at load ~25 with a
#: healthy proxy (docs/BUGS.md P010-2).
DEFAULT_CONNECT_TIMEOUT_S = 1.0
#: Claude Code request bodies carry the whole conversation (images included);
#: aiohttp's 1 MiB default would reject long sessions.
MAX_REQUEST_BYTES = 512 * 1024 * 1024
FAILOPEN_CODE = "proxy_down"

#: Hop-by-hop request headers, plus the ones the client library sets itself.
_REQ_DROP = frozenset({"host", "content-length", "connection", "keep-alive", "transfer-encoding",
                       "te", "trailer", "upgrade", "proxy-authorization", "proxy-connection"})
#: Hop-by-hop response headers. Content-Length and Content-Encoding are kept:
#: the body is passed through undecoded, so they still describe it exactly.
_RESP_DROP = frozenset({"connection", "keep-alive", "transfer-encoding", "te", "trailer",
                        "upgrade", "proxy-authenticate", "proxy-connection"})


def validate_direct_url(url: str) -> str:
    """Only Anthropic itself or a loopback test double may receive the client's
    credentials. Same rule as ``proxy.server.validate_upstream``; the copy is
    deliberate (see the module docstring) and a test pins the two together."""
    parts = urlsplit(url)
    if parts.scheme == "https" and parts.hostname == "api.anthropic.com":
        return url.rstrip("/")
    if parts.scheme in ("http", "https") and parts.hostname in ("127.0.0.1", "localhost", "::1"):
        return url.rstrip("/")
    raise ValueError(f"refusing direct upstream {url!r}: only {ANTHROPIC_URL} or a loopback address")


@dataclass(frozen=True)
class ShimConfig:
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    upstream_port: int = DEFAULT_UPSTREAM_PORT
    direct_url: str = ANTHROPIC_URL
    connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S

    @property
    def main_url(self) -> str:
        return f"http://127.0.0.1:{self.upstream_port}"

    @classmethod
    def from_env(cls, **overrides) -> "ShimConfig":
        env_port = os.environ.get("LLM_ROUTER_PROXY_UPSTREAM_PORT", "").strip()
        direct = os.environ.get("LLM_ROUTER_PROXY_UPSTREAM", "").strip() or ANTHROPIC_URL
        kw = {"upstream_port": int(env_port) if env_port else DEFAULT_UPSTREAM_PORT,
              "direct_url": validate_direct_url(direct)}
        kw.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**kw)


def _record_proxy_down(reason: str) -> None:
    """One fail-open row per request that bypassed the main proxy: code and
    reason only, never headers or content. Never raises."""
    try:
        from llm_router import failopen

        failopen.record(FAILOPEN_CODE, detail=reason)
    except Exception:  # noqa: BLE001 — accounting must never break the fallback
        try:
            sys.stderr.write(f"llm-router proxy-shim: {FAILOPEN_CODE} ({reason}); could not record it\n")
        except Exception:  # noqa: BLE001
            pass


def _error_response(message: str, status: int = 502) -> web.Response:
    body = {"type": "error", "error": {"type": "api_error", "message": message}}
    return web.Response(status=status, text=json.dumps(body), content_type="application/json")


#: Headers httpx adds on its own. They are removed again unless the client
#: sent them, so the upstream sees the client's request, not the shim's.
_CLIENT_DEFAULTS = ("accept", "accept-encoding", "user-agent", "connection")


class _Shim:
    def __init__(self, cfg: ShimConfig):
        self.cfg = cfg
        self.client: httpx.AsyncClient | None = None
        self._main_timeout = httpx.Timeout(connect=cfg.connect_timeout_s, read=None, write=None, pool=None)
        self._direct_timeout = httpx.Timeout(connect=10.0, read=None, write=None, pool=None)

    async def start(self, _app: web.Application) -> None:
        # No connection cap: many long-lived SSE streams (sub-agents, parallel
        # sessions) must never queue behind a pool limit. trust_env=False: an
        # HTTP(S)_PROXY in the environment must not reroute loopback traffic.
        self.client = httpx.AsyncClient(
            timeout=None, trust_env=False, follow_redirects=False,
            limits=httpx.Limits(max_connections=None, max_keepalive_connections=64),
        )

    async def stop(self, _app: web.Application) -> None:
        if self.client is not None:
            await self.client.aclose()

    async def _open(self, base: str, request: web.Request, headers: dict, body: bytes,
                    timeout: httpx.Timeout) -> httpx.Response:
        req = self.client.build_request(request.method, base + request.path_qs, headers=headers,
                                        content=body, timeout=timeout)
        sent = {k.lower() for k in headers}
        for k in _CLIENT_DEFAULTS:
            if k not in sent and k in req.headers:
                del req.headers[k]
        return await self.client.send(req, stream=True)

    async def handle(self, request: web.Request) -> web.StreamResponse:
        body = await request.read()
        headers = {k: v for k, v in request.headers.items() if k.lower() not in _REQ_DROP}

        resp: httpx.Response | None = None
        reason: str | None = None
        try:
            resp = await self._open(self.cfg.main_url, request, headers, body, self._main_timeout)
        except httpx.ConnectTimeout:
            reason = "connect_timeout"
        except httpx.ConnectError:
            reason = "connect_refused"
        except (httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError, httpx.ReadTimeout):
            reason = "disconnected"
        except httpx.HTTPError as exc:
            reason = f"client_error:{type(exc).__name__}"
        if resp is not None and resp.status_code >= 500 and "request-id" not in resp.headers:
            # A 5xx with Anthropic's request-id is Anthropic's own answer (e.g.
            # 529 overloaded) relayed by a working main proxy: pass it through.
            # Only a 5xx the main proxy produced itself is a proxy failure.
            reason = f"upstream_5xx:{resp.status_code}"
            await resp.aclose()
            resp = None

        if resp is None:
            _record_proxy_down(reason or "unknown")
            try:
                resp = await self._open(self.cfg.direct_url, request, headers, body, self._direct_timeout)
            except (httpx.HTTPError, OSError) as exc:
                return _error_response(
                    f"llm-router proxy-shim: the main proxy is down ({reason}) and the direct "
                    f"call to Anthropic failed ({type(exc).__name__})")

        return await self._relay(request, resp)

    async def _relay(self, request: web.Request, resp: httpx.Response) -> web.StreamResponse:
        out = web.StreamResponse(status=resp.status_code, reason=resp.reason_phrase or None)
        for k, v in resp.headers.multi_items():
            if k.lower() not in _RESP_DROP:
                out.headers.add(k, v)
        try:
            await out.prepare(request)
            # aiter_raw: the body exactly as received (never decompressed),
            # so Content-Encoding and Content-Length still describe it.
            async for chunk in resp.aiter_raw():
                await out.write(chunk)
            await out.write_eof()
        except (httpx.HTTPError, ConnectionError, asyncio.CancelledError):
            # Bytes already reached the client: no retry. Drop the client
            # connection so it sees a truncated response, not a clean end.
            if request.transport is not None:
                request.transport.close()
            raise
        finally:
            await resp.aclose()
        return out


def build_app(cfg: ShimConfig) -> web.Application:
    validate_direct_url(cfg.direct_url)
    s = _Shim(cfg)
    app = web.Application(client_max_size=MAX_REQUEST_BYTES)
    app.on_startup.append(s.start)
    app.on_cleanup.append(s.stop)
    app.router.add_route("*", "/{path:.*}", s.handle)
    return app


def main(argv: list[str] | None = None) -> int:
    try:
        from llm_router.env_loader import load_dotenv_files

        load_dotenv_files()  # LLM_ROUTER_PROXY_UPSTREAM_PORT from .env; real env wins
    except Exception as exc:  # noqa: BLE001 — the shim runs with defaults rather than not at all
        sys.stderr.write(f"llm-router proxy-shim: .env not loaded ({type(exc).__name__}); using defaults\n")
    ap = argparse.ArgumentParser(prog="llm-router proxy-shim",
                                 description="Fail-open shim: forward to the main proxy, or to "
                                             "Anthropic directly when the main proxy is down.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--upstream-port", type=int, default=None,
                    help=f"main proxy port (default $LLM_ROUTER_PROXY_UPSTREAM_PORT or {DEFAULT_UPSTREAM_PORT})")
    ap.add_argument("--connect-timeout-ms", type=float, default=DEFAULT_CONNECT_TIMEOUT_S * 1000)
    a = ap.parse_args(argv)

    from llm_router.net_bind import refuse_public_bind_or_exit

    refuse_public_bind_or_exit(a.host, component="proxy-shim")
    try:
        cfg = ShimConfig.from_env(host=a.host, port=a.port, upstream_port=a.upstream_port,
                                  connect_timeout_s=a.connect_timeout_ms / 1000.0)
    except ValueError as exc:
        sys.stderr.write(f"llm-router proxy-shim: {exc}\n")
        return 2
    if cfg.upstream_port == cfg.port:
        sys.stderr.write("llm-router proxy-shim: --upstream-port must differ from --port\n")
        return 2
    print(f"llm-router proxy-shim -> http://{cfg.host}:{cfg.port}  main proxy {cfg.main_url}  "
          f"fallback {cfg.direct_url}  connect budget {cfg.connect_timeout_s * 1000:.0f} ms", flush=True)
    web.run_app(build_app(cfg), host=cfg.host, port=cfg.port, access_log=None, print=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
