"""SSE keepalive for a long local step on the proxy (flag ``LLM_ROUTER_PROXY_KEEPALIVE``, off by default).

Claude Code's stream idle watchdog on a custom ANTHROPIC_BASE_URL (event-level ~300 s per
https://code.claude.com/docs/en/env-vars and https://code.claude.com/docs/en/network-config; the byte-level
default is unconfirmed) kills and retries a call that sends nothing. The local backend is called without
streaming (``backend.complete``): the reply exists only when it is whole and validated, so there is no "first
local token" to wait for. Once a streaming request's local attempt has run ``after_s`` seconds, the proxy commits
the HTTP 200 SSE response and sends Anthropic ``event: ping`` frames every ``interval_s`` seconds until the
attempt ends. Then either the served message is rendered as usual, or the step falls back to Claude and the
upstream SSE events are relayed into the already-open stream (``relay_fallback``).

After the 200 is committed an upstream failure can only be reported inside the stream, as ``event: error``.
Non-streaming requests cannot be kept alive (one JSON body, no earlier bytes): they never take this path and
their deadline cap stays ``deadline.DEFAULT_CAP_S``.
"""

from __future__ import annotations

import asyncio
import json
import os

ENV_FLAG = "LLM_ROUTER_PROXY_KEEPALIVE"
ENV_AFTER = "LLM_ROUTER_PROXY_KEEPALIVE_AFTER_S"
ENV_INTERVAL = "LLM_ROUTER_PROXY_KEEPALIVE_INTERVAL_S"
DEFAULT_AFTER_S = 15.0
DEFAULT_INTERVAL_S = 10.0
PING = b'event: ping\ndata: {"type": "ping"}\n\n'


def enabled() -> bool:
    # literal name below: env_registry's scan needs it
    return os.environ.get("LLM_ROUTER_PROXY_KEEPALIVE", "").strip().lower() in ("1", "on", "true", "yes")


def after_s() -> float:
    from llm_router.proxy import deadline

    return deadline._env_float(os.environ.get("LLM_ROUTER_PROXY_KEEPALIVE_AFTER_S"), DEFAULT_AFTER_S)


def interval_s() -> float:
    from llm_router.proxy import deadline

    return deadline._env_float(os.environ.get("LLM_ROUTER_PROXY_KEEPALIVE_INTERVAL_S"), DEFAULT_INTERVAL_S)


def _error_type(status: int) -> str:
    if status == 429:
        return "rate_limit_error"
    if status == 529:
        return "overloaded_error"
    if status in (401, 403):
        return "authentication_error" if status == 401 else "permission_error"
    if status == 404:
        return "not_found_error"
    if 400 <= status < 500:
        return "invalid_request_error"
    return "api_error"


def error_frame(status: int, body: bytes) -> bytes:
    """``event: error`` for an upstream reply that is not a 200: the upstream's own error type and message when
    its body carries them, otherwise a type derived from the status."""
    etype, message = _error_type(status), f"upstream returned HTTP {status}"
    try:
        err = json.loads(body).get("error")
        if isinstance(err, dict):
            etype = str(err.get("type") or etype)
            message = str(err.get("message") or message)
    except (ValueError, AttributeError):
        pass
    return b"event: error\ndata: " + json.dumps(
        {"type": "error", "error": {"type": etype, "message": message}}).encode() + b"\n\n"


async def ping_until(task: asyncio.Task, row: dict):
    """Yield a ping now and every ``interval_s`` seconds until ``task`` is done (counted in ``row``)."""
    interval = interval_s()
    while not task.done():
        yield PING
        row["keepalive_pings"] = row.get("keepalive_pings", 0) + 1
        await asyncio.wait({task}, timeout=interval)


async def relay_fallback(resp):
    """The chunks of a Claude fallback ``resp`` (a Starlette Response) inside the open 200 stream. A 200 is
    relayed as is (its SSE starts with the one message_start); anything else becomes one ``event: error``."""
    status = resp.status_code
    it = getattr(resp, "body_iterator", None)
    if it is None:
        chunks = [bytes(resp.body or b"")]
    else:
        chunks = None
    if status == 200:
        if chunks is not None:
            yield chunks[0]
        else:
            async for c in it:
                yield c
        return
    if chunks is None:
        chunks = [c async for c in it]
    yield error_frame(status, b"".join(chunks))
