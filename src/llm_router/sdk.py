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

    ``messages`` is the conversation so far as ``{"role", "content"}`` chat dicts,
    excluding ``prompt`` (the current request, sent verbatim and never trimmed; a
    trailing copy of it in ``messages`` is dropped). It feeds the context pack
    (``build_pack(door="sdk")``) inside the same ``build_chain`` + ``execute_chain``
    (or agent-loop) call the no-``messages`` path makes (D-98): model choice, agent
    loop, ``timeout`` and the ``host="sdk"`` ledger rows are the same with or
    without it. The history is sent only when ``session_store.allows_session_content``
    allows the most external provider the executor will call (``claude`` entries are
    never called from here; the agent loop calls ollama only), since a later fallback
    model gets the same context (privacy mode "local" = on-device providers only). A
    pack failure records ``CHZ-FO-SDK-PACK`` and sends the request alone. As without
    ``messages``, ``timeout`` caps each model call, and a spend cap skips paid
    providers (``RoutingError`` once the chain is exhausted).
    """
    from llm_router import call_identity

    if messages is not None and (not isinstance(messages, list) or any(
        not isinstance(message, dict)
        or not isinstance(message.get("role"), str)
        or not isinstance(message.get("content"), str)
        for message in messages
    )):
        raise ValueError("messages must be a list of dicts with string role/content")
    sid = _sdk_session_id(session_id)
    with call_identity.scope(sid, task_id=task_id):
        return _route(prompt, task_type=task_type, complexity=complexity,
                      timeout=timeout, session_id=sid, messages=messages)


def _called(chain, *, agent: bool):
    """The chain entries the executor will actually call. ``execute_chain`` skips
    ``claude`` (it cannot be called from here); the agent loop runs ollama only."""
    if agent:
        return [m for m in chain if m.provider == "ollama"]
    return [m for m in chain if m.provider != "claude"]


def _privacy_target(chain) -> str:
    """The most external provider the chain may reach: every fallback model gets
    the same context, so the privacy gate must hold for the worst of them (D-91/D-95)."""
    from llm_router.session_store import LOCAL_PROVIDERS

    return next((m.provider for m in chain if m.provider not in LOCAL_PROVIDERS),
                chain[0].provider if chain else "ollama")


def _chat_turns(recent: list[dict]) -> list[dict]:
    """The pack's turns in a shape every chat API takes: user/assistant roles only
    (another role becomes a user turn labelled with it), consecutive same-role turns
    merged, and no leading assistant turn."""
    out: list[dict] = []
    for m in recent:
        role, content = m["role"], m["content"]
        if role not in ("user", "assistant"):
            role, content = "user", f"[{role}] {content}"
        if out and out[-1]["role"] == role:
            out[-1] = {"role": role, "content": f"{out[-1]['content']}\n\n{content}"}
        elif out or role == "user":
            out.append({"role": role, "content": content})
    return out


def _pack_window(spec) -> int:
    """The pack's window for the first model the executor calls: a local model's
    num_ctx; a cloud model's registry window capped at ``CLOUD_WINDOW_CAP`` (D-101);
    else the local default request size, as before D-101."""
    from llm_router.hooks.direct_executor import _local_num_ctx

    if spec.provider != "ollama":
        from llm_router.context_pack import CLOUD_WINDOW_CAP
        from llm_router.model_registry import context_window

        known = context_window(f"{spec.provider}/{spec.model}")
        if known:
            return min(known, CLOUD_WINDOW_CAP)
    return _local_num_ctx(spec.model) or 8192


def _pack_kwargs(prompt: str, messages: list[dict] | None, chain,
                 session_id: str, *, agent: bool = False) -> tuple[str, dict]:
    """(request, extra executor kwargs) for ``messages``; ``(prompt, {})`` without them.

    The executor kwargs carry the pack as ``history`` (text chain) or folded into
    ``context`` (agent loop, which takes no history). ``okf_inject`` stays on: the
    SDK passes no project root, so the pack has no project layer and the executor's
    own OKF injection is the only one (D-43(e))."""
    called = _called(chain, agent=agent)
    if messages is None or not called:
        return prompt, {}
    try:
        from llm_router.context_pack import build_pack

        pack = build_pack(
            prompt, messages, session_id=session_id,
            target_window=_pack_window(called[0]),
            door="sdk", target_provider=_privacy_target(called),
        )
    except Exception as exc:  # noqa: BLE001 - fail open to the plain request
        from llm_router import failopen

        failopen.record("CHZ-FO-SDK-PACK", exc)
        return prompt, {}
    recent = _chat_turns(list(pack.recent))
    rendered = "\n\n".join(f"[{m['role']}]\n{m['content']}" for m in recent)
    agent_context = "\n\n".join(p for p in (pack.system, rendered) if p) or None
    return pack.request, {"context": pack.system or None, "history": recent or None,
                          "agent_context": agent_context}


def _route(prompt: str, *, task_type: str | None, complexity: str | None,
           timeout: int, session_id: str, messages: list[dict] | None = None) -> RouteResult:
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
        chain = build_chain("complex", zone, "code")
        request, packed = _pack_kwargs(prompt, messages, chain, session_id, agent=True)
        extra = {"context": packed["agent_context"]} if packed else {}
        result = execute_agent(request, chain, timeout=max(timeout, 180), **extra)
    else:
        chain = build_chain(complexity, zone, task_type)
        request, packed = _pack_kwargs(prompt, messages, chain, session_id)
        extra = {"context": packed["context"], "history": packed["history"]} if packed else {}
        result = execute_chain(request, chain, task_type, timeout=timeout, **extra)
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
