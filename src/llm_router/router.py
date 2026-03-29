"""Core routing logic — selects model and executes with fallback."""

from __future__ import annotations

import logging
from typing import Any

from llm_router import cost, media, providers
from llm_router.config import get_config
from llm_router.health import get_tracker
from llm_router.profiles import get_model_chain, provider_from_model
from llm_router.types import BudgetExceededError, LLMResponse, RoutingProfile, TaskType

log = logging.getLogger("llm_router")

# Task types that use media generation instead of LiteLLM
MEDIA_TASK_TYPES = {TaskType.IMAGE, TaskType.VIDEO, TaskType.AUDIO}

# Strings that indicate a rate-limit (429) error in various provider SDKs
_RATE_LIMIT_MARKERS = ("rate limit", "rate_limit", "429", "too many requests", "quota exceeded")


def _is_rate_limit_error(exc: Exception) -> bool:
    """Detect if an exception is a rate-limit error from any provider."""
    exc_str = str(exc).lower()
    exc_type = type(exc).__name__.lower()
    return (
        any(m in exc_str for m in _RATE_LIMIT_MARKERS)
        or "ratelimit" in exc_type
    )


async def _notify(ctx: Any | None, level: str, message: str) -> None:
    """Send a log notification via MCP context if available."""
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
) -> LLMResponse:
    """Route a request to the best available model and return the response.

    Handles both text LLMs (via LiteLLM) and media generation (via direct APIs).
    Tries models in the profile's preference order, skipping unhealthy providers.
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

    await _notify(ctx, "info", f"Routing {task_type.value} [{profile.value}] → candidates: {', '.join(models_to_try)}")

    last_error: Exception | None = None
    for model in models_to_try:
        provider = provider_from_model(model)
        model_name = model.split("/", 1)[1] if "/" in model else model

        if not tracker.is_healthy(provider):
            await _notify(ctx, "warning", f"Skipping {provider} (unhealthy)")
            log.info("Skipping unhealthy provider: %s", provider)
            continue

        await _notify(ctx, "info", f"Calling {model}...")

        try:
            if task_type in MEDIA_TASK_TYPES:
                response = await _call_media(task_type, provider, model_name, prompt, media_params)
            else:
                response = await _call_text(
                    model, prompt, system_prompt, temperature, max_tokens, task_type,
                )

            tracker.record_success(provider)
            await cost.log_usage(response, task_type, profile)
            await _notify(
                ctx, "info",
                f"Done → {model} | ${response.cost_usd:.6f} | {response.latency_ms:.0f}ms"
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
) -> LLMResponse:
    """Route a text LLM call through LiteLLM."""
    messages: list[dict[str, str]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    extra = {}
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
    """Route a media generation call to the appropriate provider."""
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
