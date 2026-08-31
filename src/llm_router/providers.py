"""LiteLLM wrapper for unified LLM API calls.

Provides two entry points for calling any LLM through LiteLLM's unified API:
- ``call_llm``: Standard request/response (returns full ``LLMResponse``).
- ``call_llm_stream``: Streaming variant that yields content chunks, ending
  with a JSON metadata trailer for the caller to parse.
- ``call_llm_stream_events``: Structured event streaming (Phase B v0.3.2).

Both functions handle OpenAI reasoning model quirks (temperature=1 requirement),
apply config defaults, and extract provider-specific features like Perplexity
citations.

Safety invariants (Phase B):
  - Structured events provide unambiguous state transitions
  - output.delta preserves message order (never buffered or throttled)
  - usage.final delivered exactly once per stream
  - Backward compatibility maintained: call_llm_stream() wraps call_llm_stream_events()
"""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator
from typing import TypedDict

# GH-74: litellm's own __init__.py unconditionally calls ``dotenv.load_dotenv()``
# (no path) the first time it is imported, unless ``LITELLM_MODE`` is already set
# to something other than "DEV" (its default). ``load_dotenv()`` with no
# arguments does an *upward filesystem search* from litellm's own install
# location (deep inside site-packages), not from this repo or from ``$HOME`` —
# so on a machine where any ancestor directory of the venv happens to contain a
# ``.env`` (e.g. a developer's personal ``~/.env`` with real provider keys for
# unrelated tools), importing litellm silently merges those keys into the real
# process ``os.environ`` for the lifetime of the interpreter. llm_router already
# owns its own, explicit config loading via ``RouterConfig`` (which reads
# ``~/.llm-router/.env``); it neither needs nor wants a second, uncontrolled,
# ancestor-directory dotenv load happening as a side effect of a third-party
# import. Setting ``LITELLM_MODE`` before the import (without clobbering an
# operator's explicit choice) opts out of that behaviour for good, in both
# production and tests — see tests/test_gh74_env_hermeticity.py.
os.environ.setdefault("LITELLM_MODE", "PROD")

import litellm

from llm_router.config import get_config
from llm_router.inference_robustness import (
    ensure_non_empty_content,
    extract_content,
    safe_max_tokens,
)
from llm_router.prompt_cache import inject_cache_control
from llm_router.types import LLMResponse

# LiteLLM emits noisy debug output by default (model mappings, retries, etc.)
# that clutters MCP server logs. Suppressing it keeps logs focused on routing.
litellm.suppress_debug_info = True

# Keys allowed in extra_params passed to LiteLLM.  Any key outside this set
# could redirect the call (api_key, base_url, api_base), leak credentials, or
# override provider selection.  New legitimate keys should be added here.
_ALLOWED_EXTRA_PARAMS: frozenset[str] = frozenset({
    "temperature",
    "max_tokens",
    "top_p",
    "top_k",
    "stop",
    "seed",
    "presence_penalty",
    "frequency_penalty",
    "logit_bias",
    "n",
    "stream",
    "logprobs",
    "top_logprobs",
    # Provider-specific safe pass-through keys
    "extra_body",   # used for Perplexity search_recency_filter
    "thinking",     # used for Anthropic extended thinking
})


# ━━━ Phase B v0.3.2: Provider-Level Structured Event Streaming ━━━
# These TypedDicts define the structured events yielded by call_llm_stream_events().
# They model the low-level provider streaming contract (before router-level mapping).


class ProviderStreamDelta(TypedDict):
    """Chunk of response content from provider stream."""
    text: str
    chars: int
    approx_tokens: int


class ProviderUsageInfo(TypedDict):
    """Final token usage and cost from provider."""
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: float


class ProviderStreamEvent(TypedDict, total=False):
    """Provider-level streaming event (base + optional payloads).

    Fields:
      - type: "delta" (content chunk) or "usage" (final metadata)
      - delta: ProviderStreamDelta (only when type=="delta")
      - usage: ProviderUsageInfo (only when type=="usage")

    Safety: output.delta is never buffered or throttled. usage.final delivered once.
    """
    type: str  # "delta" or "usage"
    delta: ProviderStreamDelta
    usage: ProviderUsageInfo


def _ollama_num_ctx() -> int | None:
    """Configured Ollama context window (LLM_ROUTER_OLLAMA_NUM_CTX), or None.

    Returns None when unset or invalid so callers keep Ollama's own default —
    passing num_ctx is opt-in. A positive integer raises the context window
    for page-sized prompts/outputs that otherwise overflow the 4096 default
    and return empty content.
    """
    import os

    raw = os.environ.get("LLM_ROUTER_OLLAMA_NUM_CTX", "").strip()
    if not raw:
        return None
    try:
        val = int(raw)
    except ValueError:
        return None
    return val if val > 0 else None


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
    # Cap max_tokens at the model's known output limit (Plan 07 D.2) —
    # prevents OpenAI silent truncation and Anthropic 400-errors when
    # callers pass oversized values. Unknown models bypass the cap.
    max_tokens = safe_max_tokens(max_tokens, model)

    # O-series reasoning models only accept temperature=1
    model_name = model.split("/", 1)[-1] if "/" in model else model
    is_reasoning = model_name.startswith(("o1", "o3", "o4"))
    if is_reasoning:
        temperature = 1

    start = time.monotonic()

    # Inject provider-agnostic cache control hints (Anthropic: system message caching)
    cached_messages = inject_cache_control(messages, model)

    kwargs: dict = {
        "model": model,
        "messages": cached_messages,
        "temperature": temperature,
        "timeout": config.request_timeout,
    }

    # 🥷 Backslash-Security: Avoid shell by using safe APIs for LLM calls.
    # Ollama integration in LiteLLM has a bug where max_tokens causes empty responses.
    # Workaround: don't pass max_tokens to Ollama models (let them respond naturally).
    # This is safe since Ollama typically returns reasonable-length responses.
    if not model.startswith("ollama/"):
        kwargs["max_tokens"] = max_tokens
    else:
        # Ollama defaults to a 4096-token context window, which silently
        # overflows on page-sized prompts/outputs and returns EMPTY content
        # (surfaced downstream as EmptyResponseError → chain failover). Let
        # operators raise it via LLM_ROUTER_OLLAMA_NUM_CTX so large generations
        # don't empty out. Unset = keep Ollama's own default (no change).
        _num_ctx = _ollama_num_ctx()
        if _num_ctx:
            kwargs["num_ctx"] = _num_ctx

    if extra_params:
        safe = {k: v for k, v in extra_params.items() if k in _ALLOWED_EXTRA_PARAMS}
        kwargs.update(safe)

    # Plan 07 Cat D.4 — apply registered per-provider quirks so future
    # provider-specific behaviour (OpenRouter prefix re-prepending, new
    # reasoning-model temperature constraints, etc.) lands as a registry
    # entry rather than another inline branch here.
    from llm_router.profiles import provider_from_model
    from llm_router.provider_quirks import get_quirk
    _quirk = get_quirk(provider_from_model(model))
    kwargs["model"] = _quirk.transform_model_name(kwargs["model"])
    kwargs = _quirk.transform_request(kwargs)

    response = await litellm.acompletion(**kwargs)  # llm_router: direct-ok (router provider layer)
    elapsed_ms = (time.monotonic() - start) * 1000

    content = extract_content(response.choices[0].message)
    # Plan 07 D.3: surface empty-content responses as a routing failure
    # so the router falls through to the next model in the chain instead
    # of silently returning an empty LLMResponse.
    content = ensure_non_empty_content(content, model)
    usage = response.usage

    # LiteLLM provides cost calculation based on its internal pricing tables;
    # falls back to calibration.cost_for_tokens for models LiteLLM hasn't
    # catalogued (notably the OpenRouter open-weight pool, which lives in our
    # _PRICING_PER_M dict via Plan 06 Step 2 but not in LiteLLM's snapshot).
    #
    # v10.1 — Unknown-paid-model fallback. If both LiteLLM AND our pricing dict
    # come up empty for a paid model (e.g. a recently-added OpenRouter model
    # we haven't priced yet), fall back to a conservative rate so the dashboard
    # doesn't report $0 for a call that genuinely cost money. Mirrors the
    # logic in session_spend._estimate_cost so both surfaces agree.
    _prompt_tokens = int(getattr(response.usage, "prompt_tokens", 0) or 0)
    _completion_tokens = int(getattr(response.usage, "completion_tokens", 0) or 0)
    try:
        cost = litellm.completion_cost(completion_response=response)
    except Exception:
        from llm_router.calibration import cost_for_tokens
        cost = cost_for_tokens(model, _prompt_tokens, _completion_tokens)
    if cost == 0 and not any(
        model.startswith(p) for p in ("ollama", "codex", "gemini_cli", "openai_compat")
    ):
        # Unknown paid model. Bias high so anomaly detection has a signal.
        # 0.01 USD per 1K output tokens matches session_spend's legacy rate.
        cost = _completion_tokens * 0.01 / 1000

    # Perplexity models return source citations alongside the response
    citations: list[str] = []
    if hasattr(response, "citations"):
        citations = response.citations or []

    from llm_router.profiles import provider_from_model

    # Anthropic prompt-caching tokens — LiteLLM exposes these on the usage
    # block when the provider response includes them. Safe defaults for
    # non-Anthropic providers (which return 0). v9.2.2.
    cache_creation = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
    cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)

    return LLMResponse(
        content=content,
        model=model,
        input_tokens=usage.prompt_tokens or 0,
        output_tokens=usage.completion_tokens or 0,
        cost_usd=cost,
        latency_ms=elapsed_ms,
        provider=provider_from_model(model),
        citations=citations,
        cache_creation_input_tokens=cache_creation,
        cache_read_input_tokens=cache_read,
    )


async def call_llm_stream_events(
    model: str,
    messages: list[dict[str, str]],
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
    extra_params: dict | None = None,
) -> AsyncIterator[ProviderStreamEvent]:
    """Stream an LLM response via LiteLLM, yielding structured provider events.

    Yields a sequence of ProviderStreamEvent objects representing the stream:
    - Multiple delta events with content chunks as they arrive
    - One final usage event with aggregated token counts and cost

    This is the provider-level streaming API (Phase B v0.3.2). The router layer
    (Phase C) maps these provider events to router-level RouterStreamEvent objects
    that include routing metadata and state tracking.

    Args:
        model: LiteLLM model string (e.g. ``"gemini/gemini-2.5-flash"``).
        messages: Chat messages in OpenAI format.
        temperature: Sampling temperature override. Uses config default if None.
        max_tokens: Max output tokens override. Uses config default if None.
        extra_params: Provider-specific parameters passed through to LiteLLM.

    Yields:
        ProviderStreamEvent dicts with:
        - "delta" events: {type: "delta", delta: {text, chars, approx_tokens}}
        - "usage" event: {type: "usage", usage: {input_tokens, output_tokens, cost_usd, latency_ms}}

    Safety:
      - output deltas preserve message order (never buffered)
      - usage is delivered exactly once as final event
      - no recursion / re-entrancy risk from provider layer
    """
    config = get_config()
    temperature = temperature if temperature is not None else config.default_temperature
    max_tokens = max_tokens or config.default_max_tokens
    max_tokens = safe_max_tokens(max_tokens, model)

    model_name = model.split("/", 1)[-1] if "/" in model else model
    is_reasoning = model_name.startswith(("o1", "o3", "o4"))
    if is_reasoning:
        temperature = 1

    start = time.monotonic()

    # Inject provider-agnostic cache control hints
    cached_messages = inject_cache_control(messages, model)

    kwargs: dict = {
        "model": model,
        "messages": cached_messages,
        "temperature": temperature,
        "timeout": config.request_timeout,
        "stream": True,
    }

    # 🥷 Backslash-Security: Same Ollama workaround as in call_llm() above.
    if not model.startswith("ollama/"):
        kwargs["max_tokens"] = max_tokens

    if extra_params:
        safe = {k: v for k, v in extra_params.items() if k in _ALLOWED_EXTRA_PARAMS}
        kwargs.update(safe)

    # Plan 07 Cat D.4 — apply registered per-provider quirks
    from llm_router.profiles import provider_from_model
    from llm_router.provider_quirks import get_quirk
    _quirk = get_quirk(provider_from_model(model))
    kwargs["model"] = _quirk.transform_model_name(kwargs["model"])
    kwargs = _quirk.transform_request(kwargs)

    response = await litellm.acompletion(**kwargs)  # llm_router: direct-ok (router provider layer)

    collected_content: list[str] = []
    input_tokens = 0
    output_tokens = 0

    # Estimate token counts for deltas (before usage arrives)
    async for chunk in response:
        delta = chunk.choices[0].delta if chunk.choices else None
        if delta and delta.content:
            text = delta.content
            collected_content.append(text)
            # Rough estimate: 1 token ≈ 4 chars (OpenAI standard)
            chars = len(text)
            approx_tokens = max(1, len(text) // 4)
            yield {
                "type": "delta",
                "delta": {
                    "text": text,
                    "chars": chars,
                    "approx_tokens": approx_tokens,
                },
            }

        # The final chunk from most providers carries aggregated usage info
        if hasattr(chunk, "usage") and chunk.usage:
            input_tokens = chunk.usage.prompt_tokens or 0
            output_tokens = chunk.usage.completion_tokens or 0

    elapsed_ms = (time.monotonic() - start) * 1000
    full_content = "".join(collected_content)

    # Estimate cost from token counts
    try:
        cost = litellm.completion_cost(
            model=model,
            prompt=str(messages),
            completion=full_content,
        )
    except Exception:
        cost = 0.0

    # Yield final usage event (delivered exactly once)
    yield {
        "type": "usage",
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": round(cost, 8),
            "latency_ms": round(elapsed_ms, 1),
        },
    }


async def call_llm_stream(
    model: str,
    messages: list[dict[str, str]],
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
    extra_params: dict | None = None,
) -> AsyncIterator[str]:
    """Stream an LLM response via LiteLLM, yielding content chunks (compatibility wrapper).

    This is a backward-compatibility wrapper around call_llm_stream_events().
    It translates structured provider events back to the legacy text-based format.

    Yields text chunks as they arrive from the provider. After all content
    chunks, yields a final ``\\n[META]{...}`` trailer line containing a JSON
    object with model, provider, token counts, cost, and latency. Callers
    should detect the ``[META]`` prefix to separate content from metadata.

    Args:
        model: LiteLLM model string (e.g. ``"gemini/gemini-2.5-flash"``).
        messages: Chat messages in OpenAI format.
        temperature: Sampling temperature override. Uses config default if None.
        max_tokens: Max output tokens override. Uses config default if None.
        extra_params: Provider-specific parameters passed through to LiteLLM.

    Yields:
        Content text chunks as they arrive, followed by a single
        ``\\n[META]{...}`` JSON metadata line as the final item.

    Safety: Backward compatible with pre-v0.3.2 callers. All existing behavior preserved.
    """
    import json

    from llm_router.profiles import provider_from_model

    # Stream from provider layer and translate to legacy format
    async for event in call_llm_stream_events(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        extra_params=extra_params,
    ):
        if event["type"] == "delta":
            # Yield content chunks as-is
            yield event["delta"]["text"]
        elif event["type"] == "usage":
            # Yield final metadata in legacy [META] format
            usage = event["usage"]
            meta = {
                "model": model,
                "provider": provider_from_model(model),
                "input_tokens": usage["input_tokens"],
                "output_tokens": usage["output_tokens"],
                "cost_usd": usage["cost_usd"],
                "latency_ms": usage["latency_ms"],
            }
            yield f"\n[META]{json.dumps(meta)}"
