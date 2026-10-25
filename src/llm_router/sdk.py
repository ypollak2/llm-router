"""In-process routing SDK — ``from llm_router import Router`` (or ``route``).

For Python agents that prefer a direct call over the HTTP gateway. Same router
(``build_chain`` + ``execute_chain``, D-98), same metering into ``usage.db``, no server:

    from llm_router import Router
    router = Router(budget_usd=0.50)
    r = router.run("Summarize this 10-K risk section ...")
    print(r.text, "via", r.model, f"${r.cost_usd:.4f}")
    r = await router.arun("Explain further",
                          messages=[{"role": "user", "content": "What is routing?"}])

``route(prompt, ...)`` is the one-call form (a shim over ``Router``). Both return a
:class:`RouteResult`. :class:`RoutingError` means the model chain is exhausted (so callers
can fall back to a direct provider if they wish); :class:`BudgetExceeded` means the
Router's ``budget_usd`` is spent.
"""
from __future__ import annotations

import asyncio
import functools
import math
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

from llm_router.agents.budget import BudgetEnvelope, BudgetExceeded

if TYPE_CHECKING:
    from llm_router.engine import Decision

__all__ = ["BudgetExceeded", "RouteResult", "Router", "RoutingError", "route"]


class RoutingError(RuntimeError):
    """All models in the chain failed to produce a response."""


@functools.lru_cache(maxsize=1)
def _secret_refusal_class():
    """``SecretRefused``: a :class:`RoutingError` that is also a ``local_privacy.SecretLocalOnlyRefused`` (category
    and place on ``.hits`` / ``.categories``), so a caller can catch either. Built on first use: ``import llm_router``
    stays cheap."""
    from llm_router.local_privacy import SecretLocalOnlyRefused

    class SecretRefused(RoutingError, SecretLocalOnlyRefused):
        """LC-12h: the request holds a secret and no loopback local model is in the chain; nothing was sent."""
    return SecretRefused


@dataclass(frozen=True)
class RouteResult:
    text: str
    model: str          # "provider/model", e.g. "gemini/gemini-2.5-flash"
    provider: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    #: The engine's Decision for the prompt (door ``sdk``) when ``LLM_ROUTER_ENGINE_MODE=decide``;
    #: None in shadow/off mode (the gateway rules routed) or when task type and complexity were
    #: both given. An explicit ``tier`` / ``task_type`` overrides its fields for routing.
    decision: Decision | None = None
    session_id: str = ""    # the ledger session the call's rows carry
    task_id: str = ""       # the ledger task id the call's rows carry
    cost_usd: float = 0.0   # list-price cost of the answering model (``savings_logger._cost_for``)

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


#: Router ``tier`` -> the complexity the chain is built for. Mirrors ``engine._TIER_COMPLEXITY``
#: (a test pins the two).
_TIER_COMPLEXITY = {"haiku": "simple", "sonnet": "moderate", "opus": "complex"}


def _check_messages(messages) -> None:
    if messages is not None and (not isinstance(messages, list) or any(
        not isinstance(message, dict)
        or not isinstance(message.get("role"), str)
        or not isinstance(message.get("content"), str)
        for message in messages
    )):
        raise ValueError("messages must be a list of dicts with string role/content")


class Router:
    """A routing client for one agent: its session, its spend cap, its calls.

    ``session`` is the caller's session id (``[A-Za-z0-9_.:-]{1,128}``) stamped on every ledger
    row; omitted, one id is generated per process (``sdk-...``). ``budget_usd`` caps what this
    Router may spend: before each call it raises :class:`BudgetExceeded` once the consumed cost
    reaches the cap, and after each call it adds the answering model's list-price cost. The cap is
    checked before a call, not reserved, so a call that starts under the cap may end over it, and
    calls already in flight when the cap is reached are not stopped. ``project`` and ``policy`` are
    stored on the Router only: per-project routing is A.4 and named policies come later; neither
    changes a route today.

    ``run`` is synchronous; ``arun`` runs ``run`` in a worker thread (``asyncio.to_thread``, which
    copies the caller's contextvars) so an event loop is never blocked by a model call.
    """

    def __init__(self, session: str | None = None, budget_usd: float | None = None,
                 project: str | None = None, policy: str | None = None) -> None:
        if budget_usd is not None:
            if isinstance(budget_usd, bool) or not isinstance(budget_usd, (int, float)) \
                    or not math.isfinite(budget_usd) or budget_usd < 0:
                raise ValueError("budget_usd must be a finite number >= 0")
        self.session_id = _sdk_session_id(session)
        self.project = project
        self.policy = policy
        self._lock = threading.Lock()
        self._budget = None if budget_usd is None else BudgetEnvelope(cap_usd=float(budget_usd))

    @property
    def budget(self) -> BudgetEnvelope | None:
        """The current envelope (cap and consumed), or None without ``budget_usd``."""
        with self._lock:
            return self._budget

    def run(self, prompt: str, *, context: str | None = None, task: str | None = None,
            parent: str | None = None, tier: str | None = None,
            messages: list[dict] | None = None, allow_cloud: bool = False) -> RouteResult:
        """Route one prompt and return the answer + routing metadata.

        ``context`` is extra system context for the model. ``task`` is the caller's task id for
        the ledger rows (omitted: the session's current turn, else generated). ``tier``
        (``haiku`` / ``sonnet`` / ``opus``) forces the complexity the chain is built for
        (simple / moderate / complex). ``messages`` is the conversation so far (see
        :func:`route`). ``parent`` (the parent session, for agent lineage) is accepted but not
        recorded yet: lineage lands in A.3. Raises :class:`BudgetExceeded` when the budget is spent
        and :class:`RoutingError` when the chain is exhausted.

        A request that holds a high-precision secret (LC-12h) runs on a loopback local model only; with none in the
        chain it raises a :class:`RoutingError` that names the category and the remedy. ``allow_cloud=True`` (or the
        prompt prefix ``cloud:``) sends it as ordinary traffic; only ``prompt`` and ``context`` are scanned, not
        ``messages``.
        """
        del parent  # A.3 records lineage; accepted now so callers can pass it
        return self._call(prompt, context=context, task=task, tier=tier, messages=messages,
                          allow_cloud=allow_cloud)

    async def arun(self, prompt: str, *, context: str | None = None, task: str | None = None,
                   parent: str | None = None, tier: str | None = None,
                   messages: list[dict] | None = None, allow_cloud: bool = False) -> RouteResult:
        """``run`` in a worker thread: the event loop keeps running while the model answers."""
        return await asyncio.to_thread(self.run, prompt, context=context, task=task,
                                       parent=parent, tier=tier, messages=messages,
                                       allow_cloud=allow_cloud)

    def _check_budget(self) -> None:
        with self._lock:
            env = self._budget
            if env is not None and env.exhausted:  # consumed >= cap
                raise BudgetExceeded(session_id=self.session_id, cap_usd=env.cap_usd,
                                     consumed_usd=env.consumed_usd, proposed_usd=0.0)

    def _consume(self, cost_usd: float) -> None:
        with self._lock:
            if self._budget is not None:
                self._budget = self._budget.consume(max(0.0, float(cost_usd or 0.0)))

    def _call(self, prompt: str, *, context: str | None = None, task: str | None = None,
              tier: str | None = None, messages: list[dict] | None = None, allow_cloud: bool = False,
              task_type: str | None = None, complexity: str | None = None,
              timeout: int = 150) -> RouteResult:
        from llm_router import call_identity

        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt is empty")
        _check_messages(messages)
        if context is not None and not isinstance(context, str):
            raise ValueError("context must be a string")
        if tier is not None:
            if tier not in _TIER_COMPLEXITY:
                raise ValueError(f"tier must be one of {', '.join(_TIER_COMPLEXITY)}")
            complexity = _TIER_COMPLEXITY[tier]
        self._check_budget()
        with call_identity.scope(self.session_id, task_id=task) as (task_id, _trace):
            result = _route(prompt, task_type=task_type, complexity=complexity, timeout=timeout,
                            session_id=self.session_id, task_id=task_id, messages=messages,
                            context=context, allow_cloud=allow_cloud)
        self._consume(result.cost_usd)
        return result


def route(prompt: str, *, task_type: str | None = None,
          complexity: str | None = None, timeout: int = 150,
          session_id: str | None = None, task_id: str | None = None,
          messages: list[dict] | None = None, allow_cloud: bool = False) -> RouteResult:
    """Route one prompt through LLM Router and return the answer + routing metadata.

    A shim over :class:`Router` (no budget): ``Router(session=session_id)`` routes the call.

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
    return Router(session=session_id)._call(
        prompt, task=task_id, messages=messages, task_type=task_type,
        complexity=complexity, timeout=timeout, allow_cloud=allow_cloud)


def _decide(prompt: str, *, session_id: str | None = None,
            task_id: str | None = None) -> tuple[str, str, Decision | None]:
    """(task_type, complexity, Decision or None) for the SDK door.

    ``LLM_ROUTER_ENGINE_MODE=decide``: the engine decides (``engine_door.decision(door="sdk")``,
    the same ``engine.decide`` the MCP door calls, so the same text gets the same Decision).
    Otherwise the gateway rules route (``classify_signals(GATEWAY_POLICY)``, as before) and the
    engine runs after them in shadow with door ``sdk`` (D-72)."""
    from llm_router import engine_door
    from llm_router.classify import GATEWAY_POLICY, classify_signals

    d = engine_door.decision(prompt, door="sdk", session_id=session_id, task_id=task_id)
    if d is not None:
        return d.task_type, d.complexity, d
    s = classify_signals(prompt, GATEWAY_POLICY)
    task_type, complexity = s.task_type.value, s.complexity.value
    engine_door.shadow(prompt, door="sdk", rules_task=task_type, rules_complexity=complexity)
    return task_type, complexity, None


def _join(*parts: str | None) -> str | None:
    return "\n\n".join(p for p in parts if p) or None


def _cost_usd(result) -> float:
    """The answering model's list-price cost; 0.0 (recorded) if pricing fails."""
    try:
        from llm_router.hooks.savings_logger import _cost_for

        return float(_cost_for(result.model.provider, result.model.model,
                               result.input_tokens, result.output_tokens))
    except Exception as exc:  # noqa: BLE001 - pricing never breaks a routed answer
        from llm_router import failopen

        failopen.record("CHZ-FO-SDK-COST", exc)
        return 0.0


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


def _secret_door(prompt: str, messages: list[dict] | None, context: str | None, allow_cloud: bool = False):
    """LC-12h / D-28 A for the SDK door: ``(prompt without the `cloud:` override, chain filter)``.

    A request with a high-precision secret may only run on a loopback, non-cloud Ollama model (the chain filter
    keeps those, and ``execute_chain`` / ``execute_agent`` then call it direct, no proxy); with none left it is
    refused with the category and the remedy. The `cloud:` prefix overrides, logged as a count."""
    from llm_router import local_privacy as lp
    prompt, override = lp.split_override(prompt)
    override = lp.override_requested(prefix=override, flag=allow_cloud)
    # Only the new request and the explicit ``context`` are scanned, never ``messages`` (earlier turns): see SECURITY.md.
    del messages
    hits = lp.scan_secrets(prompt, None, context)
    if not hits:
        return prompt, lambda chain: chain
    if override:
        for h in hits:
            lp.count_secret_event("overridden", h.label)
        return prompt, lambda chain: chain

    def only_local(chain):
        from llm_router.hooks.direct_executor import _get_ollama_url
        from llm_router.resolver.inventory import is_ollama_cloud_model
        loopback = lp.is_loopback_base(_get_ollama_url())
        kept = [m for m in chain if m.provider == "ollama" and loopback and not is_ollama_cloud_model(m.model)]
        if not kept:
            for h in hits:
                lp.count_secret_event("refused", h.label)
            raise _secret_refusal_class()(lp.refusal_message(
                hits, why=None if loopback else "the configured Ollama endpoint is not loopback"), hits)
        return kept
    return prompt, only_local


def _route(prompt: str, *, task_type: str | None, complexity: str | None,
           timeout: int, session_id: str, task_id: str = "",
           messages: list[dict] | None = None, context: str | None = None,
           allow_cloud: bool = False) -> RouteResult:
    # Lazy imports keep ``import llm_router`` cheap.
    from llm_router.hooks.chain_builder import build_chain, get_current_pressure, needs_claude_tools
    from llm_router.hooks.direct_executor import execute_agent, execute_chain

    prompt, _door = _secret_door(prompt, messages, context, allow_cloud)

    decision = None
    _layer = "override"  # W0-1: the caller named both, so no layer classified
    if not task_type or not complexity:
        _t, _c, decision = _decide(prompt, session_id=session_id, task_id=task_id)
        task_type, complexity = task_type or _t, complexity or _c
        _layer = decision.layer if decision is not None else "rules"

    zone, _pct = get_current_pressure()
    # File/local tasks → agent-loop (local tool-calling model); Q&A → text call.
    # The agent loop needs a capable tool-caller, so bias its chain to the coder tier.
    if needs_claude_tools(prompt, task_type):
        chain = _door(build_chain("complex", zone, "code"))
        request, packed = _pack_kwargs(prompt, messages, chain, session_id, agent=True)
        agent_context = _join(context, packed.get("agent_context"))
        extra = {"context": agent_context} if agent_context else {}
        result = execute_agent(request, chain, timeout=max(timeout, 180), **extra)
    else:
        chain = _door(build_chain(complexity, zone, task_type, qa_last_resort=True))
        request, packed = _pack_kwargs(prompt, messages, chain, session_id)
        extra = {"context": packed["context"], "history": packed["history"]} if packed else {}
        if context:
            extra["context"] = _join(context, extra.get("context"))
        result = execute_chain(request, chain, task_type, timeout=timeout, **extra)
    if result is None:
        raise RoutingError("LLM Router routing failed — model chain exhausted")
    cost_usd = _cost_usd(result)

    try:  # meter, like the gateway/hook paths
        from llm_router.hooks.savings_logger import log_direct_savings, log_direct_to_db
        log_direct_to_db(result=result, prompt=prompt, task_type=task_type,
                         complexity=complexity, classifier_type="sdk", session_id=session_id, door="sdk",
                         layer=_layer, confidence=decision.confidence if decision is not None else None)
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
        decision=decision,
        session_id=session_id,
        task_id=task_id or "",
        cost_usd=cost_usd,
    )
