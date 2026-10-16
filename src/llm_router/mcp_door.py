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

Light on purpose: stdlib only at import; ``mcp`` is imported inside ``run_door``.
"""
from __future__ import annotations

import asyncio
import math
import os
import traceback
from contextvars import ContextVar
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

    __slots__ = ("deadline_s", "traces")

    def __init__(self, deadline_s: float) -> None:
        self.deadline_s = deadline_s
        self.traces: list[list[str]] = []


_STATE: ContextVar[_CallState | None] = ContextVar("llm_router_mcp_door", default=None)


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
    try:
        if limit <= 0:
            return await call
        timer = asyncio.timeout(limit)
        try:
            async with timer:
                return await call
        except TimeoutError:
            if not timer.expired():
                raise
            _record("CHZ-FO-MCP-DEADLINE", None,
                    f"tool={name} deadline_s={limit:g} attempts={len(_tried(state))}")
            return _result(deadline_text(limit, _tried(state)))
    except ToolError as exc:  # UnexpectedToolError (mcp >= 2.2) is a ToolError too
        crash = _tool_crash(exc)
        if crash is None:
            raise
        _record("CHZ-FO-MCP-TOOL-ERROR", crash, f"tool={name}")
        _debug_traceback(name, crash)
        return _result(error_text(crash, _tried(state)))
    finally:
        _STATE.reset(token)
