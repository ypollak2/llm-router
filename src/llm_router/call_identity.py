"""Who asked: the Claude Code session id and tool_use id behind a routed call.

``routing_decisions`` rows written by the MCP ``llm()`` / ``llm_edit`` path carried no
session id (live ``usage.db`` 2026-10-06: 0 of 544 runtime rows), so O3 could not
scope a local answer to an organic session or judge whether it was redone. This
module supplies two ids, and only ids (no prompt or answer text):

``mcp_session_id()``: the session id from the environment Claude Code gives the MCP
server process (``CLAUDE_CODE_SESSION_ID``, then the explicit ``CLAUDE_SESSION_ID``).
Verified 2026-10-06 on a live Claude Code CLI MCP server: its environment carried the
session id of the ``claude --resume`` process that spawned it. Chosen over the two
other options:

* the hook-written pointer ``current_session.json``: one machine-wide file, last
  writer wins, so with two sessions open an MCP call is stamped with whichever session
  prompted last, and a host that sets no id (Claude Desktop, Cursor) gets a Claude Code
  session it has nothing to do with. A wrong id is worse than no id: O3 would judge the
  answer against another conversation's turns. So no pointer fallback here: no id
  means NULL, which O3 counts as "no session id", never guessed;
* a request field: Claude Code sends no session id in a ``tools/call`` (see below).

Known limit, stated: the environment is fixed when the server starts. If Claude Code
keeps the server across ``/clear`` (a new session id), later rows carry the old id.
``offload_share`` corrects that at read time: a row whose ``tool_use_id`` matches a
transcript event takes the transcript's session id.

``tool_use_id()``: the id of the ``tool_use`` block that made this MCP call. Claude Code
sends it as ``params._meta["claudecode/toolUseId"]`` on every ``tools/call`` (verified
2026-10-06 in the Claude Code 2.1.291 bundle). It is the same id
``usage_outcome`` uses as ``event_id`` (the transcript's ``tool_use`` id), so a local
answer joins its used / redone verdict exactly instead of by a 120 s time window.
``IdentityMCPServer.call_tool`` binds it for the span of one tool call (a ContextVar, so
concurrent calls never see each other's id).

Both getters validate shape and return None for anything else, so free text cannot
ride into a ledger column through them. ``ledger_session_id`` is the same check for the
other writers (the hook DIRECT path, agent-route, the SDK), and also refuses the
placeholders some of them pass (``sdk``, ``unknown``): those are not sessions.

``call_session_id()`` is what the router stamps. The environment id is the server
process's, fixed at start, so it is stamped only where that process serves one
Claude Code session: (a) when a ``tool_use`` id is bound (the client is Claude Code), or
(b) inside a ``call_tool`` on the stdio transport (``server.main`` calls
``mark_stdio_transport()``; Claude Code spawns one stdio server per session). On SSE
(many clients, one process), or on a host that inherited the variable from a shell
(Desktop, Cursor, Codex), a call with no ``tool_use`` id gets None, which the writers
record as ``unknown:mcp``: a wrong real-looking id is worse than that. Outside any MCP
call it is None as well: ``CLAUDE_CODE_SESSION_ID`` is also in every Bash tool shell,
so a gateway or ``route_server`` started from one would otherwise stamp that one
session on every call it later serves.

Light on purpose: ``savings_logger`` imports this inside the UserPromptSubmit hook, so
the MCP server stack (+234 ms measured) loads only when ``IdentityMCPServer`` is used.
"""
from __future__ import annotations

import os
import re
from contextvars import ContextVar, Token
from typing import Any

TOOL_USE_META_KEY = "claudecode/toolUseId"
_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_PLACEHOLDERS = frozenset({"sdk", "unknown", "none", "null", "default"})

UNKNOWN_PREFIX = "unknown:"

_TOOL_USE_ID: ContextVar[str | None] = ContextVar("llm_router_tool_use_id", default=None)
_IN_MCP_CALL: ContextVar[bool] = ContextVar("llm_router_in_mcp_call", default=False)


def clean_id(value: Any) -> str | None:
    """``value`` stripped when it has the shape of an id (``[A-Za-z0-9_.:-]{1,128}``), else None."""
    if isinstance(value, str):
        value = value.strip()
        if _ID_RE.fullmatch(value):
            return value
    return None


def ledger_session_id(value: Any) -> str | None:
    """A session id fit for a ledger column: an id (``clean_id``) that is not a placeholder."""
    sid = clean_id(value)
    return None if sid is None or sid.lower() in _PLACEHOLDERS else sid


def mcp_session_id() -> str | None:
    """The Claude Code session id of this process's caller, or None (see module doc)."""
    return (ledger_session_id(os.environ.get("CLAUDE_CODE_SESSION_ID"))
            or ledger_session_id(os.environ.get("CLAUDE_SESSION_ID")))


_STDIO_TRANSPORT = False


def mark_stdio_transport() -> None:
    """Called by ``server.main`` before ``mcp.run()`` (stdio): this process serves one client."""
    global _STDIO_TRANSPORT
    _STDIO_TRANSPORT = True


def is_unknown_session(sid: Any) -> bool:
    """True for a session value that names no session: None, "", "unknown", ``unknown:*``."""
    return not isinstance(sid, str) or sid in ("", "unknown") or sid.startswith(UNKNOWN_PREFIX)


def in_mcp_call() -> bool:
    """True inside an MCP tool call: ``IdentityMCPServer.call_tool`` is on the stack."""
    return _IN_MCP_CALL.get() or tool_use_id() is not None


def call_session_id() -> str | None:
    """``mcp_session_id()`` when it provably belongs to this call, else None (see module doc)."""
    if tool_use_id() is not None or (_IN_MCP_CALL.get() and _STDIO_TRANSPORT):
        return mcp_session_id()
    return None


#: Door names for ``unknown_session``: where a row was written when no session resolved.
DOOR_MCP, DOOR_NON_MCP = "mcp", "non_mcp"


def unknown_session(door: str | None = None) -> str:
    """The explicit value for a row whose session could not be resolved: ``unknown:<door>``.

    Never NULL (P0.8) and never a real-looking session. ``door`` defaults to where the
    call came from. G3 counts this value as missing (``kpi._prd_session``), so it
    cannot lift the completeness figure.
    """
    return f"{UNKNOWN_PREFIX}{door or (DOOR_MCP if in_mcp_call() else DOOR_NON_MCP)}"


class LedgerGuard:
    """LEDGER-EVERY-EXIT-1: which ledger tables one tool call has written to so far."""

    __slots__ = ("tables", "refused")

    def __init__(self) -> None:
        self.tables: set[str] = set()
        self.refused = False


_LEDGER_GUARD: ContextVar[LedgerGuard | None] = ContextVar("llm_router_ledger_guard", default=None)


def open_ledger_guard() -> tuple[LedgerGuard, Token]:
    guard = LedgerGuard()
    return guard, _LEDGER_GUARD.set(guard)


def close_ledger_guard(token: Token) -> None:
    _LEDGER_GUARD.reset(token)


def note_ledger_write(table: str) -> None:
    """Called by the two ledger writers after a committed insert. No-op outside a guarded call."""
    guard = _LEDGER_GUARD.get()
    if guard is not None:
        guard.tables.add(table)


def tool_use_id() -> str | None:
    """The calling ``tool_use`` id inside an MCP tool call, else None."""
    return _TOOL_USE_ID.get()


def tool_use_id_from_context(context: Any) -> str | None:
    """``_meta["claudecode/toolUseId"]`` of the request behind an MCP ``Context``."""
    try:
        meta = context.request_context.meta
    except Exception:  # noqa: BLE001 -- no request (a direct call), or another mcp shape
        return None
    if not isinstance(meta, dict):
        return None
    return clean_id(meta.get(TOOL_USE_META_KEY))


def bind(tool_use: str | None) -> Token:
    return _TOOL_USE_ID.set(clean_id(tool_use))


def reset(token: Token) -> None:
    _TOOL_USE_ID.reset(token)


def __getattr__(name: str):
    """``IdentityMCPServer`` is built on first use (PEP 562): see the module doc."""
    if name != "IdentityMCPServer":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from mcp.server.mcpserver import MCPServer

    class IdentityMCPServer(MCPServer):
        """``MCPServer`` that binds the caller's ``tool_use`` id around each tool call."""

        async def call_tool(self, name, arguments, context=None):  # type: ignore[override]
            token = bind(tool_use_id_from_context(context))
            flag = _IN_MCP_CALL.set(True)
            try:
                return await super().call_tool(name, arguments, context)
            finally:
                _IN_MCP_CALL.reset(flag)
                reset(token)

    globals()["IdentityMCPServer"] = IdentityMCPServer
    return IdentityMCPServer
