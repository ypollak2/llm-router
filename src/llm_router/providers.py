"""LiteLLM wrapper for unified LLM API calls.

Provides two entry points for calling any LLM through LiteLLM's unified API:
- ``call_llm``: Standard request/response (returns full ``LLMResponse``).
- ``call_llm_stream``: Streaming variant that yields content chunks, ending
  with a JSON metadata trailer for the caller to parse.

Both functions handle OpenAI reasoning model quirks (temperature=1 requirement),
apply config defaults, and extract provider-specific features like Perplexity
citations.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator

import litellm

from llm_router.config import get_config
from llm_router.types import LLMResponse

# LiteLLM emits noisy debug output by default (model mappings, retries, etc.)
# that clutters MCP server logs. Suppressing it keeps logs focused on routing.
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

    Flow:
    1. Apply config defaults for temperature and max_tokens if not provided.
    2. Detect OpenAI reasoning models (o1/o3/o4 series) and force temperature=1,
       which is the only value these models accept.
    3. Send the request to LiteLLM's async completion API.
    4. Extract cost via LiteLLM's built-in cost calculator.
    5. Extract Perplexity-specific citations if present.
    6. Return a unified ``LLMResponse`` with all metadata.

    Args:
        model: LiteLLM model string (e.g. ``"gemini/gemini-2.5-flash"``).
            Must include the provider prefix for non-OpenAI models.
        messages: Chat messages in OpenAI format
            (list of ``{"role": "...", "content": "..."}`` dicts).
        temperature: Sampling temperature override. Uses ``config.default_temperature``
            if None. Ignored (forced to 1) for reasoning models.
        max_tokens: Max output tokens override. Uses ``config.default_max_tokens``
            if None or 0.
        extra_params: Provider-specific parameters passed through to LiteLLM
            (e.g. ``{"top_p": 0.9}``). Merged into the call kwargs.

    Returns:
        An ``LLMResponse`` containing the generated content, token counts,
        cost, latency, provider name, and any citations.

    Raises:
        litellm.exceptions.APIError: On provider API errors (4xx/5xx).
        asyncio.TimeoutError: If the call exceeds ``config.request_timeout``.
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

    # LiteLLM provides cost calculation based on its internal pricing tables
    cost = litellm.completion_cost(completion_response=response)

    # Perplexity models return source citations alongside the response
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

    Yields text chunks as they arrive from the provider. After all content
    chunks, yields a final ``\\n[META]{...}`` trailer line containing a JSON
    object with model, provider, token counts, cost, and latency. Callers
    should detect the ``[META]`` prefix to separate content from metadata.

    Unlike ``call_llm``, cost is estimated from token counts rather than
    calculated from the full response object, because LiteLLM's streaming
    API doesn't provide a complete response for its cost calculator. Token
    counts come from the final chunk's usage field (if the provider sends it).

    Args:
        model: LiteLLM model string (e.g. ``"gemini/gemini-2.5-flash"``).
        messages: Chat messages in OpenAI format.
        temperature: Sampling temperature override. Uses config default if None.
        max_tokens: Max output tokens override. Uses config default if None.
        extra_params: Provider-specific parameters passed through to LiteLLM.

    Yields:
        Content text chunks as they arrive, followed by a single
        ``\\n[META]{...}`` JSON metadata line as the final item.
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

        # The final chunk from most providers carries aggregated usage info
        if hasattr(chunk, "usage") and chunk.usage:
            input_tokens = chunk.usage.prompt_tokens or 0
            output_tokens = chunk.usage.completion_tokens or 0

    elapsed_ms = (time.monotonic() - start) * 1000
    full_content = "".join(collected_content)

    # Estimate cost from token counts — litellm.completion_cost needs a full
    # response object which isn't available in streaming mode
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
