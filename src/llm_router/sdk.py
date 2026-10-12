"""In-process routing SDK — ``from llm_router import route``.

For Python agents that prefer a direct call over the HTTP gateway. Same router
(``build_chain`` + ``execute_chain``), same metering into ``usage.db``, no server:

    from llm_router import route
    r = route("Summarize this 10-K risk section ...")
    print(r.text, "via", r.model)

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
          session_id: str | None = None, task_id: str | None = None) -> RouteResult:
    """Route one prompt through LLM Router and return the answer + routing metadata.

    task_type / complexity are inferred from the prompt when omitted. ``session_id`` and
    ``task_id`` (ids, ``[A-Za-z0-9_.:-]{1,128}``) name the caller's session and request on
    the ledger rows; omitted, the session is generated once per process and the task per call.
    """
    from llm_router import call_identity

    sid = _sdk_session_id(session_id)
    with call_identity.scope(sid, task_id=task_id):
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
