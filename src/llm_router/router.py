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
import logging
from typing import Any

from llm_router import cost, media, providers
from llm_router.codex_agent import CODEX_MODELS, is_codex_available, run_codex
from llm_router.compaction import compact_structural
from llm_router.config import get_config
from llm_router.repo_config import effective_config as get_repo_config
from llm_router.context import build_context_messages, get_session_buffer
from llm_router.health import get_tracker
from llm_router.profiles import get_model_chain, provider_from_model
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

log = logging.getLogger("llm_router")

# Guards the check-then-spend budget sequence so concurrent calls cannot
# both slip through the limit before either has recorded its spend.
_budget_lock = asyncio.Lock()

# Task types routed to provider-specific media APIs instead of LiteLLM.
# LiteLLM only supports text completion; media generation requires direct
# calls to each provider's SDK (DALL-E, Flux, Runway, ElevenLabs, etc.).
MEDIA_TASK_TYPES = {TaskType.IMAGE, TaskType.VIDEO, TaskType.AUDIO}

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
    tracker = get_tracker()

    # ── Profile resolution (foundational routing rule) ────────────────────────
    # Priority: explicit profile > complexity_hint > prompt-length heuristic > config default.
    # Complexity is the correct signal; prompt length is a cheap fallback when
    # the caller doesn't know complexity yet (e.g. media tasks, model_override calls).
    use_thinking = False  # Extended thinking flag — set for deep_reasoning complexity
    if profile is None and not model_override:
        c: Complexity | None = None
        if complexity_hint is not None:
            c = Complexity(complexity_hint) if isinstance(complexity_hint, str) else complexity_hint
        elif classification_data and "complexity" in classification_data:
            try:
                c = Complexity(classification_data["complexity"])
            except ValueError:
                c = Complexity.MODERATE
        else:
            # Fast heuristic — no API call, no latency.
            n = len(prompt)
            c = Complexity.SIMPLE if n < 300 else (Complexity.COMPLEX if n > 3000 else Complexity.MODERATE)
        # deep_reasoning routes to PREMIUM with extended thinking enabled
        if c == Complexity.DEEP_REASONING:
            use_thinking = True
        profile = _COMPLEXITY_TO_PROFILE.get(c, config.llm_router_profile)
    else:
        profile = profile or config.llm_router_profile

    # Budget enforcement — block calls if daily or monthly budget is exceeded.
    # Both checks share one lock to prevent two concurrent callers from both
    # passing the limit check before either has recorded their cost.
    async with _budget_lock:
        # Guard: llm_router_daily_spend_limit may be MagicMock in tests
        _raw_daily = getattr(config, "llm_router_daily_spend_limit", 0.0)
        _daily_limit = float(_raw_daily) if isinstance(_raw_daily, (int, float)) else 0.0
        if _daily_limit > 0:
            daily_spend = await cost.get_daily_spend()
            if daily_spend >= _daily_limit:
                raise BudgetExceededError(
                    f"Daily spend limit of ${_daily_limit:.2f} exceeded "
                    f"(spent: ${daily_spend:.4f} today UTC). "
                    "Resets at midnight UTC. "
                    "To raise the limit: set LLM_ROUTER_DAILY_SPEND_LIMIT env var."
                )

        if config.llm_router_monthly_budget > 0:
            monthly_spend = await cost.get_monthly_spend()
            budget = config.llm_router_monthly_budget
            if monthly_spend >= budget:
                raise BudgetExceededError(
                    f"Monthly budget of ${budget:.2f} exceeded "
                    f"(spent: ${monthly_spend:.2f}). "
                    "To continue: run llm_usage() to see the breakdown, or "
                    "llm_set_profile(profile='budget') to switch to cheaper models. "
                    "To raise the limit: set LLM_ROUTER_MONTHLY_BUDGET env var."
                )
            if monthly_spend >= budget * 0.9:
                log.warning(
                    "Monthly budget at %.0f%% ($%.2f / $%.2f)",
                    100 * monthly_spend / budget, monthly_spend, budget,
                )

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

    if model_override:
        # Validate format early — LiteLLM requires "provider/model" and will
        # produce a cryptic AuthenticationError if the format is wrong.
        _local_prefixes = {"codex", "ollama"}
        if "/" not in model_override and model_override not in _local_prefixes:
            raise ValueError(
                f"Invalid model_override format: {model_override!r}. "
                "Use 'provider/model' format (e.g. 'openai/gpt-4o', "
                "'anthropic/claude-haiku-4-5-20251001', 'gemini/gemini-2.5-flash'). "
                "Run llm_providers() to see all available models."
            )
        # Subscription mode hard block — even explicit overrides must not route
        # to Anthropic when Claude subscription mode is active. The API key is
        # intentionally absent; routing back would fail with auth errors and waste
        # time. Swap to the first non-Anthropic model in the balanced chain instead.
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
            models_to_try = fallback_chain or get_model_chain(RoutingProfile.BALANCED, task_type)
        else:
            models_to_try = [model_override]
    else:
        # Pre-fetch penalty data while we're in an async context, so the
        # benchmark ordering can apply failure-rate and latency penalties
        # without hitting the sync/async deadlock inside penalty functions.
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

        models_to_try = get_model_chain(
            profile, task_type,
            failure_rates=_failure_rates,
            latency_stats=_latency_stats,
            acceptance_scores=_acceptance_scores,
        )
        if task_type not in MEDIA_TASK_TYPES:
            from llm_router.claude_usage import get_claude_pressure
            pressure = get_claude_pressure()

            # ── Provider filter (must run before injection) ──────────────────
            # Filter here so that Codex/Ollama injection decisions (has_claude,
            # has_no_claude) reflect what's actually callable.  In subscription
            # mode, anthropic/* is stripped here; checking has_claude on the
            # unfiltered chain would wrongly place Codex after removed Claude
            # slots, causing it to appear first once those slots are gone.
            # Codex and Ollama use local runtimes — no API key needed.
            available = config.available_providers
            models_to_try = [
                m for m in models_to_try
                if provider_from_model(m) in available
                or provider_from_model(m) in {"codex", "ollama"}
            ]

            # ── Repo config: block_providers + model/provider pin ────────────
            repo_cfg = get_repo_config()
            if repo_cfg.block_providers:
                blocked = set(repo_cfg.block_providers)
                models_to_try = [
                    m for m in models_to_try
                    if provider_from_model(m) not in blocked
                ]
            # Model pin: prepend pinned model so it's tried first
            pinned_model = repo_cfg.model_override(task_type.value)
            pinned_provider = repo_cfg.provider_override(task_type.value)
            if pinned_model and pinned_model not in models_to_try:
                models_to_try = [pinned_model] + models_to_try
            elif pinned_provider and not pinned_model:
                # Provider pin: move models from that provider to the front
                pinned = [m for m in models_to_try if provider_from_model(m) == pinned_provider]
                rest   = [m for m in models_to_try if provider_from_model(m) != pinned_provider]
                models_to_try = pinned + rest

            # ── Ollama injection ─────────────────────────────────────────────
            # Always inject when configured — Ollama is free and local, so there
            # is never a reason to skip it. If the model can't answer (quality
            # mismatch or timeout) it fails fast and the fallback chain continues.
            # This ensures >80% of routable tasks hit a free model first.
            ollama_models = config.all_ollama_models()
            if ollama_models:
                models_to_try = ollama_models + models_to_try

            # ── Codex injection ──────────────────────────────────────────────
            # Codex uses the user's OpenAI subscription (free from Claude quota).
            # Excluded: RESEARCH (no web browsing), BUDGET profile (Codex is balanced-tier).
            #
            # Priority principle: prefer already-paid capacity before paid external APIs.
            # Hierarchy: free-local (Ollama) → free-prepaid (Codex) → paid-per-call.
            # This applies to ALL eligible task types, not just CODE.
            #
            # Injection position:
            #   pressure ≥ 0.95               : Codex at front (before Claude)
            #   Claude in chain, CODE task     : Codex after FIRST Claude
            #                                    (beats paid externals as first fallback)
            #   Claude in chain, other tasks   : Codex after LAST Claude (quality-first)
            #   No Claude (subscription mode)  : Codex after Ollama, before paid externals
            #                                    (already-paid beats paid API for all tasks)
            _codex_eligible_tasks = {TaskType.CODE, TaskType.ANALYZE, TaskType.GENERATE, TaskType.QUERY}
            if (
                profile != RoutingProfile.BUDGET
                and task_type in _codex_eligible_tasks
                and is_codex_available()
            ):
                codex_chain = [f"codex/{m}" for m in CODEX_MODELS[:2]]  # top 2 models
                has_claude = any(m.startswith("anthropic/") for m in models_to_try)
                if pressure >= 0.95:
                    # Near-exhaustion: Codex before everything including remaining Claude
                    log.debug("Codex injected at front (pressure=%.0f%%)", pressure * 100)
                    models_to_try = codex_chain + models_to_try
                elif has_claude and task_type == TaskType.CODE:
                    # CODE task: Codex right after the FIRST Claude model so it beats
                    # paid external APIs (GPT-4o, Gemini Pro) as first non-Claude fallback.
                    first_claude = next(
                        i for i, m in enumerate(models_to_try) if m.startswith("anthropic/")
                    )
                    insert_at = first_claude + 1
                    log.debug("Codex injected after first Claude at index %d (CODE task)", insert_at)
                    models_to_try = models_to_try[:insert_at] + codex_chain + models_to_try[insert_at:]
                elif has_claude:
                    # ANALYZE/GENERATE/QUERY with Claude: quality-first, Codex after last Claude
                    last_claude = max(
                        (i for i, m in enumerate(models_to_try) if m.startswith("anthropic/")),
                        default=-1,
                    )
                    insert_at = last_claude + 1
                    log.debug("Codex injected after last Claude at index %d (%s task)", insert_at, task_type.value)
                    models_to_try = models_to_try[:insert_at] + codex_chain + models_to_try[insert_at:]
                else:
                    # Subscription mode (no Claude in chain): inject Codex after any Ollama
                    # models but before all paid external APIs — free beats paid for every
                    # eligible task type (CODE, ANALYZE, GENERATE, QUERY).
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

    if not models_to_try:
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
                return cached
        except Exception as _sc_err:
            log.debug("Semantic cache check failed (continuing): %s", _sc_err)

    top_model = models_to_try[0].split("/", 1)[1] if "/" in models_to_try[0] else models_to_try[0]
    await _notify(ctx, "info", f"🤖 Routing to {top_model} ({task_type.value}/{profile.value})")

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

    last_error: Exception | None = None
    for model in models_to_try:
        provider = provider_from_model(model)
        model_name = model.split("/", 1)[1] if "/" in model else model

        if not tracker.is_healthy(provider):
            await _notify(ctx, "warning", f"⚠️  {provider} unhealthy — trying next")
            log.info("Skipping unhealthy provider: %s", provider)
            continue

        await _notify(ctx, "info", f"⏳ {model_name} working...")

        try:
            if task_type in MEDIA_TASK_TYPES:
                response = await _call_media(task_type, provider, model_name, prompt, media_params)
            elif provider == "codex":
                import time as _time
                _t0 = _time.monotonic()
                codex_result = await run_codex(prompt, model=model_name)
                if not codex_result.success:
                    raise RuntimeError(f"Codex exited {codex_result.exit_code}: {codex_result.content[:200]}")
                response = LLMResponse(
                    content=codex_result.content,
                    model=f"codex/{model_name}",
                    input_tokens=0,   # CLI doesn't report token counts
                    output_tokens=0,
                    cost_usd=0.0,     # free via OpenAI subscription
                    latency_ms=codex_result.duration_sec * 1000,
                    provider="codex",
                )
            else:
                response = await _call_text(
                    model, prompt, system_prompt, temperature, max_tokens, task_type,
                    caller_context=caller_context,
                    use_thinking=use_thinking,
                )

            tracker.record_success(provider)
            await cost.log_usage(response, task_type, profile)

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
                    )
                except Exception as e:
                    log.warning("Failed to log routing decision: %s", e)

            await _notify(
                ctx, "info",
                f"✅ {model_name} — {response.latency_ms:.0f}ms · ${response.cost_usd:.6f}"
            )

            # Daily spend alert — fire async so it never blocks the response
            # Guard: llm_router_daily_spend_limit may be MagicMock in tests
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
            if task_type not in MEDIA_TASK_TYPES and not model_override:
                try:
                    from llm_router import semantic_cache
                    await semantic_cache.store(prompt, task_type, response)
                except Exception as _sc_err:
                    log.debug("Semantic cache store failed (non-fatal): %s", _sc_err)

            return response

        except Exception as e:
            is_rate_limit = _is_rate_limit_error(e)
            is_content_filter = not is_rate_limit and _is_content_filter_error(e)
            is_auth = not is_rate_limit and not is_content_filter and _is_auth_error(e)
            if is_rate_limit:
                await _notify(ctx, "warning", f"{model} rate-limited — switching provider...")
                log.warning("Rate limit on %s, switching to next", model)
                tracker.record_rate_limit(provider)
            elif is_content_filter:
                # Silent skip — content filter is a provider policy decision, not a
                # reliability failure. Don't show a warning or trip the circuit breaker.
                log.info("Content filter on %s, trying next model silently", model)
            elif is_auth:
                hint = _auth_error_hint(provider)
                await _notify(ctx, "warning", hint)
                log.warning("Auth error on %s: %s", model, e)
                tracker.record_failure(provider)
            else:
                await _notify(ctx, "warning", f"{model} failed: {e} — trying next...")
                log.warning("Model %s failed: %s", model, e)
                tracker.record_failure(provider)
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
    raise RuntimeError(
        f"All models failed for {task_type.value}/{profile.value}. "
        f"Last error: {last_error}.{setup_hint}"
    )


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
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})

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
