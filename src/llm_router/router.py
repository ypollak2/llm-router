"""Core routing logic — selects model and executes with fallback.

This module is the central dispatch layer of llm-router. It receives a
(task_type, prompt) pair, resolves the best model chain from profiles,
enforces budget limits, and walks the chain until one model succeeds.

Text tasks are dispatched through LiteLLM (unified OpenAI-compatible SDK).
Media tasks (image/video/audio) bypass LiteLLM and call provider-specific
generation APIs directly, because LiteLLM has no media generation support.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

from llm_router import cost, media, providers
from llm_router.budget import get_budget_state
from llm_router.state import get_active_agent
from llm_router.codex_agent import CODEX_MODELS, is_codex_available, run_codex
from llm_router.gemini_cli_agent import GEMINI_MODELS, is_gemini_cli_available, run_gemini_cli
from llm_router.logging import get_logger
from llm_router.compaction import compact_structural
from llm_router.config import get_config
from llm_router.repo_config import effective_config as get_repo_config
from llm_router.context import build_context_messages, get_session_buffer
from llm_router.health import get_tracker
from llm_router.profiles import get_model_chain, provider_from_model
from llm_router.tracing import set_span_attributes, traced_span
from llm_router.types import BudgetExceededError, Complexity, LLMResponse, RoutingProfile, TaskType

# Foundational routing rule: complexity always determines the profile.
# This mapping is the single source of truth — every call through route_and_call
# honours it automatically. simple→BUDGET (Haiku/cheap), moderate→BALANCED
# (Sonnet/GPT-4o), complex→PREMIUM (Opus/o3). An explicit profile= argument
# overrides this (escape hatch for power users), but no caller should need to.
_COMPLEXITY_TO_PROFILE: dict[Complexity, RoutingProfile] = {
    Complexity.SIMPLE: RoutingProfile.BUDGET,
    Complexity.MODERATE: RoutingProfile.BALANCED,
    Complexity.COMPLEX: RoutingProfile.PREMIUM,
    Complexity.DEEP_REASONING: RoutingProfile.PREMIUM,  # Extended thinking — same chain as PREMIUM
}

log = get_logger("llm_router.router")


async def _build_and_filter_chain(
    task_type: TaskType,
    profile: RoutingProfile,
    model_override: str | None,
    complexity_hint: Complexity | str | None,
    c: Complexity,
    config,
) -> list[str]:
    """Build and filter the ordered list of candidate models to try.

    Handles override validation, subscription mode, dynamic vs. static chain
    selection, provider filtering, policy engine, Ollama/Codex injection, and dedup.

    Args:
        task_type: The task type being routed.
        profile: Resolved routing profile.
        model_override: If set, use only this model (with subscription validation).
        complexity_hint: Raw complexity hint (string or enum) for dynamic chain selection.
        c: Resolved Complexity enum (from _resolve_profile).
        config: Application config.

    Returns:
        Ordered list of model identifiers, highest priority first. May be empty.
    """
    if model_override:
        _local_prefixes = {"codex", "ollama", "gemini_cli"}
        if "/" not in model_override and model_override not in _local_prefixes:
            raise ValueError(
                f"Invalid model_override format: {model_override!r}. "
                "Use 'provider/model' format (e.g. 'openai/gpt-4o', "
                "'anthropic/claude-haiku-4-5-20251001', 'gemini/gemini-2.5-flash'). "
                "Run llm_providers() to see all available models."
            )
        if (
            config.llm_router_claude_subscription
            and model_override.startswith("anthropic/")
        ):
            log.warning(
                "model_override %r blocked in subscription mode — "
                "routing to balanced chain instead",
                model_override,
            )
            fallback_chain = [
                m for m in get_model_chain(RoutingProfile.BALANCED, task_type)
                if not m.startswith("anthropic/")
            ]
            return fallback_chain or get_model_chain(RoutingProfile.BALANCED, task_type)
        return [model_override]

    # ── Pre-fetch penalty data ────────────────────────────────────────────────
    _failure_rates: dict[str, float] | None = None
    _latency_stats: dict[str, dict] | None = None
    _acceptance_scores: dict[str, float] | None = None
    if task_type not in MEDIA_TASK_TYPES:
        try:
            from llm_router.cost import (
                get_model_acceptance_scores,
                get_model_failure_rates,
                get_model_latency_stats,
            )
            _failure_rates, _latency_stats, _acceptance_scores = await asyncio.gather(
                get_model_failure_rates(window_days=30),
                get_model_latency_stats(window_days=7),
                get_model_acceptance_scores(window_days=30),
            )
        except Exception as _penalty_err:
            log.warning(
                "Failed to fetch benchmark penalty data — model ordering will use static chain: %s",
                _penalty_err,
            )

    # ── Dynamic chain selection (v5.0) with session-start discovery ──────────────
    # At session start, discover available providers and build optimized routing tables.
    # All subsequent routing requests use these pre-built tables.
    # Fallback to static chain if dynamic routing failed to initialize.
    models_to_try = None
    
    try:
        from llm_router.dynamic_routing import get_dynamic_model_chain
        dynamic_chain = get_dynamic_model_chain(profile, task_type)
        if dynamic_chain is not None:
            models_to_try = dynamic_chain
            log.debug(
                "Using session-start dynamic routing table for %s/%s",
                profile.value, task_type.value,
            )
    except Exception as _dynroute_err:
        log.debug(
            "Dynamic routing table lookup failed: %s",
            _dynroute_err,
        )
    
    # Fall back to static chain if dynamic tables not available
    if models_to_try is None:
        models_to_try = get_model_chain(
            profile, task_type,
            failure_rates=_failure_rates,
            latency_stats=_latency_stats,
            acceptance_scores=_acceptance_scores,
        )

    if task_type not in MEDIA_TASK_TYPES:
        from llm_router.claude_usage import get_claude_pressure
        pressure = get_claude_pressure()

        # ── Provider filter (must run before injection) ───────────────────────
        available = config.available_providers
        models_to_try = [
            m for m in models_to_try
            if provider_from_model(m) in available
            or provider_from_model(m) in {"codex", "ollama", "gemini_cli"}
        ]

        # ── Repo config: block_providers + model/provider pin ─────────────────
        repo_cfg = get_repo_config()
        if repo_cfg.block_providers:
            blocked = set(repo_cfg.block_providers)
            models_to_try = [
                m for m in models_to_try
                if provider_from_model(m) not in blocked
            ]

        # ── Policy engine ─────────────────────────────────────────────────────
        from llm_router.policy import OrgPolicy, apply_policy, load_org_policy
        _org = load_org_policy()
        _merged_block = list({*_org.block_models, *repo_cfg.block_models})
        _merged_allow = list({*_org.allow_models, *repo_cfg.allow_models})
        _merged_block_prov = list({*_org.block_providers})
        _policy = OrgPolicy(
            block_providers=_merged_block_prov,
            block_models=_merged_block,
            allow_models=_merged_allow,
            task_caps=_org.task_caps,
            source=_org.source,
        )
        if _merged_block or _merged_allow:
            models_to_try, _policy_blocked = apply_policy(
                models_to_try, task_type.value, _policy,
            )

        # Model pin: prepend pinned model so it's tried first
        pinned_model = repo_cfg.model_override(task_type.value)
        pinned_provider = repo_cfg.provider_override(task_type.value)
        if pinned_model and pinned_model not in models_to_try:
            models_to_try = [pinned_model] + models_to_try
        elif pinned_provider and not pinned_model:
            pinned = [m for m in models_to_try if provider_from_model(m) == pinned_provider]
            rest   = [m for m in models_to_try if provider_from_model(m) != pinned_provider]
            models_to_try = pinned + rest

        # ── Ollama injection ──────────────────────────────────────────────────
        ollama_models = config.all_ollama_models()
        if ollama_models:
            models_to_try = ollama_models + models_to_try

        # ── Codex injection ───────────────────────────────────────────────────
        _codex_eligible_tasks = {TaskType.CODE, TaskType.ANALYZE, TaskType.GENERATE, TaskType.QUERY}
        if (
            profile != RoutingProfile.BUDGET
            and task_type in _codex_eligible_tasks
            and is_codex_available()
        ):
            codex_chain = [f"codex/{m}" for m in CODEX_MODELS[:2]]
            has_claude = any(m.startswith("anthropic/") for m in models_to_try)
            if pressure >= 0.95:
                log.debug("Codex injected at front (pressure=%.0f%%)", pressure * 100)
                models_to_try = codex_chain + models_to_try
            elif has_claude and task_type == TaskType.CODE:
                first_claude = next(
                    i for i, m in enumerate(models_to_try) if m.startswith("anthropic/")
                )
                insert_at = first_claude + 1
                log.debug("Codex injected after first Claude at index %d (CODE task)", insert_at)
                models_to_try = models_to_try[:insert_at] + codex_chain + models_to_try[insert_at:]
            elif has_claude:
                last_claude = max(
                    (i for i, m in enumerate(models_to_try) if m.startswith("anthropic/")),
                    default=-1,
                )
                insert_at = last_claude + 1
                log.debug(
                    "Codex injected after last Claude at index %d (%s task)",
                    insert_at, task_type.value,
                )
                models_to_try = models_to_try[:insert_at] + codex_chain + models_to_try[insert_at:]
            else:
                # Subscription mode: inject Codex after Ollama, before paid externals
                first_paid = next(
                    (i for i, m in enumerate(models_to_try)
                     if provider_from_model(m) not in {"ollama", "codex"}),
                    len(models_to_try),
                )
                log.debug(
                    "Codex injected before paid externals at index %d (%s task, subscription mode)",
                    first_paid, task_type.value,
                )
                models_to_try = models_to_try[:first_paid] + codex_chain + models_to_try[first_paid:]

        # ── Gemini CLI injection ──────────────────────────────────────────────
        _gemini_eligible_tasks = {TaskType.CODE, TaskType.ANALYZE, TaskType.GENERATE, TaskType.QUERY}
        if (
            profile != RoutingProfile.BUDGET
            and task_type in _gemini_eligible_tasks
            and is_gemini_cli_available()
        ):
            gemini_chain = [f"gemini_cli/{m}" for m in GEMINI_MODELS[:2]]
            has_claude = any(m.startswith("anthropic/") for m in models_to_try)
            from llm_router.claude_usage import get_claude_pressure
            pressure = get_claude_pressure()
            if pressure >= 0.95:
                log.debug("Gemini CLI injected at front (pressure=%.0f%%)", pressure * 100)
                models_to_try = gemini_chain + models_to_try
            elif has_claude and task_type == TaskType.CODE:
                first_claude = next(
                    i for i, m in enumerate(models_to_try) if m.startswith("anthropic/")
                )
                insert_at = first_claude + 1
                log.debug("Gemini CLI injected after first Claude at index %d (CODE task)", insert_at)
                models_to_try = models_to_try[:insert_at] + gemini_chain + models_to_try[insert_at:]
            elif has_claude:
                last_claude = max(
                    (i for i, m in enumerate(models_to_try) if m.startswith("anthropic/")),
                    default=-1,
                )
                insert_at = last_claude + 1
                log.debug(
                    "Gemini CLI injected after last Claude at index %d (%s task)",
                    insert_at, task_type.value,
                )
                models_to_try = models_to_try[:insert_at] + gemini_chain + models_to_try[insert_at:]
            else:
                # Subscription mode: inject Gemini CLI after Ollama/Codex, before paid externals
                first_paid = next(
                    (i for i, m in enumerate(models_to_try)
                     if provider_from_model(m) not in {"ollama", "codex", "gemini_cli"}),
                    len(models_to_try),
                )
                log.debug(
                    "Gemini CLI injected before paid externals at index %d (%s task, subscription mode)",
                    first_paid, task_type.value,
                )
                models_to_try = models_to_try[:first_paid] + gemini_chain + models_to_try[first_paid:]

        # ── Agent-context chain reordering ────────────────────────────────────
        models_to_try = _reorder_for_agent_context(
            models_to_try, get_active_agent(), c,
        )

        # ── Quality-based reordering (v6.2) ───────────────────────────────────
        # Demote models with low quality scores to the end of the chain.
        # This allows the router to learn from historical quality feedback.
        try:
            from llm_router.judge import reorder_by_quality
            models_to_try = await reorder_by_quality(models_to_try, days=7)
        except Exception as _quality_err:
            log.debug("Quality reordering skipped: %s", _quality_err)

        # Dedup: preserve free-first order, remove injected duplicates
        _seen: set[str] = set()
        models_to_try = [
            m for m in models_to_try
            if m not in _seen and not _seen.add(m)  # type: ignore[func-returns-value]
        ]

        # ── Quota-balanced reordering (v7.1.0) ─────────────────────────────────
        # QUOTA_BALANCED: dynamically reorder chain to balance usage across
        # Claude, Gemini CLI, and Codex subscription providers.
        if profile == RoutingProfile.QUOTA_BALANCED:
            try:
                from llm_router.quota_balance import (
                    get_provider_pressures,
                    get_balanced_provider_order,
                    reorder_chain_by_providers,
                )
                pressures = await get_provider_pressures()
                order = get_balanced_provider_order(pressures)
                models_to_try = reorder_chain_by_providers(models_to_try, order)
                log.debug(
                    "QUOTA_BALANCED reordering: pressures=%s, order=%s",
                    {k: f"{v:.1%}" for k, v in pressures.items()},
                    order,
                )
            except Exception as _quota_err:
                log.warning("QUOTA_BALANCED reordering failed: %s", _quota_err)

    return models_to_try


def _resolve_profile(
    profile: RoutingProfile | None,
    complexity_hint: Complexity | str | None,
    classification_data: dict | None,
    prompt: str,
    model_override: str | None,
    config,
) -> tuple[RoutingProfile, Complexity, bool]:
    """Resolve the effective routing profile, complexity, and thinking flag.

    Priority: explicit profile > complexity_hint > classification_data >
              prompt-length heuristic > config default.

    Args:
        profile: Explicit profile override from the caller, or None.
        complexity_hint: Caller-supplied complexity string or enum, or None.
        classification_data: Optional dict containing a "complexity" key.
        prompt: Raw prompt text (used only for the length heuristic fallback).
        model_override: When set, skips profile resolution entirely.
        config: Application config (provides llm_router_profile default).

    Returns:
        Tuple of (resolved_profile, effective_complexity, use_thinking).
        use_thinking is True only for DEEP_REASONING complexity.
    """
    c: Complexity = Complexity.MODERATE
    use_thinking = False

    if profile is None and not model_override:
        if complexity_hint is not None:
            if isinstance(complexity_hint, str):
                try:
                    c = Complexity(complexity_hint)
                except ValueError:
                    c = Complexity.MODERATE
            else:
                c = complexity_hint
        elif classification_data and "complexity" in classification_data:
            try:
                c = Complexity(classification_data["complexity"])
            except ValueError:
                c = Complexity.MODERATE
        else:
            # Fast heuristic — no API call, no latency.
            n = len(prompt)
            c = Complexity.SIMPLE if n < 300 else (Complexity.COMPLEX if n > 3000 else Complexity.MODERATE)
        if c == Complexity.DEEP_REASONING:
            use_thinking = True
        resolved = _COMPLEXITY_TO_PROFILE.get(c, config.llm_router_profile)
    else:
        resolved = profile or config.llm_router_profile

    return resolved, c, use_thinking


def _reorder_for_agent_context(
    models: list[str],
    agent: str | None,
    complexity: Complexity,
) -> list[str]:
    """Reorder model chain to prefer subscription-covered models for the active agent.

    Priority matrix (subscription-first ordering):
      Codex session + simple/moderate  : Ollama → Codex → Gemini CLI → rest → Claude
      Codex session + complex          : Codex → Gemini CLI → Claude → rest → Ollama
      Gemini CLI session + simple/moderate : Ollama → Gemini CLI → Codex → rest → Claude
      Gemini CLI session + complex     : Gemini CLI → Codex → Claude → rest → Ollama
      Claude Code session + simple/moderate : Ollama → Claude → Gemini CLI → rest → Codex
      Claude Code session + complex    : Claude → Gemini CLI → rest → Codex → Ollama

    Does not filter any models — every model stays in the chain, just reordered
    so the cheapest/already-paid tier is attempted first.
    """
    if agent is None:
        return models
    ollama     = [m for m in models if provider_from_model(m) == "ollama"]
    codex      = [m for m in models if provider_from_model(m) == "codex"]
    gemini_cli = [m for m in models if provider_from_model(m) == "gemini_cli"]
    claude     = [m for m in models if provider_from_model(m) == "anthropic"]
    rest       = [m for m in models if m not in set(ollama + codex + gemini_cli + claude)]
    if complexity in (Complexity.SIMPLE, Complexity.MODERATE):
        if agent == "codex":
            return ollama + codex + gemini_cli + rest + claude
        elif agent == "gemini_cli":
            return ollama + gemini_cli + codex + rest + claude
        else:  # claude_code
            return ollama + claude + gemini_cli + rest + codex
    else:  # COMPLEX / DEEP_REASONING
        if agent == "codex":
            return codex + gemini_cli + claude + rest + ollama
        elif agent == "gemini_cli":
            return gemini_cli + codex + claude + rest + ollama
        else:  # claude_code
            return claude + gemini_cli + rest + codex + ollama

# Guards the check-then-spend budget sequence so concurrent calls cannot
# both slip through the limit before either has recorded its spend.
# _pending_spend tracks in-flight estimated costs so the next caller
# sees the full committed + pending total when performing the budget check.
_budget_lock = asyncio.Lock()
_pending_spend: float = 0.0  # sum of provisional spend for all in-flight calls

# Task types routed to provider-specific media APIs instead of LiteLLM.
# LiteLLM only supports text completion; media generation requires direct
# calls to each provider's SDK (DALL-E, Flux, Runway, ElevenLabs, etc.).
MEDIA_TASK_TYPES = {TaskType.IMAGE, TaskType.VIDEO, TaskType.AUDIO}

# Allowed keys per media task type.  Caller-supplied media_params are filtered
# through this whitelist before being spread into the generator functions, so
# an MCP caller cannot inject unexpected kwargs into provider SDKs.
_ALLOWED_MEDIA_PARAMS: dict[TaskType, frozenset[str]] = {
    TaskType.IMAGE: frozenset({"size", "quality", "style", "n", "response_format"}),
    TaskType.VIDEO: frozenset({"duration", "resolution", "fps", "aspect_ratio"}),
    TaskType.AUDIO: frozenset({"voice", "speed", "format", "sample_rate"}),
}


def _filter_media_params(task_type: TaskType, params: dict | None) -> dict:
    """Return media_params filtered to the allowed keys for *task_type*."""
    if not params:
        return {}
    allowed = _ALLOWED_MEDIA_PARAMS.get(task_type, frozenset())
    return {k: v for k, v in params.items() if k in allowed}

# Substrings checked against exception messages and type names to detect
# rate-limit (HTTP 429) errors. Each provider SDK formats these differently
# (OpenAI says "Rate limit", Anthropic uses "rate_limit", etc.), so we
# check multiple markers to catch them all reliably.
_RATE_LIMIT_MARKERS = ("rate limit", "rate_limit", "429", "too many requests", "quota exceeded")
_AUTH_MARKERS = ("authentication", "401", "not logged in", "invalid api key", "incorrect api key",
                 "no auth", "unauthorized", "api key")
# Content filtering errors are provider-side policy blocks, not infrastructure failures.
# They should be silently skipped (no warning to user, no circuit-breaker trip) so the
# router tries the next model without alarming the user with a policy error message.
_CONTENT_FILTER_MARKERS = (
    "output blocked by content filtering",
    "content filtering policy",
    "content_policy_violation",
    "content filter",
    "violates our usage policy",
    "safety system",
)

# Provider → env var name, so auth errors name exactly what to set.
_PROVIDER_KEY_ENV: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "google": "GEMINI_API_KEY",
    "perplexity": "PERPLEXITY_API_KEY",
    "groq": "GROQ_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "fal": "FAL_KEY",
    "replicate": "REPLICATE_API_TOKEN",
    "elevenlabs": "ELEVENLABS_API_KEY",
    "runway": "RUNWAYML_API_SECRET",
}


def _is_auth_error(exc: Exception) -> bool:
    """Detect if an exception is an authentication (HTTP 401/403) error."""
    exc_str = str(exc).lower()
    exc_type = type(exc).__name__.lower()
    return (
        any(m in exc_str for m in _AUTH_MARKERS)
        or "authentication" in exc_type
        or "unauthorized" in exc_type
    )


def _auth_error_hint(provider: str) -> str:
    """Return a human-readable fix hint for an auth error from *provider*."""
    env_var = _PROVIDER_KEY_ENV.get(provider.lower())
    if env_var:
        return (
            f"❌  {provider} authentication failed — {env_var} is missing or invalid.\n"
            f"    Fix: run `llm-router setup` to configure it, or set {env_var} in "
            f"~/.llm-router/.env\n"
            f"    Note: Claude Code subscription covers Haiku/Sonnet/Opus — no API key needed "
            f"for those. External providers like {provider} require their own key."
        )
    return (
        f"❌  {provider} authentication failed — API key missing or invalid.\n"
        f"    Fix: run `llm-router setup` to configure your providers.\n"
        f"    Note: Claude Code subscription covers Haiku/Sonnet/Opus — no API key needed "
        f"for those. External providers require their own key."
    )


def _is_content_filter_error(exc: Exception) -> bool:
    """Detect if an exception is a provider-side content filter block (HTTP 400).

    Content filter errors are not infrastructure failures — they are policy
    decisions by the provider. We skip silently rather than tripping the
    circuit breaker, so temporary false-positives don't degrade the provider's
    health score for legitimate future calls.
    """
    exc_str = str(exc).lower()
    return any(m in exc_str for m in _CONTENT_FILTER_MARKERS)


def _is_rate_limit_error(exc: Exception) -> bool:
    """Detect if an exception is a rate-limit (HTTP 429) error from any provider.

    Checks both the exception message string and the exception class name,
    because some SDKs use dedicated exception types (e.g. ``RateLimitError``)
    while others embed the status code in a generic error message.

    Args:
        exc: The exception raised during an LLM or media API call.

    Returns:
        True if the error indicates rate limiting, False otherwise.
    """
    exc_str = str(exc).lower()
    exc_type = type(exc).__name__.lower()
    return (
        any(m in exc_str for m in _RATE_LIMIT_MARKERS)
        or "ratelimit" in exc_type
    )


def _extract_retry_after(exc: Exception) -> int | None:
    """Extract Retry-After header value from a rate-limit exception.
    
    Attempts to read the Retry-After header from LiteLLM exceptions,
    which wrap provider-specific error details. Returns the number of
    seconds to wait before retrying, or None if not available.
    
    Args:
        exc: The exception from a failed LLM call.
    
    Returns:
        The Retry-After value in seconds, or None if not found.
    """
    try:
        # Check for LiteLLM-specific error attribute
        if hasattr(exc, 'http_response'):
            headers = getattr(exc.http_response, 'headers', {})
            if 'retry-after' in headers:
                val = headers['retry-after']
                return int(val)
        # Fallback: check exception attributes for common patterns
        if hasattr(exc, '_response'):
            headers = getattr(exc._response, 'headers', {})
            if 'retry-after' in headers:
                val = headers['retry-after']
                return int(val)
    except (ValueError, TypeError, AttributeError):
        pass
    return None


async def _notify(ctx: Any | None, level: str, message: str) -> None:
    """Send a log notification to the MCP client if a context object is available.

    MCP tool handlers receive a ``ctx`` (RequestContext) that exposes
    ``ctx.info()``, ``ctx.warning()``, etc. for streaming progress back to the
    caller. When ``route_and_call`` is invoked outside an MCP handler (e.g.
    from tests or the CLI), ``ctx`` is None and this is a no-op.

    Errors are silently swallowed so that notification failures never abort
    the routing pipeline.

    Args:
        ctx: MCP RequestContext or None when called outside MCP.
        level: Log level method name on ctx (``"info"``, ``"warning"``, etc.).
        message: Human-readable progress message.
    """
    if ctx is None:
        return
    try:
        await getattr(ctx, level)(message)
    except Exception:
        pass


async def _dispatch_model_loop(
    models_to_try: list[str],
    task_type: TaskType,
    profile: RoutingProfile,
    prompt: str,
    system_prompt: str | None,
    temperature: float | None,
    max_tokens: int | None,
    media_params: dict | None,
    ctx: Any | None,
    classification_data: dict | None,
    caller_context: str | None,
    use_thinking: bool,
    correlation_id: str,
    complexity_hint: Complexity | str | None,
    c: Complexity,
    config: Any,
    route_span: Any,
    route_log: Any,
    _reservation: float,
    effective_complexity: str,
) -> LLMResponse:
    """Execute the main model dispatch loop with primary + emergency fallback chains.

    Walks through models_to_try in order, calling each until one succeeds.
    On complete failure of the primary chain, attempts emergency BUDGET fallback
    (if profile != BUDGET) to prevent total routing failure when external providers are down.

    Args:
        models_to_try: Ordered list of model IDs to attempt.
        task_type: The task type being routed.
        profile: Resolved routing profile.
        prompt: User prompt/query.
        system_prompt: Optional system message.
        temperature: Optional temperature override.
        max_tokens: Optional max tokens override.
        media_params: Media task parameters (image/video/audio).
        ctx: MCP RequestContext for progress notifications.
        classification_data: Classification metadata for logging.
        caller_context: Caller/agent context for tracing.
        use_thinking: Whether to enable extended thinking (Claude only).
        correlation_id: Request correlation ID for tracing.
        complexity_hint: Raw complexity hint from caller.
        c: Resolved Complexity enum.
        config: Application config.
        route_span: Tracing span for the route operation.
        route_log: Structured logger instance.
        _reservation: Reserved budget amount for this call.
        effective_complexity: Stringified complexity for logging.

    Returns:
        LLMResponse on success.

    Raises:
        RuntimeError: When all models in primary + emergency chains fail.
    """
    global _pending_spend
    tracker = get_tracker()
    last_error: Exception | None = None

    for attempt, model in enumerate(models_to_try, start=1):
        provider = provider_from_model(model)
        model_name = model.split("/", 1)[1] if "/" in model else model

        if not tracker.is_healthy(provider):
            await _notify(ctx, "warning", f"⚠️  {provider} unhealthy — trying next")
            log.info("Skipping unhealthy provider: %s", provider)
            route_log.warning(
                "provider_unhealthy_skip",
                correlation_id=correlation_id,
                provider=provider,
                model=model,
            )
            continue

        # Refresh provider budget state for each attempt so long fallback walks
        # do not keep routing to providers that exhausted their budget mid-chain.
        budget_state = await get_budget_state(provider)
        if budget_state.pressure >= 0.8:
            route_log.warning(
                "provider_budget_pressure_high",
                correlation_id=correlation_id,
                provider=provider,
                model=model,
                pressure=budget_state.pressure,
            )
        if budget_state.pressure >= 1.0:
            await _notify(ctx, "warning", f"⚠️  {provider} budget exhausted — trying next")
            log.info("Skipping budget-exhausted provider: %s", provider)
            last_error = BudgetExceededError(f"{provider} budget exhausted")
            continue

        await _notify(ctx, "info", f"⏳ {model_name} working...")

        try:
            with traced_span(
                "provider_call",
                tracer_name="llm_router.router",
                correlation_id=correlation_id,
                attempt=attempt,
                model=model,
                provider=provider,
                task_type=task_type,
            ) as provider_span:
                if task_type in MEDIA_TASK_TYPES:
                    response = await _call_media(task_type, provider, model_name, prompt,
                                                 _filter_media_params(task_type, media_params),
                                                 correlation_id=correlation_id)
                elif provider == "codex":
                    codex_result = await run_codex(prompt, model=model_name)
                    if not codex_result.success:
                        raise RuntimeError(f"Codex exited {codex_result.exit_code}: (response omitted)")
                    response = LLMResponse(
                        content=codex_result.content,
                        model=f"codex/{model_name}",
                        input_tokens=0,
                        output_tokens=0,
                        cost_usd=0.0,
                        latency_ms=codex_result.duration_sec * 1000,
                        provider="codex",
                    )
                elif provider == "gemini_cli":
                    gemini_result = await run_gemini_cli(prompt, model=model_name)
                    if not gemini_result.success:
                        raise RuntimeError(f"Gemini CLI exited {gemini_result.exit_code}: (response omitted)")
                    response = LLMResponse(
                        content=gemini_result.content,
                        model=f"gemini_cli/{model_name}",
                        input_tokens=0,
                        output_tokens=0,
                        cost_usd=0.0,
                        latency_ms=gemini_result.duration_sec * 1000,
                        provider="gemini_cli",
                    )
                else:
                    response = await _call_text(
                        model, prompt, system_prompt, temperature, max_tokens, task_type,
                        caller_context=caller_context,
                        use_thinking=use_thinking,
                        correlation_id=correlation_id,
                    )

                set_span_attributes(
                    provider_span,
                    response_model=response.model,
                    response_provider=response.provider,
                    cost_usd=response.cost_usd,
                    latency_ms=response.latency_ms,
                )

            tracker.record_success(provider)
            await cost.log_usage(response, task_type, profile, correlation_id=correlation_id)

            # Record Codex/Gemini CLI requests for quota tracking (v7.1.0)
            if provider == "codex":
                try:
                    from llm_router.quota_balance import record_codex_request
                    record_codex_request(config.codex_daily_limit)
                except Exception as _quota_err:
                    log.debug("Failed to record Codex request: %s", _quota_err)
            elif provider == "gemini_cli":
                try:
                    from llm_router.gemini_cli_quota import record_gemini_request
                    record_gemini_request()
                except Exception as _quota_err:
                    log.debug("Failed to record Gemini CLI request: %s", _quota_err)

            # Record spend for real-time session spend meter (v4.0)
            try:
                from llm_router.session_spend import get_session_spend
                get_session_spend().record(
                    model=model,
                    tool=task_type.value,
                    input_tokens=response.input_tokens,
                    output_tokens=response.output_tokens,
                    cost_usd=response.cost_usd,
                )
            except Exception:
                pass

            # Record exchange in session buffer for future context injection
            buf = get_session_buffer()
            buf.record("user", prompt, task_type=task_type.value)
            buf.record("assistant", response.content, task_type=task_type.value)

            # Log routing decision for quality analytics
            if classification_data:
                try:
                    await cost.log_routing_decision(
                        prompt=prompt,
                        task_type=classification_data.get("task_type", task_type.value),
                        profile=classification_data.get("profile", profile.value),
                        classifier_type=classification_data.get("classifier_type", "unknown"),
                        classifier_model=classification_data.get("classifier_model"),
                        classifier_confidence=classification_data.get("classifier_confidence", 0.0),
                        classifier_latency_ms=classification_data.get("classifier_latency_ms", 0.0),
                        complexity=classification_data.get("complexity", "moderate"),
                        recommended_model=classification_data.get("recommended_model", model),
                        base_model=classification_data.get("base_model", model),
                        was_downshifted=classification_data.get("was_downshifted", False),
                        budget_pct_used=classification_data.get("budget_pct_used", 0.0),
                        quality_mode=classification_data.get("quality_mode", "balanced"),
                        final_model=response.model,
                        final_provider=response.provider,
                        success=True,
                        input_tokens=response.input_tokens,
                        output_tokens=response.output_tokens,
                        cost_usd=response.cost_usd,
                        latency_ms=response.latency_ms,
                        reason_code=classification_data.get("reason_code"),
                        correlation_id=correlation_id,
                        response=response.content,
                        requested_complexity=classification_data.get("requested_complexity"),
                    )
                except Exception as e:
                    log.warning("Failed to log routing decision: %s", e)

            await _notify(
                ctx, "info",
                f"✅ {model_name} — {response.latency_ms:.0f}ms · ${response.cost_usd:.6f}"
            )
            route_log.info(
                "routing_decision",
                correlation_id=correlation_id,
                task_type=task_type.value,
                complexity=effective_complexity,
                profile=profile.value,
                model=response.model,
                provider=response.provider,
                cost_usd=response.cost_usd,
                latency_ms=response.latency_ms,
            )
            set_span_attributes(
                route_span,
                final_model=response.model,
                final_provider=response.provider,
                cost_usd=response.cost_usd,
                latency_ms=response.latency_ms,
                attempts=attempt,
            )

            # Daily spend alert — fire async so it never blocks the response
            _raw_limit = getattr(config, "llm_router_daily_spend_limit", 0.0)
            daily_limit = float(_raw_limit) if isinstance(_raw_limit, (int, float)) else 0.0
            if daily_limit > 0:
                try:
                    daily_spend = await cost.get_daily_spend()
                    if daily_spend >= daily_limit:
                        cost.fire_budget_alert(
                            "LLM Router — Daily Limit Reached",
                            f"Daily spend ${daily_spend:.3f} has crossed the "
                            f"${daily_limit:.2f} limit.",
                        )
                    elif daily_spend >= daily_limit * 0.9:
                        cost.fire_budget_alert(
                            "LLM Router — Daily Spend Warning",
                            f"Daily spend ${daily_spend:.3f} is at "
                            f"{100 * daily_spend / daily_limit:.0f}% of the "
                            f"${daily_limit:.2f} limit.",
                        )
                except Exception as e:
                    log.debug("Daily budget alert check failed: %s", e)

            # Store in semantic cache for future dedup (fire-and-forget)
            if task_type not in MEDIA_TASK_TYPES:
                try:
                    from llm_router import semantic_cache
                    await semantic_cache.store(prompt, task_type, response)
                except Exception as _sc_err:
                    log.debug("Semantic cache store failed (non-fatal): %s", _sc_err)

            async with _budget_lock:
                _pending_spend = max(0.0, _pending_spend - _reservation)
            return response

        except Exception as e:
            is_rate_limit = _is_rate_limit_error(e)
            is_content_filter = not is_rate_limit and _is_content_filter_error(e)
            is_auth = not is_rate_limit and not is_content_filter and _is_auth_error(e)
            if is_rate_limit:
                await _notify(ctx, "warning", f"{model} rate-limited — switching provider...")
                log.warning("Rate limit on %s, switching to next", model)
                route_log.warning(
                    "routing_fallback",
                    correlation_id=correlation_id,
                    model=model,
                    provider=provider,
                    error_type=type(e).__name__,
                    fallback_reason="rate_limit",
                )
                # Extract Retry-After header if available for more accurate cooldown
                retry_after = _extract_retry_after(e)
                tracker.record_rate_limit(provider, cooldown_seconds=retry_after)
            elif is_content_filter:
                log.info("Content filter on %s, trying next model silently", model)
            elif is_auth:
                hint = _auth_error_hint(provider)
                await _notify(ctx, "warning", hint)
                log.warning("Auth error on %s: %s", model, e)
                route_log.warning(
                    "routing_fallback",
                    correlation_id=correlation_id,
                    model=model,
                    provider=provider,
                    error_type=type(e).__name__,
                    fallback_reason="auth_error",
                )
                tracker.record_failure(provider)
            else:
                await _notify(ctx, "warning", f"{model} failed: {e} — trying next...")
                log.warning("Model %s failed: %s", model, e)
                route_log.warning(
                    "routing_fallback",
                    correlation_id=correlation_id,
                    model=model,
                    provider=provider,
                    error_type=type(e).__name__,
                    fallback_reason="provider_error",
                )
                tracker.record_failure(provider)
            last_error = e
            continue

    # ── Emergency fallback: try BUDGET chain when primary chain exhausts ────
    if profile != RoutingProfile.BUDGET and task_type not in MEDIA_TASK_TYPES:
        await _notify(
            ctx, "warning",
            f"⚠️  {profile.value} chain exhausted — trying budget models as fallback"
        )
        log.warning(
            "Primary %s chain exhausted, attempting BUDGET emergency fallback",
            profile.value
        )
        emergency_chain = await _build_and_filter_chain(
            task_type, RoutingProfile.BUDGET, None, complexity_hint, Complexity.SIMPLE, config
        )
        if emergency_chain and emergency_chain != models_to_try:
            for attempt, model in enumerate(emergency_chain, start=len(models_to_try) + 1):
                provider = provider_from_model(model)
                model_name = model.split("/", 1)[1] if "/" in model else model

                if not tracker.is_healthy(provider):
                    log.info("Skipping unhealthy provider in emergency fallback: %s", provider)
                    continue

                try:
                    await _notify(ctx, "info", f"⏳ {model_name} (emergency fallback) working...")

                    if task_type in MEDIA_TASK_TYPES:
                        response = await _call_media(
                            task_type, provider, model_name, prompt,
                            _filter_media_params(task_type, media_params),
                            correlation_id=correlation_id
                        )
                    else:
                        response = await _call_text(
                            model, prompt, system_prompt, temperature, max_tokens, task_type,
                            caller_context=caller_context,
                            use_thinking=use_thinking,
                            correlation_id=correlation_id,
                        )

                    tracker.record_success(provider)
                    await cost.log_usage(response, task_type, RoutingProfile.BUDGET, correlation_id=correlation_id)

                    route_log.info(
                        "emergency_fallback_success",
                        correlation_id=correlation_id,
                        task_type=task_type.value,
                        original_profile=profile.value,
                        fallback_model=response.model,
                        fallback_provider=response.provider,
                        cost_usd=response.cost_usd,
                        latency_ms=response.latency_ms,
                    )

                    await _notify(
                        ctx, "info",
                        f"✅ Emergency fallback {model_name} — {response.latency_ms:.0f}ms · ${response.cost_usd:.6f}"
                    )

                    async with _budget_lock:
                        _pending_spend = max(0.0, _pending_spend - _reservation)
                    return response

                except Exception as e:
                    log.warning(
                        "Emergency fallback model %s failed: %s", model, e
                    )
                    last_error = e
                    continue

    last_is_auth = last_error is not None and _is_auth_error(last_error)
    setup_hint = (
        " Run `llm-router setup` to configure provider API keys, or "
        "`llm-router doctor` to diagnose all issues."
        if last_is_auth else
        " Run `llm_health()` to see circuit breaker status, or "
        "`llm-router doctor` to diagnose all issues."
    )
    set_span_attributes(
        route_span,
        attempts=len(models_to_try),
        last_error_type=type(last_error).__name__ if last_error else None,
    )
    async with _budget_lock:
        _pending_spend = max(0.0, _pending_spend - _reservation)
    raise RuntimeError(
        f"All models failed for {task_type.value}/{profile.value}. "
        f"Last error: {last_error}.{setup_hint}"
    )


async def route_and_call(
    task_type: TaskType,
    prompt: str,
    *,
    profile: RoutingProfile | None = None,
    complexity_hint: Complexity | str | None = None,
    system_prompt: str | None = None,
    model_override: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    media_params: dict | None = None,
    ctx: Any | None = None,
    classification_data: dict | None = None,
    caller_context: str | None = None,
) -> LLMResponse:
    """Route a request to the best available model and return the response.

    Full routing flow:
      1. **Budget check** — if a monthly budget is configured and exceeded,
         raise ``BudgetExceededError`` immediately (fail-fast).
      2. **Model chain** — resolve the ordered list of candidate models from
         the routing profile, or use ``model_override`` if provided.
      3. **Provider filter** — drop any models whose provider has no API key.
      4. **Health check** — skip providers the circuit breaker has marked
         unhealthy (too many recent failures).
      5. **Dispatch** — call the model via ``_call_text`` (LiteLLM) or
         ``_call_media`` (direct API), depending on ``task_type``.
      6. **Fallback** — on failure, record the error in the health tracker
         and try the next model in the chain.
      7. **Cost logging** — on success, log token usage and cost to SQLite.

    Args:
        task_type: What kind of task this is (query, code, image, etc.).
        prompt: The user's prompt text.
        profile: Explicit routing profile override (budget/balanced/premium).
            When omitted, profile is derived from complexity_hint. Prefer
            passing complexity_hint and letting the router pick the profile.
        complexity_hint: Task complexity — "simple", "moderate", or "complex".
            Drives profile selection: simple→BUDGET, moderate→BALANCED,
            complex→PREMIUM. Ignored when profile is explicitly set.
            When both are None, falls back to a fast prompt-length heuristic.
        system_prompt: Optional system prompt prepended to text LLM calls.
        model_override: Force a specific model, bypassing the routing table.
        temperature: Sampling temperature override for text calls.
        max_tokens: Max output tokens override for text calls.
        media_params: Extra keyword arguments forwarded to media generators
            (e.g. image size, video duration).
        ctx: MCP RequestContext for streaming progress notifications.
        classification_data: Optional dict with classification and recommendation
            metadata for quality logging. When provided, a routing decision is
            logged to the ``routing_decisions`` table after a successful call.
        caller_context: Optional context string from the MCP tool caller
            (e.g. recent conversation summary). Injected alongside session
            buffer and persistent history into the LLM messages.

    Returns:
        An ``LLMResponse`` with the model output, cost, and latency.

    Raises:
        BudgetExceededError: Monthly spend has reached the configured limit.
        ValueError: No models available for the given task/profile combo.
        RuntimeError: All candidate models failed (wraps the last error).
    """
    config = get_config()
    correlation_id = uuid4().hex[:8]

    # ── Profile resolution (foundational routing rule) ────────────────────────
    profile, c, use_thinking = _resolve_profile(
        profile, complexity_hint, classification_data, prompt, model_override, config
    )
    effective_complexity = c.value if hasattr(c, "value") else str(complexity_hint or "moderate")
    available = config.available_providers
    with traced_span(
        "route_and_call",
        tracer_name="llm_router.router",
        correlation_id=correlation_id,
        task_type=task_type,
        complexity=effective_complexity,
        profile=profile,
        model_override=model_override,
        prompt_chars=len(prompt),
    ) as route_span:
        route_log = log.bind(
            correlation_id=correlation_id,
            task_type=task_type.value,
            profile=profile.value,
            complexity=effective_complexity,
        )

        # Budget enforcement — block calls if daily or monthly budget is exceeded.
        # _pending_spend tracks in-flight estimated costs so concurrent callers
        # see the full committed + pending total (prevents TOCTOU overrun).
        global _pending_spend
        _reservation: float = 0.0
        async with _budget_lock:
            # Guard: llm_router_daily_spend_limit may be MagicMock in tests
            _raw_daily = getattr(config, "llm_router_daily_spend_limit", 0.0)
            _daily_limit = float(_raw_daily) if isinstance(_raw_daily, (int, float)) else 0.0
            if _daily_limit > 0:
                daily_spend = await cost.get_daily_spend()
                if daily_spend + _pending_spend >= _daily_limit:
                    raise BudgetExceededError(
                        f"Daily spend limit of ${_daily_limit:.2f} exceeded "
                        f"(spent: ${daily_spend:.4f} today UTC). "
                        "Resets at midnight UTC. "
                        "To raise the limit: set LLM_ROUTER_DAILY_SPEND_LIMIT env var."
                    )

            # Per-task daily cap enforcement (from org policy)
            from llm_router.policy import get_task_cap, load_org_policy
            org_policy = load_org_policy()
            task_cap = get_task_cap(task_type.value, org_policy)
            if task_cap and task_cap > 0:
                task_daily_spend = await cost.get_daily_spend_by_task_type(task_type.value)
                if task_daily_spend + _pending_spend >= task_cap:
                    raise BudgetExceededError(
                        f"Task-type daily limit for {task_type.value} (${task_cap:.2f}) exceeded "
                        f"(spent: ${task_daily_spend:.4f} today UTC). "
                        f"Resets at midnight UTC. "
                        f"To raise the limit: update ~/.llm-router/org-policy.yaml task_caps."
                    )

            if config.llm_router_monthly_budget > 0:
                monthly_spend = await cost.get_monthly_spend()
                budget = config.llm_router_monthly_budget
                if monthly_spend + _pending_spend >= budget:
                    raise BudgetExceededError(
                        f"Monthly budget of ${budget:.2f} exceeded "
                        f"(spent: ${monthly_spend:.2f}). "
                        "To continue: run llm_usage() to see the breakdown, or "
                        "llm_set_profile(profile='budget') to switch to cheaper models. "
                        "To raise the limit: set LLM_ROUTER_MONTHLY_BUDGET env var."
                    )
                if (monthly_spend + _pending_spend) >= budget * 0.9:
                    log.warning(
                        "Monthly budget at %.0f%% ($%.2f / $%.2f)",
                        100 * monthly_spend / budget, monthly_spend, budget,
                    )
                    set_span_attributes(route_span, monthly_budget_pressure=monthly_spend / budget)

            # Reserve estimated cost inside the lock so the next concurrent caller
            # includes this call's expected spend in its budget check.
            try:
                from llm_router.session_spend import _estimate_cost as _est_fn
                _reservation = _est_fn("gpt-4o", len(prompt) // 4, 500)
            except Exception:
                _reservation = 0.0
            _pending_spend += _reservation

        # Structural compaction — shrink prompt before sending to external LLMs
        # Guard: compaction_mode/threshold may be MagicMock in test mocks
        compaction_mode = getattr(config, "compaction_mode", "structural")
        compaction_threshold = getattr(config, "compaction_threshold", 4000)
        if (
            isinstance(compaction_mode, str)
            and compaction_mode != "off"
            and isinstance(compaction_threshold, int)
            and task_type not in MEDIA_TASK_TYPES
        ):
            prompt, compaction_result = await compact_structural(
                prompt, compaction_threshold,
            )
            if compaction_result.tokens_saved_estimate > 0:
                log.info(
                    "Compacted prompt: %d→%d chars (~%d tokens saved) [%s]",
                    compaction_result.original_length,
                    compaction_result.compacted_length,
                    compaction_result.tokens_saved_estimate,
                    ", ".join(compaction_result.strategies_applied),
                )
                set_span_attributes(
                    route_span,
                    compacted=True,
                    compacted_chars=compaction_result.compacted_length,
                    tokens_saved_estimate=compaction_result.tokens_saved_estimate,
                )

        models_to_try = await _build_and_filter_chain(
            task_type, profile, model_override, complexity_hint, c, config
        )

        # Quality-based reordering: demote models with low avg judge scores
        if models_to_try and not model_override:  # Only reorder if no manual override
            try:
                from llm_router.judge import reorder_by_quality
                models_to_try = await reorder_by_quality(models_to_try, days=7)
            except Exception as _judge_err:
                log.debug("Quality reordering failed (continuing): %s", _judge_err)

        if not models_to_try:
            set_span_attributes(
                route_span,
                available_providers=sorted(available),
                candidate_count=0,
            )
            raise ValueError(
                f"No available models for {task_type.value}/{profile.value}. "
                f"Configured providers: {available or 'none'}. "
                "Run llm_setup(action='test') to check API keys, or "
                "llm_providers() to see all configured models."
            )

        # Semantic dedup cache — skip the LLM call entirely when an equivalent
        # prompt was answered recently (cosine similarity ≥ 0.95 within 24 hours).
        # Only active when Ollama is configured; silently skipped otherwise.
        if task_type not in MEDIA_TASK_TYPES and not model_override:
            try:
                from llm_router import semantic_cache
                cached = await semantic_cache.check(prompt, task_type)
                if cached is not None:
                    await _notify(ctx, "info", "⚡ Semantic cache hit — skipping LLM call")
                    set_span_attributes(
                        route_span,
                        semantic_cache_hit=True,
                        final_model=cached.model,
                        final_provider=cached.provider,
                        cost_usd=cached.cost_usd,
                        latency_ms=cached.latency_ms,
                    )
                    return cached
            except Exception as _sc_err:
                log.debug("Semantic cache check failed (continuing): %s", _sc_err)

        set_span_attributes(
            route_span,
            semantic_cache_hit=False,
            candidate_count=len(models_to_try),
            top_model=models_to_try[0],
        )

        # Format model chain for visibility: "model1 → model2 → model3" (up to 3 shown)
        chain_display = " → ".join([m.split("/", 1)[1] if "/" in m else m for m in models_to_try[:3]])
        if len(models_to_try) > 3:
            chain_display += f" + {len(models_to_try) - 3} more"

        route_log.info(
            "route_start",
            correlation_id=correlation_id,
            task_type=task_type.value,
            complexity=effective_complexity,
            profile=profile.value,
            top_model=models_to_try[0],
            model_chain=chain_display,
            candidate_count=len(models_to_try),
        )
        await _notify(ctx, "info", f"🤖 Routing: {chain_display} ({task_type.value}/{effective_complexity})")

        # Warn when a RESEARCH task falls back to a non-web-grounded model.
        # Perplexity is the only model in the chain with real-time web access.
        # If it's unavailable (no API key, circuit open), subsequent models will
        # produce plausible but potentially stale answers with no source citations.
        if task_type == TaskType.RESEARCH and "perplexity" not in models_to_try[0]:
            log.warning(
                "RESEARCH task routed to %s — this model has no web access. "
                "Add PERPLEXITY_API_KEY for web-grounded research answers.",
                models_to_try[0],
            )
            await _notify(
                ctx, "warning",
                "⚠️  No web-grounded model available — answer may not reflect current information. "
                "Set PERPLEXITY_API_KEY for real-time web access.",
            )

        # Cost-threshold escalation check (v4.0): block expensive calls before they happen.
        # Uses the same config reference already resolved above (respects mocks in tests).
        config_for_escalation = config
        _escalate_above = getattr(config_for_escalation, "llm_router_escalate_above", 0.0)
        if isinstance(_escalate_above, (int, float)) and _escalate_above > 0 and task_type not in MEDIA_TASK_TYPES:
            try:
                from llm_router.session_spend import get_session_spend, _estimate_cost
                _top_model = models_to_try[0] if models_to_try else ""
                _estimated = _estimate_cost(_top_model, len(prompt) // 4, 500)
                _session_total = get_session_spend().total_usd
                if _estimated > config_for_escalation.llm_router_escalate_above:
                    from llm_router.tools.admin import _set_pending_approval
                    _set_pending_approval({"model": _top_model, "estimated_cost": _estimated})
                    raise BudgetExceededError(
                        f"Call to {_top_model} (estimated ${_estimated:.4f}) exceeds "
                        f"LLM_ROUTER_ESCALATE_ABOVE=${config_for_escalation.llm_router_escalate_above:.2f}. "
                        f"Run llm_approve_route(approve=True) to proceed or "
                        f"llm_approve_route(downgrade_to='gemini/gemini-2.5-flash') to use a cheaper model."
                    )
                if (config_for_escalation.llm_router_hard_stop_above > 0
                        and _session_total >= config_for_escalation.llm_router_hard_stop_above):
                    raise BudgetExceededError(
                        f"Session spend ${_session_total:.4f} has reached the hard stop limit "
                        f"(LLM_ROUTER_HARD_STOP_ABOVE=${config_for_escalation.llm_router_hard_stop_above:.2f}). "
                        f"No further calls will be made this session. "
                        f"Unset LLM_ROUTER_HARD_STOP_ABOVE to continue."
                    )
            except BudgetExceededError:
                async with _budget_lock:
                    _pending_spend = max(0.0, _pending_spend - _reservation)
                raise
            except Exception:
                pass  # Never block routing due to escalation check errors

        # Dispatch through the extracted model loop, which handles both primary
        # and emergency BUDGET fallback chains atomically.
        response = await _dispatch_model_loop(
            models_to_try=models_to_try,
            task_type=task_type,
            profile=profile,
            prompt=prompt,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            media_params=media_params,
            ctx=ctx,
            classification_data=classification_data,
            caller_context=caller_context,
            use_thinking=use_thinking,
            correlation_id=correlation_id,
            complexity_hint=complexity_hint,
            c=c,
            config=config,
            route_span=route_span,
            route_log=route_log,
            _reservation=_reservation,
            effective_complexity=effective_complexity,
        )
        async with _budget_lock:
            _pending_spend = max(0.0, _pending_spend - _reservation)
        return response


async def _call_text(
    model: str,
    prompt: str,
    system_prompt: str | None,
    temperature: float | None,
    max_tokens: int | None,
    task_type: TaskType,
    *,
    caller_context: str | None = None,
    use_thinking: bool = False,
    correlation_id: str | None = None,
) -> LLMResponse:
    """Dispatch a text completion call through LiteLLM's unified API.

    Builds the messages list with optional context injection:
      [system_prompt?] → [context_messages...] → [user_prompt]

    Context messages include previous session summaries and recent
    conversation history, assembled by ``build_context_messages()``.

    Args:
        model: LiteLLM model identifier (e.g. ``"openai/gpt-4o"``).
        prompt: The user's prompt text.
        system_prompt: Optional system message prepended to the conversation.
        temperature: Sampling temperature (None = provider default).
        max_tokens: Maximum output tokens (None = provider default).
        task_type: Used to inject task-specific parameters (e.g. research
            tasks add a recency filter for Perplexity).
        caller_context: Optional caller-supplied context string.

    Returns:
        An ``LLMResponse`` with the generated text, cost, and latency.
    """
    config = get_config()
    messages: list[dict[str, str]] = []

    # Inject system prompt (user-provided or Caveman mode)
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    else:
        # Apply Caveman mode if no user system prompt
        caveman_mode = getattr(config, "caveman_mode", "full")
        if caveman_mode != "off":
            from llm_router.caveman import CavemanIntensity, get_caveman_prompt, should_use_caveman
            if should_use_caveman(model):
                try:
                    caveman_intensity = CavemanIntensity(caveman_mode)
                    messages.append({"role": "system", "content": get_caveman_prompt(caveman_intensity)})
                except ValueError:
                    # Invalid caveman mode — skip
                    pass

    # Inject session + persistent context between system prompt and user prompt
    # Guard: context_enabled may be MagicMock in test mocks
    context_enabled = getattr(config, "context_enabled", True)
    if isinstance(context_enabled, bool) and context_enabled:
        context_msgs = await build_context_messages(
            caller_context=caller_context,
            max_session_messages=getattr(config, "context_max_messages", 5),
            max_previous_sessions=getattr(config, "context_max_previous_sessions", 3),
            max_context_tokens=getattr(config, "context_max_tokens", 1500),
        )
        messages.extend(context_msgs)

    messages.append({"role": "user", "content": prompt})

    extra = {}
    # Perplexity's sonar models support a recency filter that limits
    # search results to the last week, keeping research answers current.
    # Only apply to Perplexity models — other providers reject this field.
    if task_type == TaskType.RESEARCH and "perplexity" in model.lower():
        extra["extra_body"] = {"search_recency_filter": "week"}

    # Extended thinking — enabled for deep_reasoning complexity on Claude models.
    # Anthropic's extended thinking lets the model reason for longer before
    # responding, improving accuracy on proofs, derivations, and complex analysis.
    # Only supported on claude-sonnet-4+ and claude-opus-4+; other providers ignore it.
    if use_thinking and model.startswith("anthropic/"):
        extra["thinking"] = {"type": "enabled", "budget_tokens": 16000}
        # Extended thinking requires temperature=1 (Anthropic API constraint)
        temperature = 1

    if config.prompt_cache_enabled:
        from llm_router.prompt_cache import inject_cache_control
        messages = inject_cache_control(messages, model, min_tokens=config.prompt_cache_min_tokens)

    return await providers.call_llm(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        extra_params=extra or None,
    )


async def _call_media(
    task_type: TaskType,
    provider: str,
    model_name: str,
    prompt: str,
    params: dict | None,
    correlation_id: str | None = None,
) -> LLMResponse:
    """Dispatch a media generation call to the appropriate provider SDK.

    Unlike text calls, media generation bypasses LiteLLM entirely. Each
    provider (fal, OpenAI, Stability, etc.) has its own generator function
    registered in ``media.IMAGE_GENERATORS``, ``media.VIDEO_GENERATORS``,
    or ``media.AUDIO_GENERATORS``. This function looks up the correct
    generator by provider name and forwards the prompt and params.

    Args:
        task_type: The media modality (IMAGE, VIDEO, or AUDIO).
        provider: Provider name extracted from the model string.
        model_name: Model name without the provider prefix.
        prompt: The generation prompt.
        params: Optional provider-specific parameters (size, duration, etc.).

    Returns:
        An ``LLMResponse`` with ``media_url`` set to the generated asset URL.

    Raises:
        ValueError: No generator registered for the provider, or unknown
            media task type.
    """
    params = params or {}

    if task_type == TaskType.IMAGE:
        generators = media.IMAGE_GENERATORS
        if provider not in generators:
            raise ValueError(f"No image generator for provider: {provider}")
        return await generators[provider](prompt, model=model_name, **params)

    elif task_type == TaskType.VIDEO:
        generators = media.VIDEO_GENERATORS
        if provider not in generators:
            raise ValueError(f"No video generator for provider: {provider}")
        return await generators[provider](prompt, model=model_name, **params)

    elif task_type == TaskType.AUDIO:
        generators = media.AUDIO_GENERATORS
        if provider not in generators:
            raise ValueError(f"No audio generator for provider: {provider}")
        return await generators[provider](prompt, model=model_name, **params)

    raise ValueError(f"Unknown media task type: {task_type}")
