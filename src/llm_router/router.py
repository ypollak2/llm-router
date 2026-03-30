"""Core routing logic — selects model and executes with fallback.

This module is the central dispatch layer of llm-router. It receives a
(task_type, prompt) pair, resolves the best model chain from profiles,
enforces budget limits, and walks the chain until one model succeeds.

Text tasks are dispatched through LiteLLM (unified OpenAI-compatible SDK).
Media tasks (image/video/audio) bypass LiteLLM and call provider-specific
generation APIs directly, because LiteLLM has no media generation support.
"""

from __future__ import annotations

import logging
from typing import Any

from llm_router import cost, media, providers
from llm_router.compaction import compact_structural
from llm_router.config import get_config
from llm_router.context import build_context_messages, get_session_buffer
from llm_router.health import get_tracker
from llm_router.profiles import get_model_chain, provider_from_model
from llm_router.types import BudgetExceededError, LLMResponse, RoutingProfile, TaskType

log = logging.getLogger("llm_router")

# Task types routed to provider-specific media APIs instead of LiteLLM.
# LiteLLM only supports text completion; media generation requires direct
# calls to each provider's SDK (DALL-E, Flux, Runway, ElevenLabs, etc.).
MEDIA_TASK_TYPES = {TaskType.IMAGE, TaskType.VIDEO, TaskType.AUDIO}

# Substrings checked against exception messages and type names to detect
# rate-limit (HTTP 429) errors. Each provider SDK formats these differently
# (OpenAI says "Rate limit", Anthropic uses "rate_limit", etc.), so we
# check multiple markers to catch them all reliably.
_RATE_LIMIT_MARKERS = ("rate limit", "rate_limit", "429", "too many requests", "quota exceeded")


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
        profile: Routing profile override (budget/balanced/premium).
            Defaults to the profile set in config.
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
    profile = profile or config.llm_router_profile
    tracker = get_tracker()

    # Budget enforcement — block calls if monthly budget is exceeded
    if config.llm_router_monthly_budget > 0:
        monthly_spend = await cost.get_monthly_spend()
        if monthly_spend >= config.llm_router_monthly_budget:
            raise BudgetExceededError(
                f"Monthly budget of ${config.llm_router_monthly_budget:.2f} exceeded "
                f"(spent: ${monthly_spend:.2f}). "
                "Increase budget via LLM_ROUTER_MONTHLY_BUDGET or wait until next month."
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
        models_to_try = [model_override]
    else:
        models_to_try = get_model_chain(profile, task_type)

    # Filter to providers we have keys for
    available = config.available_providers
    models_to_try = [
        m for m in models_to_try if provider_from_model(m) in available
    ]

    if not models_to_try:
        raise ValueError(
            f"No available models for {task_type.value}/{profile.value}. "
            f"Configured providers: {available or 'none'}. "
            "Run `llm-router-onboard` to configure API keys."
        )

    top_model = models_to_try[0].split("/", 1)[1] if "/" in models_to_try[0] else models_to_try[0]
    await _notify(ctx, "info", f"🤖 Routing to {top_model} ({task_type.value}/{profile.value})")

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
            else:
                response = await _call_text(
                    model, prompt, system_prompt, temperature, max_tokens, task_type,
                    caller_context=caller_context,
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
                    )
                except Exception as e:
                    log.warning("Failed to log routing decision: %s", e)

            await _notify(
                ctx, "info",
                f"✅ {model_name} — {response.latency_ms:.0f}ms · ${response.cost_usd:.6f}"
            )
            return response

        except Exception as e:
            is_rate_limit = _is_rate_limit_error(e)
            if is_rate_limit:
                await _notify(ctx, "warning", f"{model} rate-limited — switching provider...")
                log.warning("Rate limit on %s, switching to next", model)
                tracker.record_rate_limit(provider)
            else:
                await _notify(ctx, "warning", f"{model} failed: {e} — trying next...")
                log.warning("Model %s failed: %s", model, e)
                tracker.record_failure(provider)
            last_error = e
            continue

    raise RuntimeError(
        f"All models failed for {task_type.value}/{profile.value}. "
        f"Last error: {last_error}"
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
    if task_type == TaskType.RESEARCH:
        extra["extra_body"] = {"search_recency_filter": "week"}

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
