"""LiteLLM wrapper for unified LLM API calls."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator

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
        model: LiteLLM model string (e.g. "gemini/gemini-2.5-flash").
        messages: Chat messages in OpenAI format.
        temperature: Sampling temperature (uses config default if None).
        max_tokens: Max output tokens (uses config default if None).
        extra_params: Provider-specific params passed to LiteLLM.
    """
    config = get_config()
    temperature = temperature if temperature is not None else config.default_temperature
    max_tokens = max_tokens or config.default_max_tokens

    # O-series reasoning models only accept temperature=1
    model_name = model.split("/", 1)[-1] if "/" in model else model
    is_reasoning = model_name.startswith(("o1", "o3", "o4"))
    if is_reasoning:
        temperature = 1

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


async def call_llm_stream(
    model: str,
    messages: list[dict[str, str]],
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
    extra_params: dict | None = None,
) -> AsyncIterator[str]:
    """Stream an LLM response via LiteLLM, yielding content chunks.

    Yields text chunks as they arrive. The final yielded item is a JSON metadata
    line prefixed with ``\\n[META]`` containing model, tokens, cost, and latency.

    Args:
        model: LiteLLM model string (e.g. "gemini/gemini-2.5-flash").
        messages: Chat messages in OpenAI format.
        temperature: Sampling temperature (uses config default if None).
        max_tokens: Max output tokens (uses config default if None).
        extra_params: Provider-specific params passed to LiteLLM.
    """
    import json

    config = get_config()
    temperature = temperature if temperature is not None else config.default_temperature
    max_tokens = max_tokens or config.default_max_tokens

    model_name = model.split("/", 1)[-1] if "/" in model else model
    is_reasoning = model_name.startswith(("o1", "o3", "o4"))
    if is_reasoning:
        temperature = 1

    start = time.monotonic()

    kwargs: dict = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "timeout": config.request_timeout,
        "stream": True,
    }
    if extra_params:
        kwargs.update(extra_params)

    response = await litellm.acompletion(**kwargs)

    collected_content: list[str] = []
    input_tokens = 0
    output_tokens = 0

    async for chunk in response:
        delta = chunk.choices[0].delta if chunk.choices else None
        if delta and delta.content:
            collected_content.append(delta.content)
            yield delta.content

        # Final chunk often carries usage info
        if hasattr(chunk, "usage") and chunk.usage:
            input_tokens = chunk.usage.prompt_tokens or 0
            output_tokens = chunk.usage.completion_tokens or 0

    elapsed_ms = (time.monotonic() - start) * 1000
    full_content = "".join(collected_content)

    # Estimate cost — litellm.completion_cost needs a full response object,
    # so we estimate from token counts when streaming
    try:
        cost = litellm.completion_cost(
            model=model,
            prompt=str(messages),
            completion=full_content,
        )
    except Exception:
        cost = 0.0

    from llm_router.profiles import provider_from_model

    meta = {
        "model": model,
        "provider": provider_from_model(model),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": round(cost, 8),
        "latency_ms": round(elapsed_ms, 1),
    }
    yield f"\n[META]{json.dumps(meta)}"
