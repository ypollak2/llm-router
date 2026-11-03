"""The MCP tool door: a readable error instead of a bare one, and an overall deadline (D-141).

A. ``mcp`` >= 2.2 (the live install, 2.2.0) reduces any unexpected exception a tool raises
to the text ``Error executing tool <name>`` and keeps the exception's own message on the
server (``UnexpectedToolError`` in ``mcp/server/mcpserver/tools/base.py``); 2.0.0, the version
``uv.lock`` pins, wraps it as ``ToolError("Error executing tool <name>: <message>")``. Both
put the tool's own exception in ``__cause__``. Our router raises messages written for the
user ("No providers available for research/premium ... Fix: run `llm-router doctor`"),
and Claude Code showed only the bare string (coordinator repro 2026-10-16, D-141).
``run_door`` is the one choke point: ``IdentityMCPServer.call_tool`` runs every tool
call through it, so an unexpected exception from ANY tool comes back as a result whose
first line is ``llm-router error: <ExceptionType>: <message>``, plus the models tried
when the router recorded any. ``asyncio.CancelledError`` and ``KeyboardInterrupt`` are
never caught here (they are not ``Exception`` and ``mcp`` does not wrap them).

B. A routed call had no overall deadline: a ``task="query"`` chain of four local models
hung >150 s while other processes held the same Ollama model, and the MCP client stops
waiting at ~120 s. The routed doors (``DEADLINE_TOOLS``: every MCP tool that calls
``route_and_call``) now run under one deadline, ``LLM_ROUTER_MCP_DEADLINE_S`` (default
90 s, below the client's ~120 s; 0 turns it off). Inside that deadline each LOCAL
(``ollama/``) attempt made through ``providers.call_llm`` is capped at half the deadline
(45 s by default, below ``request_timeout``'s 120 s), so one hung local model still leaves
room for the next candidate. Cloud attempts are not capped (D-154: extended-thinking and
research calls legitimately take 45-120 s); they run until the overall deadline. A cap
expiry raises ``AttemptCapExceeded``, which is not a timeout to the router: it does not
start the LOCAL-TIMEOUT-1 cooldown or count as a provider failure, so routing does not
change. When the deadline itself expires the door returns
``llm-router error: deadline <N>s exceeded; tried: <models>; ...``.

C. A stale server (BIND W2). The MCP server lives as long as the Claude Code session, so a
deploy (an upgrade, a pull into an editable install) replaces the package on disk under a
process that loaded the old modules at start. A lazy import then pulls a NEW module into
an OLD process and the tool crashes. In the audit window 5 of the 16 MCP errors were this:
a bare ``Error executing tool`` until the owner reconnected the server. ``snapshot_start``
records a fingerprint of the package at server start (see ``server.py``); when a tool
crashes, ``run_door`` compares it with the package on disk, and if it changed the result
says so and names the fix (``/mcp``), with the original error under it.

D. A busy local queue (BIND W2). After a deadline expired with only local (``ollama/``)
models tried, the next calls would each wait the full deadline again (5 x 90 s = 7.5 min
in the audit window, at least 3 of the 5 made under a hold). For ``BUSY_WINDOW_S``
after such an expiry, a call that is still on local models only ``BUSY_PROBE_S`` after it
started is given up at once, with a message that says why. A call that has reached a
cloud model is never cut.

Light on purpose: stdlib only at import; ``mcp`` is imported inside ``run_door``.
"""
from __future__ import annotations

import asyncio
import math
import os
import time
import traceback
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Awaitable

#: Default overall deadline for a routed MCP call, seconds. Below the MCP client's ~120 s.
MCP_DEADLINE_DEFAULT_S = 90.0

#: Share of the deadline one provider attempt may use, so a hung attempt leaves room for
#: at least one more candidate inside the same deadline.
ATTEMPT_SHARE = 0.5

#: Every MCP tool that calls ``route_and_call``: ``llm()``, ``llm_route``, the task aliases,
#: ``llm_edit``, ``llm_text_job`` and ``llm_router_agent_route`` (D-154).
DEADLINE_TOOLS = frozenset({
    "llm", "llm_route",
    "llm_query", "llm_research", "llm_generate", "llm_analyze", "llm_reason", "llm_code",
    "llm_edit", "llm_text_job", "llm_router_agent_route",
})


class AttemptCapExceeded(Exception):
    """A local attempt gave no answer within its share of the MCP deadline.

    Deliberately NOT a ``TimeoutError`` and without "timeout" in its name: the router's
    LOCAL-TIMEOUT-1 cooldown (``router._is_timeout_error``) must not fire for a cap the door
    imposed, and the router records it as ``fallback_reason="deadline_cap"``."""

    def __init__(self, model: str, cap_s: float) -> None:
        super().__init__(f"deadline_cap: {model} gave no answer within the {cap_s:g}s "
                         "per-attempt cap of the MCP deadline")
        self.model = model
        self.cap_s = cap_s

_MAX_MESSAGE_CHARS = 2000


class _CallState:
    """One MCP tool call: its deadline and the router's attempt lists (shared by reference)."""

    __slots__ = ("deadline_s", "traces", "gave_up_early")

    def __init__(self, deadline_s: float) -> None:
        self.deadline_s = deadline_s
        self.traces: list[list[str]] = []
        self.gave_up_early = False


_STATE: ContextVar[_CallState | None] = ContextVar("llm_router_mcp_door", default=None)


# ── C. a stale server ────────────────────────────────────────────────────────

#: The package as it was when this server process started: ``fp`` is
#: :func:`code_fingerprint`, ``at`` the wall-clock time. None until ``snapshot_start``.
_START: dict[str, Any] = {"fp": None, "at": None}


def _py_files(root: Path) -> tuple[int, int]:
    """(count, newest mtime in ns) of the ``.py`` files under *root*."""
    count = newest = 0
    stack = [str(root)]
    while stack:
        try:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name != "__pycache__":
                            stack.append(entry.path)
                    elif entry.name.endswith(".py"):
                        count += 1
                        newest = max(newest, entry.stat().st_mtime_ns)
        except OSError:
            continue
    return count, newest


def code_fingerprint(root: Path | None = None) -> tuple[str, int, int]:
    """``(distribution version on disk, python files, newest mtime ns)`` of the package.

    The version is read from the installed metadata on every call (an upgrade changes it);
    the file count and newest mtime catch an editable install that moved without one."""
    import llm_router

    try:
        from importlib.metadata import version

        installed = version("llm-routing")
    except Exception:  # noqa: BLE001 -- no metadata (a source checkout): the files still tell
        installed = "unknown"
    count, newest = _py_files(root or Path(llm_router.__file__).resolve().parent)
    return installed, count, newest


def snapshot_start() -> None:
    """Record the package fingerprint of this server process; the first call wins."""
    if _START["fp"] is None:
        try:
            _START["fp"], _START["at"] = code_fingerprint(), time.time()
        except Exception as exc:  # noqa: BLE001 -- no snapshot means no stale claim, never an error
            _record("CHZ-FO-MCP-SNAPSHOT", exc, "snapshot_start")


def stale_reason() -> str | None:
    """Why this server's code is out of date, or None (unchanged, or never snapshotted).

    Unknown is not stale: without a start snapshot nothing is claimed."""
    start = _START["fp"]
    if start is None:
        return None
    try:
        now = code_fingerprint()
    except Exception:  # noqa: BLE001 -- cannot read the package: claim nothing
        return None
    if now == start:
        return None
    started = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(_START["at"] or 0))
    if now[0] != start[0]:
        what = f"version {start[0]} -> {now[0]}"
    elif now[1] != start[1]:
        what = f"{now[1] - start[1]:+d} python file(s)"
    else:
        what = "files changed on disk"
    return f"started {started}; the package has changed since ({what})"


def stale_text(reason: str, exc: BaseException, tried: list[str]) -> str:
    """The result a stale server returns for a crashed tool: the fix first, the error under it."""
    return (
        f"llm-router error: this MCP server is running code from before the last update "
        f"({reason}), which can make a tool crash like this. Reconnect it with /mcp "
        f"(or restart Claude Code) and retry.\n"
        + error_text(exc, tried).replace("llm-router error:", "original error:", 1)
    )


# ── D. a busy local queue ────────────────────────────────────────────────────

#: How long after a local-only deadline expiry a still-local call is given up early.
BUSY_WINDOW_S = 300.0
#: In that window, a call still on local models this long after it started is given up.
BUSY_PROBE_S = 20.0
_BUSY: dict[str, float] = {"until": 0.0, "waited": 0.0}


def _all_local(tried: list[str]) -> bool:
    return bool(tried) and all(str(m).startswith("ollama/") for m in tried)


def busy_window_open() -> bool:
    return time.monotonic() < _BUSY["until"]


def reset_busy() -> None:
    """Close the busy window (test seam, and a process that learned Ollama is free)."""
    _BUSY["until"] = 0.0
    _BUSY["waited"] = 0.0


def busy_text(limit: float, probe: float, tried: list[str]) -> str:
    return (
        f"llm-router error: local models are busy: still waiting on {', '.join(tried)} after "
        f"{probe:g}s, and the last call waited {_BUSY['waited']:g}s for them; gave up early "
        f"(limit {limit:g}s). Retry in a minute, use tier=fast, or handle it yourself."
    )


def deadline_s() -> float:
    """``LLM_ROUTER_MCP_DEADLINE_S`` (default 90). 0 or negative turns the deadline off;
    an unparsable or non-finite value falls back to the default."""
    raw = os.environ.get("LLM_ROUTER_MCP_DEADLINE_S", "").strip()
    if not raw:
        return MCP_DEADLINE_DEFAULT_S
    try:
        value = float(raw)
    except ValueError:
        return MCP_DEADLINE_DEFAULT_S
    if not math.isfinite(value):
        return MCP_DEADLINE_DEFAULT_S
    return value if value > 0 else 0.0


def attempt_cap_s(model: str) -> float | None:
    """The per-attempt cap for ``model`` inside a deadline-bound MCP call, else None.

    Local (``ollama/``) models only, the incident class (a busy Ollama queue). A cloud
    attempt is bounded by the overall deadline alone (D-154)."""
    state = _STATE.get()
    if state is None or state.deadline_s <= 0 or not str(model).startswith("ollama/"):
        return None
    return state.deadline_s * ATTEMPT_SHARE


def attach_trace(attempts: list[str]) -> None:
    """Register the router's live attempt list for this MCP call (no-op outside one).

    The list object is kept, not copied: the router appends to it as it walks the chain,
    so the models tried are known even when the deadline cancels the walk half way."""
    state = _STATE.get()
    if state is not None:
        state.traces.append(attempts)


def _tried(state: _CallState) -> list[str]:
    try:
        from llm_router.quality_feedback import is_skip_marker
    except Exception:  # noqa: BLE001 -- the error text must not fail on a missing helper
        def is_skip_marker(entry: str) -> bool:
            return False
    out: list[str] = []
    for trace in state.traces:
        for model in list(trace):
            if isinstance(model, str) and not is_skip_marker(model) and model not in out:
                out.append(model)
    return out


def _unexpected_types() -> tuple[type, ...]:
    """mcp's crash wrapper type(s): ``UnexpectedToolError`` on mcp >= 2.2, none on 2.0."""
    from mcp.server.mcpserver import exceptions

    t = getattr(exceptions, "UnexpectedToolError", None)
    return (t,) if isinstance(t, type) else ()


def _tool_crash(exc: BaseException) -> BaseException | None:
    """The exception a tool crashed with, under ``Tool.run``'s ``ToolError`` wrapper, or None
    when mcp's own text is already the readable one: an unknown tool (no ``__cause__``), a
    deliberate ``ToolError``/``ResourceError`` from the tool, or argument validation."""
    from mcp.server.mcpserver.exceptions import ResourceError, ToolError
    from pydantic import ValidationError

    unexpected = _unexpected_types()
    cause = exc.__cause__
    for _ in range(10):
        if cause is None:
            return None
        if unexpected and isinstance(cause, unexpected):
            cause = cause.__cause__  # a nested tool or resource crash: keep unwrapping
            continue
        if isinstance(cause, (ToolError, ResourceError, ValidationError)):
            return None
        return cause
    return None


def error_text(exc: BaseException, tried: list[str]) -> str:
    """``llm-router error: <ExceptionType>: <message>`` (+ ``tried: ...`` when known).

    The message is secret-scrubbed before it reaches the client. F4's ``neutralise()``
    (tool-call text) is to be applied here too once #530 merges (D-154 follow-up)."""
    from llm_router.secret_scrubber import scrub_text

    message = scrub_text(str(exc)).strip() or "(no message)"
    if len(message) > _MAX_MESSAGE_CHARS:
        message = message[:_MAX_MESSAGE_CHARS] + " ..."
    text = f"llm-router error: {type(exc).__name__}: {message}"
    if tried:
        text += "\ntried: " + ", ".join(tried)
    return text


def deadline_text(limit: float, tried: list[str]) -> str:
    models = ", ".join(tried) if tried else "no model attempt recorded"
    return (
        f"llm-router error: deadline {limit:g}s exceeded; tried: {models}; "
        "local models may be busy (Ollama queue) — retry or use tier=fast"
    )


def _result(text: str) -> Any:
    from mcp.types import CallToolResult, TextContent

    return CallToolResult(content=[TextContent(type="text", text=text)], is_error=True)


def _record(code: str, exc: BaseException | None, detail: str) -> None:
    """Ids only in the log: the tool name and exception type, never the message or prompt."""
    try:
        from llm_router import failopen

        failopen.record(code, exc, detail=detail)
        from llm_router.logging import get_logger

        get_logger("llm_router.mcp_door").warning(
            "mcp_tool_error", code=code, exc_type=type(exc).__name__ if exc else None, detail=detail,
        )
    except Exception:  # noqa: BLE001 -- telemetry never changes the reply
        pass


def _debug_traceback(name: str, exc: BaseException) -> None:
    """The crash's traceback at DEBUG (D-154): mcp 2.2 no longer logs it once the door turns
    the crash into a result. Traceback text only (no frame locals), secret-scrubbed."""
    try:
        from llm_router.logging import get_logger
        from llm_router.secret_scrubber import scrub_text

        text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        get_logger("llm_router.mcp_door").debug("mcp_tool_traceback", tool=name, traceback=scrub_text(text))
    except Exception:  # noqa: BLE001 -- telemetry never changes the reply
        pass


async def run_door(name: str, call: Awaitable[Any]) -> Any:
    """Await one MCP tool call (``call``) under the door's contract; see the module doc."""
    from mcp.server.mcpserver.exceptions import ToolError

    limit = deadline_s() if name in DEADLINE_TOOLS else 0.0
    state = _CallState(limit)
    token = _STATE.set(state)
    watchdog = None
    started = time.monotonic()
    try:
        if limit <= 0:
            return await call
        timer = asyncio.timeout(limit)
        try:
            async with timer:
                if busy_window_open() and BUSY_PROBE_S < limit:
                    loop = asyncio.get_running_loop()

                    def _give_up_early() -> None:
                        # D: a still-local call in the busy window is given up at once; one
                        # that has reached a cloud model is left to its deadline.
                        if _all_local(_tried(state)):
                            state.gave_up_early = True
                            timer.reschedule(loop.time())

                    watchdog = loop.call_later(BUSY_PROBE_S, _give_up_early)
                return await call
        except TimeoutError:
            if not timer.expired():
                raise
            tried = _tried(state)
            if state.gave_up_early:
                _record("CHZ-FO-MCP-LOCAL-BUSY", None,
                        f"tool={name} probe_s={BUSY_PROBE_S:g} attempts={len(tried)}")
                return _result(busy_text(limit, BUSY_PROBE_S, tried))
            if _all_local(tried):  # D: remember it, so the next local calls do not each wait it out
                _BUSY["until"] = time.monotonic() + BUSY_WINDOW_S
                _BUSY["waited"] = round(time.monotonic() - started, 1)
            _record("CHZ-FO-MCP-DEADLINE", None,
                    f"tool={name} deadline_s={limit:g} attempts={len(tried)}")
            return _result(deadline_text(limit, tried))
    except ToolError as exc:  # UnexpectedToolError (mcp >= 2.2) is a ToolError too
        crash = _tool_crash(exc)
        if crash is None:
            raise
        _record("CHZ-FO-MCP-TOOL-ERROR", crash, f"tool={name}")
        _debug_traceback(name, crash)
        stale = stale_reason()  # C: a crash in a server older than its package says so
        if stale:
            _record("CHZ-FO-MCP-STALE-SERVER", crash, f"tool={name}")
            return _result(stale_text(stale, crash, _tried(state)))
        return _result(error_text(crash, _tried(state)))
    finally:
        if watchdog is not None:
            watchdog.cancel()
        _STATE.reset(token)
