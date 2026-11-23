"""One Claude CLI session per llm-router session, so offloaded history is prompt-cached.

``run_claude`` used to flatten the session buffer into ONE ``claude -p`` prompt per call.
Measured 2026-11-22 (N=3 per arm): a flattened multi-turn conversation gets zero prompt-cache
reads, so every offload paid the cache-WRITE price on all of its history; real turns sent with
``claude -p --resume <session_id>`` read that history from cache (Opus $4.10 -> $0.62,
Sonnet $0.42 -> $0.15 on the same tasks).

This module is the in-process memory of those session ids. Key =
(llm-router session id, scope root, cwd, model alias). The cwd is part of the key because the
CLI finds a session by its working directory: a resumed call from another cwd would not find it.

Fail-open in every direction: no key, no entry, an expired or oversized entry, an error, an
``is_error`` result or a session id that is not the one asked for all mean "run a fresh,
non-resumed call" (the pre-existing behaviour). Nothing here raises into the router.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

#: Bound on remembered sessions (a long-lived MCP server sees many sessions); oldest evicted.
_MAX_ENTRIES = 64
#: How long a CLI that answered a resume with another session id keeps resume switched off.
_MISMATCH_COOLDOWN_S = 3600.0


@dataclass
class _Entry:
    session_id: str
    last_used: float                 # time.monotonic() of the last successful turn
    last_total_cost: float | None    # cumulative total_cost_usd after that turn; None = unknown
    ctx_tokens: int                  # tokens the session held after that turn (input+cache+output)


@dataclass
class Settled:
    """What the router needs after one CLI call."""
    retry: bool = False              # the resumed call is not to be trusted: redo it fresh
    resumed: bool = False
    api_cost_usd: float | None = None     # THIS call's API-equivalent cost; None = unknown
    discarded_cost_usd: float | None = 0.0  # cost of a call whose answer was thrown away (None = unknown)


_STORE: dict[tuple, _Entry] = {}
_resume_off_until: float = 0.0


def _reset_for_tests() -> None:
    global _resume_off_until
    _STORE.clear()
    _resume_off_until = 0.0


def host_session_id() -> str | None:
    """The llm-router session this call belongs to, or None.

    ``call_identity.call_session_id`` is the one that provably belongs to this call. The
    machine-wide pointer file (``session_store.resolve_session_id``) is NOT used: it is last
    writer wins, and resuming another conversation's CLI session would leak its history.
    """
    try:
        from llm_router import call_identity
        sid = call_identity.call_session_id()
        if sid:
            return str(sid)
    except Exception:                                        # noqa: BLE001
        pass
    for var in ("CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        v = os.environ.get(var, "").strip()
        if v:
            return v
    return None


def make_key(model_alias: str, scope_root: str | None, cwd: str) -> tuple | None:
    sid = host_session_id()
    if not sid:
        return None
    return (sid, scope_root or "", os.path.realpath(cwd), model_alias)


def _cfg(config: Any, name: str, default: Any) -> Any:
    v = getattr(config, name, default)
    return v if isinstance(v, type(default)) and not isinstance(v, bool) else default


def enabled(config: Any) -> bool:
    return getattr(config, "llm_router_claude_offload_resume", False) is True


def plan(config: Any, model_alias: str, scope_root: str | None, cwd: str,
         *, conversation: object | None, now: float | None = None) -> tuple[tuple | None, _Entry | None]:
    """``(key, entry)`` for this call. ``key`` None = do not use sessions; ``entry`` None = first turn.

    A caller-supplied ``conversation`` (the gateway) owns its own history and is packed per
    attempt, so it never resumes.
    """
    try:
        if not enabled(config) or conversation is not None:
            return None, None
        now = time.monotonic() if now is None else now
        if now < _resume_off_until:
            return None, None
        key = make_key(model_alias, scope_root, cwd)
        if key is None:
            return None, None
        entry = _STORE.get(key)
        if entry is None:
            return key, None
        ttl = float(_cfg(config, "llm_router_claude_offload_resume_ttl_s", 270))
        cap = int(_cfg(config, "llm_router_claude_offload_resume_max_ctx_tokens", 150_000))
        if now - entry.last_used > ttl or entry.ctx_tokens >= cap:
            _STORE.pop(key, None)
            return key, None
        return key, entry
    except Exception:                                        # noqa: BLE001
        return None, None


def settle(key: tuple | None, entry: _Entry | None, result: Any, *, now: float | None = None) -> Settled:
    """Fold one CLI result into the store and say whether to redo the call fresh."""
    global _resume_off_until
    now = time.monotonic() if now is None else now
    resumed = entry is not None
    if key is None:
        # No session to track: a fresh call, whose reported total IS this call's cost.
        t = getattr(result, "total_cost_usd", None)
        return Settled(api_cost_usd=t if getattr(result, "success", False) else None)
    sid = getattr(result, "session_id", None)
    total = getattr(result, "total_cost_usd", None)
    usage = getattr(result, "usage", None)
    ok = bool(getattr(result, "success", False))
    if resumed and not ok:
        _STORE.pop(key, None)
        # a timeout is not retried: a second full wait would double the stall
        return Settled(retry=getattr(result, "exit_code", 1) != 124, resumed=True)
    if resumed and sid != entry.session_id:
        # The CLI answered a resume with a different (or no) session id: the history this
        # answer saw is not the one we meant. Drop it, redo fresh, and stop resuming for a
        # while so a CLI that rewrites ids does not double every call.
        _STORE.pop(key, None)
        _resume_off_until = now + _MISMATCH_COOLDOWN_S
        return Settled(retry=True, resumed=True, discarded_cost_usd=_delta(total, entry.last_total_cost, True))
    if not ok or not sid:
        _STORE.pop(key, None)
        return Settled(resumed=resumed)
    cost = _delta(total, entry.last_total_cost if resumed else None, resumed)
    ctx = sum(usage.values()) if isinstance(usage, dict) else 0
    if usage is None:
        # context size unknown: never resume an unmeasured session past one turn.
        _STORE.pop(key, None)
    else:
        _STORE[key] = _Entry(sid, now, total, ctx)
        while len(_STORE) > _MAX_ENTRIES:
            _STORE.pop(min(_STORE, key=lambda k: _STORE[k].last_used), None)
    return Settled(resumed=resumed, api_cost_usd=cost)


def _delta(total: float | None, prev: float | None, resumed: bool) -> float | None:
    """This call's cost from the CUMULATIVE session ``total_cost_usd``; None when not knowable."""
    if total is None:
        return None
    if not resumed:
        return total
    if prev is None or total < prev:
        return None
    return total - prev
