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
process's, fixed at start, and also inherited by every child of a Claude Code shell
(Codex, Cursor, Desktop spawning the same stdio server), so it is stamped only when
the call itself shows it came from Claude Code: (a) a ``tool_use`` id is bound, or
(b) the MCP client named itself ``claude-code`` in the ``initialize`` handshake
(``clientInfo.name``, read per server session from the request, never per process;
the exact name was captured from a real handshake, see ``mcp_roots.py``). Any other
call (Codex, Cursor, Desktop, unknown client, no ``clientInfo``) gets None, which the
writers record as ``unknown:mcp``: a wrong real-looking id is worse than that. Outside
any MCP call it is None as well: ``CLAUDE_CODE_SESSION_ID`` is also in every Bash tool
shell, so a gateway or ``route_server`` started from one would otherwise stamp that
one session on every call it later serves. Remaining limits: several Claude Code
sessions sharing one SSE process, with a bound ``tool_use`` id, still read that
process's one env id; and a stale id after ``/clear`` on a long-lived server, which
``offload_share`` corrects at read time.

Task identity (PLAN v16 P1.10, R-EVL-2). Three ids ride every ledger row:

* ``session_id``: the Claude Code session (above), or the SDK caller's / a generated one.
* ``task_id``: one human request. Host turns: ``sha256(session_id + human_turn_index + nonce)[:16]``
  (plain concatenation; session ids are fixed-length UUIDs; the PLAN formula plus a random
  per-turn random nonce so a session resumed after its turn state was pruned never reuses an id),
  where the index is a per-session counter the UserPromptSubmit hook advances
  (``begin_turn``) and every other process reads (``turn_task_id``): the MCP server, the
  proxy and the sub-agent hooks all see the same id for the same turn. SDK / gateway / MCP
  callers may pass their own (``scope(task_id=...)``). When nothing names a task a row gets
  a GENERATED stand-in, ``gen-<12 hex>`` (``GENERATED_PREFIX``): it keeps the row joinable
  to its trace but is not a recorded task, so the G3 / P1.10-a counts treat it as missing
  (``is_generated_task_id``). Escalations and retries run inside one ``scope`` and share
  its task_id.
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

UNKNOWN_PREFIX = "unknown:"

_TOOL_USE_ID: ContextVar[str | None] = ContextVar("llm_router_tool_use_id", default=None)
_IN_MCP_CALL: ContextVar[bool] = ContextVar("llm_router_in_mcp_call", default=False)
_CLIENT_IS_CLAUDE_CODE: ContextVar[bool] = ContextVar("llm_router_client_is_cc", default=False)

#: ``clientInfo.name`` Claude Code sends in ``initialize`` (real handshake, 2.1.267).
CLAUDE_CODE_CLIENT_NAME = "claude-code"

_TASK_ID: ContextVar[str | None] = ContextVar("llm_router_task_id", default=None)
_TRACE_ID: ContextVar[str | None] = ContextVar("llm_router_trace_id", default=None)

TASK_ID_LEN = 16
TURN_STATE_DIR = "turn_state"
GENERATED_PREFIX = "gen-"
#: ``begin_turn`` deletes turn-state files untouched for this long (see ``_prune_turn_state``).
TURN_STATE_TTL_S = 7 * 86400
#: ``begin_turn`` waits at most this long for the turn-state lock. It runs inside the
#: UserPromptSubmit hook (P0.9 latency bars are tens of ms), so a held lock must not stall it.
TURN_LOCK_TIMEOUT_S = 0.05


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


def client_is_claude_code(context: Any) -> bool:
    """True when this request's MCP session identified itself as Claude Code (``clientInfo.name``)."""
    try:
        name = context.request_context.session.client_params.client_info.name
    except Exception:  # noqa: BLE001 -- no session / no clientInfo / another mcp shape
        return False
    return isinstance(name, str) and name.strip().lower() == CLAUDE_CODE_CLIENT_NAME


def is_unknown_session(sid: Any) -> bool:
    """True for a session value that names no session: None, "", "unknown", ``unknown:*``."""
    return not isinstance(sid, str) or sid in ("", "unknown") or sid.startswith(UNKNOWN_PREFIX)


def in_mcp_call() -> bool:
    """True inside an MCP tool call: ``IdentityMCPServer.call_tool`` is on the stack."""
    return _IN_MCP_CALL.get() or tool_use_id() is not None


def call_session_id() -> str | None:
    """``mcp_session_id()`` when it provably belongs to this call, else None (see module doc)."""
    if tool_use_id() is not None or (_IN_MCP_CALL.get() and _CLIENT_IS_CLAUDE_CODE.get()):
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

    __slots__ = ("tables", "refused", "refused_reason", "profile")

    def __init__(self) -> None:
        self.tables: set[str] = set()
        self.refused = False
        #: F25 / D-170: the reason code of a refusal that is not a breaker refusal (None = ``breaker_open``).
        self.refused_reason: str | None = None
        #: F25 / D-170 (4): the routing profile the router resolved for this call (None = not routed yet).
        self.profile: Any = None


_LEDGER_GUARD: ContextVar[LedgerGuard | None] = ContextVar("llm_router_ledger_guard", default=None)


def open_ledger_guard() -> tuple[LedgerGuard, Token]:
    guard = LedgerGuard()
    return guard, _LEDGER_GUARD.set(guard)


def close_ledger_guard(token: Token) -> None:
    _LEDGER_GUARD.reset(token)


def note_ledger_profile(profile: Any) -> None:
    """F25 / D-170 (4): record the call's resolved routing profile, so a row the guard writes names it
    instead of the process-wide active profile. No-op outside a guarded call."""
    guard = _LEDGER_GUARD.get()
    if guard is not None:
        guard.profile = profile


def refuse_ledger_call(reason: str) -> None:
    """F25 / D-170 (2): mark the guarded call as refused for ``reason``; the guard's row carries that code.
    No-op outside a guarded call."""
    guard = _LEDGER_GUARD.get()
    if guard is not None:
        guard.refused = True
        guard.refused_reason = reason


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


def derive_task_id(session_id: str, turn_index: int, nonce: str = "") -> str:
    """The host task id of human turn ``turn_index`` of ``session_id`` (16 hex chars).

    ``nonce`` is the random value ``begin_turn`` mints for each turn and stores in the
    turn state. It makes the id independent of the counter: a session resumed after
    ``_prune_turn_state`` deleted its state restarts at turn 1 with a new nonce, so it cannot
    reuse the old turn-1 id, and a lock-timeout race cannot repeat an id. Deviation from the PLAN formula
    ``sha256(session_id + index)``; with no nonce the formula is exactly the PLAN's.
    """
    return hashlib.sha256(f"{session_id}{int(turn_index)}{nonce}".encode("utf-8")).hexdigest()[:TASK_ID_LEN]


def is_generated_task_id(value: Any) -> bool:
    """True for a stand-in minted because nothing named the task (``gen-<hex>``)."""
    return isinstance(value, str) and value.startswith(GENERATED_PREFIX)


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
    session id is not an id or the state cannot be written. The lock wait is capped at
    ``TURN_LOCK_TIMEOUT_S`` (50 ms): on a timeout the turn still gets an id at once, still
    unique (every turn carries its own random nonce), never a blocked hook.
    """
    sid = ledger_session_id(session_id)
    if sid is None:
        return None
    try:
        from llm_router.file_lock import exclusive_lock

        path = _turn_state_path(sid)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Read-modify-write under an advisory lock: two hooks of one session (a resumed
        # process, a sub-agent's prompt) must not both read N and write N+1. Best-effort:
        # the wait is capped at TURN_LOCK_TIMEOUT_S (the prompt hook is latency-bound).
        with exclusive_lock(path.with_suffix(".lock"), timeout=TURN_LOCK_TIMEOUT_S):
            prev = _read_turn_state(sid) or {}
            last = prev.get("turn_index")
            idx = 1 if not isinstance(last, int) else last + 1  # no state: the first turn
            # A fresh random nonce per turn, stored with the turn. On a lock timeout
            # (``exclusive_lock`` yields False: FAIL OPEN, proceed unlocked) the counter may be stale or a
            # racing hook may roll it back; the nonce alone keeps the id unique, so a race
            # costs at worst a repeated turn_index, never a repeated task_id. It also means a
            # session resumed after _prune_turn_state cannot reuse an old turn's id.
            nonce = uuid.uuid4().hex[:12]
            task = derive_task_id(sid, idx, nonce)
            tmp = path.with_name(path.name + f".{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp")
            tmp.write_text(json.dumps({"turn_index": idx, "task_id": task, "nonce": nonce}),
                           encoding="utf-8")
            os.replace(tmp, path)
        if idx == 1:  # a new session: the one moment housekeeping is cheap and bounded
            _prune_turn_state(path.parent)
        return task
    except Exception as exc:  # noqa: BLE001 -- identity must never break the hook
        try:
            from llm_router import failopen

            failopen.record("CHZ-FO-BEGIN-TURN", exc)
        except Exception:  # noqa: BLE001
            pass
        return None


def _prune_turn_state(directory) -> None:
    """Delete turn-state files (``.json``, ``.lock``, stray ``.tmp``) untouched for
    ``TURN_STATE_TTL_S``. Runs from ``begin_turn`` when a session's first turn is recorded,
    so it costs one directory scan per new session, never per prompt. Never raises."""
    import time

    try:
        cutoff = time.time() - TURN_STATE_TTL_S
        for entry in os.scandir(directory):
            try:
                if entry.is_file() and entry.stat().st_mtime < cutoff:
                    os.unlink(entry.path)
            except OSError:
                continue
    except OSError:
        return


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
    human turn, else a ``gen-`` stand-in (not counted as recorded). Never None."""
    tid = ledger_task_id(explicit) or current_task_id() or turn_task_id(session_id)
    if tid:
        return tid
    seed = f"{ledger_session_id(session_id) or ''}:untracked:{trace_id or current_trace_id() or new_trace_id()}"
    return GENERATED_PREFIX + hashlib.sha256(seed.encode("utf-8")).hexdigest()[: TASK_ID_LEN - len(GENERATED_PREFIX)]


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
        """``MCPServer`` that binds the caller's ``tool_use`` id around each tool call and
        runs it through ``mcp_door.run_door`` (readable errors, routed-call deadline)."""

        async def call_tool(self, name, arguments, context=None):  # type: ignore[override]
            token = bind(tool_use_id_from_context(context))
            flag = _IN_MCP_CALL.set(True)
            cc = _CLIENT_IS_CLAUDE_CODE.set(client_is_claude_code(context))
            try:
                # P1.10: every routed call of this tool call shares one task id.
                with scope(call_session_id()):
                    # D-141: one choke point for every tool — a readable error, not the bare
                    # "Error executing tool <name>", and the routed doors' overall deadline.
                    from llm_router.mcp_door import run_door

                    return await run_door(name, super().call_tool(name, arguments, context))
            finally:
                _CLIENT_IS_CLAUDE_CODE.reset(cc)
                _IN_MCP_CALL.reset(flag)
                reset(token)

    globals()["IdentityMCPServer"] = IdentityMCPServer
    return IdentityMCPServer
