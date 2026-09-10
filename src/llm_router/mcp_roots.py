"""Resolve the caller's project from MCP roots.

The MCP server is a long-lived process whose cwd is wherever the host editor was
launched. In the field that is `$HOME`, which is nobody's project — so OKF
retrieval scoped to it found 2 documents while 1069 about the repository actually
being discussed sat one directory away, and the routed model answered "I do not
have enough information about this repository".

MCP already carries the answer. The client advertises a `roots` capability and the
server asks for the list. Verified against a real handshake on 2026-09-10 by
pointing a throwaway stdio server at Claude Code:

    "clientInfo": {"name": "claude-code", "version": "2.1.267"},
    "capabilities": {"roots": {"listChanged": true}, "elicitation": {}}

Everything here degrades rather than failing. Not every client sends roots —
Cursor's support is unverified — and a lookup that raises, times out or comes back
empty must leave scope exactly where it was before this module existed. Context is
an optimisation; a router that stops answering because it could not determine a
directory is worse than one with no context at all.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

# `list_roots()` is a round-trip to the client, and a routed call is on the user's
# critical path. Roots change when a workspace changes, which is rare.
_TTL_S = 300.0
_CACHE: dict[int, tuple[float, Path | None]] = {}
# A server may see many sessions over its life; the cache must not be one of the
# things that grows forever.
_CACHE_MAX = 32


def clear_cache() -> None:
    _CACHE.clear()


def _to_path(raw: Any) -> Path | None:
    """A root may arrive as a file:// URI or a bare path, depending on the client."""
    text = str(getattr(raw, "uri", raw) or "")
    if not text:
        return None
    if text.startswith("file://"):
        parsed = urlparse(text)
        text = unquote(parsed.path)
    try:
        return Path(text)
    except (TypeError, ValueError):
        return None


async def root_from_ctx(ctx: Any) -> Path | None:
    """The caller's project directory, or None if it cannot be determined.

    None is a normal answer, not an error: it means "fall back to the existing
    behaviour" — `$LLM_ROUTER_PROJECT_ROOT`, then the cwd.
    """
    session = getattr(ctx, "session", None)
    if session is None:
        return None

    key = id(session)
    now = time.monotonic()
    hit = _CACHE.get(key)
    if hit is not None and (now - hit[0]) < _TTL_S:
        return hit[1]

    resolved: Path | None = None
    try:
        # Ask only if the client said it can answer. Calling list_roots() on a
        # client without the capability is a protocol error, not a None.
        supports = True
        check = getattr(session, "check_client_capability", None)
        if check is not None:
            try:
                from mcp.types import RootsCapability

                supports = bool(check(RootsCapability()))
            except Exception:  # noqa: BLE001 — shape differs across mcp versions
                try:
                    supports = bool(check(None))
                except Exception:  # noqa: BLE001
                    supports = True
        if supports:
            result = await session.list_roots()
            for raw in getattr(result, "roots", None) or []:
                candidate = _to_path(raw)
                # First root that actually exists. A client may advertise a
                # workspace folder that has since been removed.
                if candidate is not None and candidate.exists():
                    resolved = candidate
                    break
    except Exception:  # noqa: BLE001 — never let a context lookup break a route
        resolved = None

    if len(_CACHE) >= _CACHE_MAX:
        oldest = min(_CACHE, key=lambda k: _CACHE[k][0])
        _CACHE.pop(oldest, None)
    _CACHE[key] = (now, resolved)
    return resolved
