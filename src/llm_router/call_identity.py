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

``call_session_id()`` is what the router stamps: the environment id only inside an MCP
tool call (a ``tool_use`` id is bound). ``CLAUDE_CODE_SESSION_ID`` is also in every Bash
tool shell, so a gateway or ``route_server`` started from one would otherwise stamp that
one session on every call it later serves, for any host.

Task identity (PLAN v16 P1.10, R-EVL-2). Three ids ride every ledger row:

* ``session_id``: the Claude Code session (above), or the SDK caller's / a generated one.
* ``task_id``: one human request. Host turns: ``sha256(session_id + ":" + human_turn_index)
  [:16]``, where the index is a per-session counter the UserPromptSubmit hook advances
  (``begin_turn``) and every other process reads (``turn_task_id``): the MCP server, the
  proxy and the sub-agent hooks all see the same id for the same turn. SDK / gateway / MCP
  callers may pass their own (``scope(task_id=...)``). When nothing names a task a row gets
  a generated one (``sha256(session_id + ":untracked:" + trace_id)[:16]``), so a row is
  never NULL because the host was unknown. Escalations and retries run inside one
  ``scope`` and share its task_id.
* ``trace_id``: ``uuid4().hex`` minted per routed call (``scope`` mints a fresh one on
  entry), so a retry is a new trace of the same task.

The ids are bound in ContextVars (like ``tool_use_id``), so ``cost.log_usage`` and
``cost.log_routing_decision`` stamp them without any caller passing them; a writer that
runs outside a scope (a hook, a test) resolves them at write time. All of them are ids
only (``clean_id`` shape), never prompt text.

Light on purpose: ``savings_logger`` imports this inside the UserPromptSubmit hook, so
the MCP server stack (+234 ms measured) loads only when ``IdentityMCPServer`` is used.
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import uuid
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any, Iterator

TOOL_USE_META_KEY = "claudecode/toolUseId"
_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_PLACEHOLDERS = frozenset({"sdk", "unknown", "none", "null", "default"})

_TOOL_USE_ID: ContextVar[str | None] = ContextVar("llm_router_tool_use_id", default=None)
_TASK_ID: ContextVar[str | None] = ContextVar("llm_router_task_id", default=None)
_TRACE_ID: ContextVar[str | None] = ContextVar("llm_router_trace_id", default=None)

TASK_ID_LEN = 16
TURN_STATE_DIR = "turn_state"


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


def call_session_id() -> str | None:
    """``mcp_session_id()`` inside an MCP tool call (a ``tool_use`` id is bound), else None."""
    return mcp_session_id() if tool_use_id() is not None else None


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


def derive_task_id(session_id: str, turn_index: int) -> str:
    """The host task id of human turn ``turn_index`` of ``session_id`` (16 hex chars)."""
    return hashlib.sha256(f"{session_id}:{int(turn_index)}".encode("utf-8")).hexdigest()[:TASK_ID_LEN]


def new_trace_id() -> str:
    """A fresh per-call trace id (``uuid4().hex``)."""
    return uuid.uuid4().hex


def ledger_task_id(value: Any) -> str | None:
    """A task id fit for a ledger column: an id (``clean_id``) that is not a placeholder."""
    return ledger_session_id(value)


def _turn_state_path(session_id: str):
    from llm_router import paths

    safe = re.sub(r"[^A-Za-z0-9._-]", "_", session_id)
    return paths.state_path(TURN_STATE_DIR, f"{safe}.json")


def _read_turn_state(session_id: str) -> dict | None:
    try:
        data = json.loads(_turn_state_path(session_id).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 -- absent, torn or unreadable: no turn state
        return None
    return data if isinstance(data, dict) else None


def begin_turn(session_id: Any) -> str | None:
    """Advance ``session_id``'s human-turn counter and return the new turn's task id.

    Called once per human prompt by the UserPromptSubmit hook. Persisted per session
    (``<state>/turn_state/<session>.json``: counter + current task id, no text) so the
    MCP server, the proxy and the sub-agent hooks, which are other processes, read the
    same id with ``turn_task_id``. Fail-open: returns None, never raises, when the
    session id is not an id or the state cannot be written.
    """
    sid = ledger_session_id(session_id)
    if sid is None:
        return None
    try:
        prev = _read_turn_state(sid) or {}
        idx = int(prev.get("turn_index", 0)) + 1
        task = derive_task_id(sid, idx)
        path = _turn_state_path(sid)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"turn_index": idx, "task_id": task}), encoding="utf-8")
        os.replace(tmp, path)
        return task
    except Exception:  # noqa: BLE001 -- identity must never break the hook
        return None


def turn_task_id(session_id: Any) -> str | None:
    """The task id of ``session_id``'s current human turn (``begin_turn``), else None."""
    sid = ledger_session_id(session_id)
    if sid is None:
        return None
    state = _read_turn_state(sid)
    return ledger_task_id(state.get("task_id")) if state else None


def current_task_id() -> str | None:
    """The task id bound by the enclosing ``scope``, else None."""
    return _TASK_ID.get()


def current_trace_id() -> str | None:
    """The trace id bound by the enclosing ``scope``, else None."""
    return _TRACE_ID.get()


def resolve_task_id(session_id: Any = None, explicit: Any = None, trace_id: str | None = None) -> str:
    """The task id for a call: the caller's, else the bound one, else the session's current
    human turn, else a generated one. Never None, never a placeholder."""
    tid = ledger_task_id(explicit) or current_task_id() or turn_task_id(session_id)
    if tid:
        return tid
    seed = f"{ledger_session_id(session_id) or ''}:untracked:{trace_id or current_trace_id() or new_trace_id()}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:TASK_ID_LEN]


def row_ids(session_id: Any = None) -> tuple[str, str]:
    """``(task_id, trace_id)`` to stamp on a ledger row written now for ``session_id``."""
    trace = current_trace_id() or new_trace_id()
    return resolve_task_id(session_id, trace_id=trace), trace


@contextmanager
def scope(session_id: Any = None, task_id: Any = None) -> Iterator[tuple[str, str]]:
    """Bind a fresh trace id and a task id for one routed call; yield ``(task_id, trace_id)``.

    The task id is ``resolve_task_id``: ``task_id`` if the caller passed one, else the
    enclosing scope's (so an escalation or retry run inside an outer scope shares it),
    else the session's current human turn, else generated. The trace id is always new.
    """
    trace = new_trace_id()
    task = resolve_task_id(session_id, explicit=task_id, trace_id=trace)
    t_tok, r_tok = _TASK_ID.set(task), _TRACE_ID.set(trace)
    try:
        yield task, trace
    finally:
        _TRACE_ID.reset(r_tok)
        _TASK_ID.reset(t_tok)


def traced(func):
    """Decorator: run an async routed-call function inside a ``scope`` (one trace per call).

    The session is the MCP caller's (``call_session_id``). Arguments pass through untouched."""
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        with scope(call_session_id()):
            return await func(*args, **kwargs)
    return wrapper


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
            try:
                # P1.10: every routed call of this tool call shares one task id.
                with scope(call_session_id()):
                    return await super().call_tool(name, arguments, context)
            finally:
                reset(token)

    globals()["IdentityMCPServer"] = IdentityMCPServer
    return IdentityMCPServer
