"""Core routing logic — selects model and executes with fallback."""

from __future__ import annotations

import logging

from llm_router import cost, providers
from llm_router.config import get_config
from llm_router.health import get_tracker
from llm_router.profiles import get_model_chain, provider_from_model
from llm_router.types import LLMResponse, RoutingProfile, TaskType

log = logging.getLogger("llm_router")


async def route_and_call(
    task_type: TaskType,
    prompt: str,
    *,
    profile: RoutingProfile | None = None,
    system_prompt: str | None = None,
    model_override: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> LLMResponse:
    """Route a request to the best available model and return the response.

    Tries models in the profile's preference order, skipping unhealthy providers.
    Logs usage to the cost tracker after each successful call.
    """
    config = get_config()
    profile = profile or config.llm_router_profile
    tracker = get_tracker()

    messages: list[dict[str, str]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    # If user specified a model, try it directly
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

    # Extra params for Perplexity search
    extra = {}
    if task_type == TaskType.RESEARCH:
        extra["extra_body"] = {"search_recency_filter": "week"}

    last_error: Exception | None = None
    for model in models_to_try:
        provider = provider_from_model(model)

        if not tracker.is_healthy(provider):
            log.info("Skipping unhealthy provider: %s", provider)
            continue

        try:
            response = await providers.call_llm(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                extra_params=extra if extra else None,
            )
            tracker.record_success(provider)
            await cost.log_usage(response, task_type, profile)
            return response

        except Exception as e:
            log.warning("Model %s failed: %s", model, e)
            tracker.record_failure(provider)
            last_error = e
            continue

    raise RuntimeError(
        f"All models failed for {task_type.value}/{profile.value}. "
        f"Last error: {last_error}"
    )
