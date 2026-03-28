"""LiteLLM wrapper for unified LLM API calls."""

from __future__ import annotations

import time

import litellm

from llm_router.config import get_config
from llm_router.types import LLMResponse

# Suppress LiteLLM's verbose logging
litellm.suppress_debug_info = True


async def call_llm(
    model: str,
    messages: list[dict[str, str]],
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
    extra_params: dict | None = None,
) -> LLMResponse:
    """Call an LLM via LiteLLM and return a standardized response.

    Args:
        model: LiteLLM model string (e.g. "gemini/gemini-2.0-flash").
        messages: Chat messages in OpenAI format.
        temperature: Sampling temperature (uses config default if None).
        max_tokens: Max output tokens (uses config default if None).
        extra_params: Provider-specific params passed to LiteLLM.
    """
    config = get_config()
    temperature = temperature if temperature is not None else config.default_temperature
    max_tokens = max_tokens or config.default_max_tokens

    start = time.monotonic()

    kwargs: dict = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "timeout": config.request_timeout,
    }
    if extra_params:
        kwargs.update(extra_params)

    response = await litellm.acompletion(**kwargs)
    elapsed_ms = (time.monotonic() - start) * 1000

    content = response.choices[0].message.content or ""
    usage = response.usage

    # LiteLLM provides cost calculation
    cost = litellm.completion_cost(completion_response=response)

    # Extract citations from Perplexity responses
    citations: list[str] = []
    if hasattr(response, "citations"):
        citations = response.citations or []

    from llm_router.profiles import provider_from_model

    return LLMResponse(
        content=content,
        model=model,
        input_tokens=usage.prompt_tokens or 0,
        output_tokens=usage.completion_tokens or 0,
        cost_usd=cost,
        latency_ms=elapsed_ms,
        provider=provider_from_model(model),
        citations=citations,
    )
