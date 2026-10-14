"""In-process routing SDK — ``from llm_router import route``.

For Python agents that prefer a direct call over the HTTP gateway. Same router
(``build_chain`` + ``execute_chain``), same metering into ``usage.db``, no server:

    from llm_router import route
    r = route("Summarize this 10-K risk section ...")
    print(r.text, "via", r.model)
    r = route("Explain further", messages=[{"role": "user", "content": "What is routing?"}])

Returns a :class:`RouteResult`. Raises :class:`RoutingError` if the model chain
is exhausted (so callers can fall back to a direct provider if they wish).
"""
from __future__ import annotations

from dataclasses import dataclass


class RoutingError(RuntimeError):
    """All models in the chain failed to produce a response."""


@dataclass(frozen=True)
class RouteResult:
    text: str
    model: str          # "provider/model", e.g. "gemini/gemini-2.5-flash"
    provider: str
    input_tokens: int
    output_tokens: int
    latency_ms: int

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


_PROCESS_SESSION: str | None = None


def _sdk_session_id(session_id: str | None) -> str:
    """The caller's session id when it is an id, else one generated once per process.

    P1.10: the SDK used to stamp the literal ``"sdk"`` on every row, which the ledger
    refuses as a placeholder (NULL session). A generated id keeps one process's calls
    together and is never mistaken for a Claude Code session (``sdk-`` prefix)."""
    global _PROCESS_SESSION
    from llm_router.call_identity import ledger_session_id

    sid = ledger_session_id(session_id)
    if sid:
        return sid
    if _PROCESS_SESSION is None:
        import uuid

        _PROCESS_SESSION = f"sdk-{uuid.uuid4().hex[:12]}"
    return _PROCESS_SESSION


def route(prompt: str, *, task_type: str | None = None,
          complexity: str | None = None, timeout: int = 150,
          session_id: str | None = None, task_id: str | None = None,
          messages: list[dict] | None = None) -> RouteResult:
    """Route one prompt through LLM Router and return the answer + routing metadata.

    task_type / complexity are inferred from the prompt when omitted. ``session_id`` and
    ``task_id`` (ids, ``[A-Za-z0-9_.:-]{1,128}``) name the caller's session and request on
    the ledger rows; omitted, the session is generated once per process and the task per call.

    messages is the conversation so far as {"role", "content"} chat dicts,
    excluding prompt, the current request, which is sent verbatim and never
    trimmed. build_pack drops a trailing user message equal to prompt. None keeps
    existing behavior. Supplied history is packed via door="sdk" per provider
    attempt and sent only when session_store.allows_session_content allows it at
    call time (only local/ollama are privacy-local). Disallowed attempts get just
    the request; leading assistant turns are dropped for API providers. Pack
    failures record CHZ-FO-ROUTER-SDK-PACK and send the plain prompt. A hash of
    messages isolates semantic cache entries by conversation.

    With ``messages`` the call goes through ``route_and_call`` as a text
    completion, bounded by ``timeout`` (its wall-clock cap); file and tool tasks
    do not get the local agent loop that the no-``messages`` path uses. A budget
    refusal raises ``BudgetExceededError`` (never ``RoutingError``), so a caller
    cannot fall back around a spend cap.
    """
    from llm_router import call_identity

    sid = _sdk_session_id(session_id)
    with call_identity.scope(sid, task_id=task_id):
        if messages is not None:
            if not isinstance(messages, list) or any(
                not isinstance(message, dict)
                or not isinstance(message.get("role"), str)
                or not isinstance(message.get("content"), str)
                for message in messages
            ):
                raise ValueError("messages must be a list of dicts with string role/content")
            return _route_messages(prompt, messages, task_type, complexity, sid, timeout)
        return _route(prompt, task_type=task_type, complexity=complexity,
                      timeout=timeout, session_id=sid)


def _route(prompt: str, *, task_type: str | None, complexity: str | None,
           timeout: int, session_id: str) -> RouteResult:
    # Lazy imports keep ``import llm_router`` cheap.
    from llm_router.gateway import _classify
    from llm_router.hooks.chain_builder import build_chain, get_current_pressure, needs_claude_tools
    from llm_router.hooks.direct_executor import execute_agent, execute_chain

    if not prompt or not prompt.strip():
        raise ValueError("prompt is empty")
    if not task_type or not complexity:
        _t, _c = _classify(prompt)
        task_type, complexity = task_type or _t, complexity or _c

    zone, _pct = get_current_pressure()
    # File/local tasks → agent-loop (local tool-calling model); Q&A → text call.
    # The agent loop needs a capable tool-caller, so bias its chain to the coder tier.
    if needs_claude_tools(prompt, task_type):
        result = execute_agent(prompt, build_chain("complex", zone, "code"),
                               timeout=max(timeout, 180))
    else:
        result = execute_chain(prompt, build_chain(complexity, zone, task_type),
                               task_type, timeout=timeout)
    if result is None:
        raise RoutingError("LLM Router routing failed — model chain exhausted")

    try:  # meter, like the gateway/hook paths
        from llm_router.hooks.savings_logger import log_direct_savings, log_direct_to_db
        log_direct_to_db(result=result, prompt=prompt, task_type=task_type,
                         complexity=complexity, classifier_type="sdk", session_id=session_id)
        log_direct_savings(result=result, task_type=task_type, complexity=complexity,
                           session_id=session_id, host="sdk")
    except Exception:
        pass

    return RouteResult(
        text=result.text,
        model=f"{result.model.provider}/{result.model.model}",
        provider=result.model.provider,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        latency_ms=int(result.latency_ms or 0),
    )


def _run_coro(coro):
    """Run a coroutine synchronously, preserving caller identity in a worker."""
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    from concurrent.futures import ThreadPoolExecutor
    from contextvars import copy_context

    context = copy_context()
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(context.run, asyncio.run, coro).result()


async def _drained(coro):
    """Await ``coro``, then the turn's fire-and-forget tasks (receipts, savings
    rows) before ``asyncio.run`` closes the loop and would cancel them."""
    from llm_router.router import drain_bg_tasks

    try:
        return await coro
    finally:
        await drain_bg_tasks()


def _route_messages(prompt: str, messages: list[dict], task_type: str | None,
                    complexity: str | None, session_id: str, timeout: int) -> RouteResult:
    """Route caller history within the SDK's existing identity scope."""
    from llm_router.gateway import _classify
    from llm_router.router import route_and_call
    from llm_router.types import BudgetExceededError, TaskType

    if not prompt or not prompt.strip():
        raise ValueError("prompt is empty")
    if not task_type or not complexity:
        _t, _c = _classify(prompt)
        task_type, complexity = task_type or _t, complexity or _c
    try:
        tt = TaskType(task_type)
    except ValueError:
        tt = TaskType.QUERY
    try:
        resp = _run_coro(_drained(route_and_call(
            tt, prompt, complexity_hint=complexity, messages=messages,
            max_wall_clock_seconds=timeout,
        )))
    except BudgetExceededError:
        raise  # a refusal, not an exhausted chain
    except (RuntimeError, ValueError, TimeoutError) as exc:
        raise RoutingError(f"LLM Router routing failed: {exc}") from exc
    try:  # meter, like the no-messages path and the gateway
        from llm_router.route_server import _log_route_savings
        _log_route_savings(resp, tt.value, complexity or resp.complexity or "moderate", "sdk")
    except Exception:
        pass
    return RouteResult(
        text=resp.content,
        model=resp.model if "/" in resp.model else f"{resp.provider}/{resp.model}",
        provider=resp.provider,
        input_tokens=resp.input_tokens,
        output_tokens=resp.output_tokens,
        latency_ms=int(resp.latency_ms or 0),
    )
