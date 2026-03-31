"""Routing profiles — maps (profile, task_type) to ordered model preferences.

This module defines the static routing tables that power the multi-provider
fallback chain. For every (RoutingProfile, TaskType) pair, there is an ordered
list of models to try. The router walks this list top-to-bottom, skipping
unhealthy providers, until one succeeds.

Three profile tiers exist:
  - **BUDGET**: cheapest models that still produce usable results. Prioritizes
    free/low-cost providers (Gemini Flash, Groq, DeepSeek).
  - **BALANCED**: quality/cost sweet spot. Uses mid-tier models from major
    providers (GPT-4o, Claude Sonnet, Gemini Pro).
  - **PREMIUM**: best available quality, cost secondary. Uses frontier models
    (o3, Claude Opus, Gemini Pro).

Model IDs use LiteLLM's ``provider/model`` format for text models and the
same convention for media models (though media bypasses LiteLLM).
"""

from __future__ import annotations

import logging

from llm_router.types import Complexity, RoutingProfile, TaskType

log = logging.getLogger("llm_router")

# Models treated as "free" under a Claude Pro subscription — tried first.
_CLAUDE_MODELS: frozenset[str] = frozenset({
    "anthropic/claude-opus-4-6",
    "anthropic/claude-sonnet-4-6",
    "anthropic/claude-haiku-4-5-20251001",
})

# Free external models (Codex uses OpenAI subscription, no API spend).
_FREE_EXTERNAL_MODELS: frozenset[str] = frozenset({
    "codex/gpt-5.4",
    "codex/o3",
    "codex/gpt-4o",
})

# Cheap-but-not-free models (< $0.002/1K tokens blended).
# deepseek-reasoner is $0.0014 — cheaper than Gemini Pro ($0.003) and outperforms
# it on every benchmark, so it belongs in the cheap tier for pressure reordering.
_CHEAP_MODELS: frozenset[str] = frozenset({
    "gemini/gemini-2.5-flash",
    "gemini/gemini-2.5-flash-lite",
    "groq/llama-3.3-70b-versatile",
    "deepseek/deepseek-chat",
    "deepseek/deepseek-reasoner",
    "openai/gpt-4o-mini",
})

# Master routing table: maps (profile, task_type) -> ordered model chain.
# Each entry is a list of model IDs in LiteLLM's "provider/model" format.
# The router tries models in order, falling back to the next on failure or
# rate-limiting. Models are ordered by preference within each tier (best
# fit first, broadest fallback last).

ROUTING_TABLE: dict[tuple[RoutingProfile, TaskType], list[str]] = {
    # ═══════════════════════════════════════════════════════════════════
    # BUDGET — cheapest models, good enough for most tasks
    # ═══════════════════════════════════════════════════════════════════
    # BUDGET chains: Haiku leads so the pattern is always
    #   (with Ollama)    Ollama → Haiku → cheap externals
    #   (without Ollama) Haiku  → cheap externals
    (RoutingProfile.BUDGET, TaskType.QUERY): [
        "anthropic/claude-haiku-4-5-20251001",
        "gemini/gemini-2.5-flash",
        "groq/llama-3.3-70b-versatile",
        "deepseek/deepseek-chat",
        "openai/gpt-4o-mini",
    ],
    (RoutingProfile.BUDGET, TaskType.RESEARCH): [
        "perplexity/sonar",               # web-grounded first
        "anthropic/claude-haiku-4-5-20251001",
        "gemini/gemini-2.5-flash",
        "openai/gpt-4o-mini",
    ],
    (RoutingProfile.BUDGET, TaskType.GENERATE): [
        "anthropic/claude-haiku-4-5-20251001",
        "gemini/gemini-2.5-flash",
        "deepseek/deepseek-chat",
        "mistral/mistral-small-latest",
        "openai/gpt-4o-mini",
    ],
    (RoutingProfile.BUDGET, TaskType.ANALYZE): [
        "anthropic/claude-haiku-4-5-20251001",
        "gemini/gemini-2.5-flash",
        "deepseek/deepseek-reasoner",
        "groq/llama-3.3-70b-versatile",
        "openai/gpt-4o-mini",
    ],
    (RoutingProfile.BUDGET, TaskType.CODE): [
        "anthropic/claude-haiku-4-5-20251001",
        "deepseek/deepseek-chat",
        "gemini/gemini-2.5-flash",
        "groq/llama-3.3-70b-versatile",
        "openai/gpt-4o-mini",
    ],
    (RoutingProfile.BUDGET, TaskType.IMAGE): [
        "fal/flux-dev",
        "gemini/imagen-3-fast",
        "stability/stable-diffusion-3",
        "openai/dall-e-2",
    ],
    (RoutingProfile.BUDGET, TaskType.VIDEO): [
        "fal/minimax-video",
        "gemini/veo-2",
        "replicate/minimax-video",
    ],
    (RoutingProfile.BUDGET, TaskType.AUDIO): [
        "openai/tts-1",
        "elevenlabs/eleven_multilingual_v2",
    ],

    # ═══════════════════════════════════════════════════════════════════
    # BALANCED — quality/cost sweet spot
    # Claude models lead: they're "free" under a Pro subscription.
    # External models are fallbacks or activated under quota pressure.
    # RESEARCH is the exception: Claude can't browse the web, so
    # Perplexity (web-grounded) stays first regardless of pressure.
    # ═══════════════════════════════════════════════════════════════════
    # BALANCED chains: Sonnet → paid externals → Haiku (last resort cheap Claude)
    # Codex (free via OpenAI sub) is injected dynamically by router.py when
    # is_codex_available() — it cannot be in the static table since it routes
    # through codex_agent.run_codex(), not LiteLLM.
    (RoutingProfile.BALANCED, TaskType.QUERY): [
        "anthropic/claude-sonnet-4-6",
        "deepseek/deepseek-chat",       # quality 1.0, $0.0007 — leads when Claude is out
        "openai/gpt-4o",
        "gemini/gemini-2.5-pro",
        "mistral/mistral-large-latest",
        "anthropic/claude-haiku-4-5-20251001",
    ],
    (RoutingProfile.BALANCED, TaskType.RESEARCH): [
        "perplexity/sonar-pro",       # web-grounded, Claude can't search
        "perplexity/sonar",
        "anthropic/claude-sonnet-4-6",
        "gemini/gemini-2.5-pro",
        "openai/gpt-4o",
    ],
    (RoutingProfile.BALANCED, TaskType.GENERATE): [
        "anthropic/claude-sonnet-4-6",
        "deepseek/deepseek-chat",       # quality 1.0 for generation, $0.0007
        "gemini/gemini-2.5-pro",
        "openai/gpt-4o",
        "cohere/command-r-plus",
        "anthropic/claude-haiku-4-5-20251001",
    ],
    (RoutingProfile.BALANCED, TaskType.ANALYZE): [
        "anthropic/claude-sonnet-4-6",
        "openai/gpt-4o",
        "gemini/gemini-2.5-pro",
        "deepseek/deepseek-reasoner",
        "anthropic/claude-haiku-4-5-20251001",
    ],
    (RoutingProfile.BALANCED, TaskType.CODE): [
        "anthropic/claude-sonnet-4-6",
        "openai/gpt-4o",
        "gemini/gemini-2.5-pro",
        "deepseek/deepseek-chat",
        "anthropic/claude-haiku-4-5-20251001",
    ],
    (RoutingProfile.BALANCED, TaskType.IMAGE): [
        "fal/flux-pro",
        "gemini/imagen-3",
        "openai/dall-e-3",
        "stability/stable-diffusion-3",
    ],
    (RoutingProfile.BALANCED, TaskType.VIDEO): [
        "fal/kling-video",
        "gemini/veo-2",
        "runway/gen3a_turbo",
        "replicate/minimax-video",
    ],
    (RoutingProfile.BALANCED, TaskType.AUDIO): [
        "elevenlabs/eleven_multilingual_v2",
        "openai/tts-1-hd",
    ],

    # ═══════════════════════════════════════════════════════════════════
    # PREMIUM — best available per task, cost secondary
    # Claude Opus leads (strongest model, free under subscription).
    # ═══════════════════════════════════════════════════════════════════
    (RoutingProfile.PREMIUM, TaskType.QUERY): [
        "anthropic/claude-opus-4-6",
        "anthropic/claude-sonnet-4-6",
        "deepseek/deepseek-chat",       # quality 1.0, $0.0007 — leads at >85% pressure
        "openai/gpt-4o",
        "gemini/gemini-2.5-pro",
        "openai/o3",                    # expensive last resort ($0.025)
        "xai/grok-3",
    ],
    (RoutingProfile.PREMIUM, TaskType.RESEARCH): [
        "perplexity/sonar-pro",       # web-grounded, stays first always
        "perplexity/sonar",
        "anthropic/claude-opus-4-6",
        "openai/o3",
        "gemini/gemini-2.5-pro",
    ],
    (RoutingProfile.PREMIUM, TaskType.GENERATE): [
        "anthropic/claude-opus-4-6",
        "anthropic/claude-sonnet-4-6",
        "deepseek/deepseek-chat",       # quality 1.0 for generation, cheap fallback
        "gemini/gemini-2.5-pro",
        "openai/gpt-4o",
        "openai/o3",
    ],
    (RoutingProfile.PREMIUM, TaskType.ANALYZE): [
        "anthropic/claude-opus-4-6",
        "anthropic/claude-sonnet-4-6",
        "openai/o3",
        "deepseek/deepseek-reasoner",
        "gemini/gemini-2.5-pro",
    ],
    (RoutingProfile.PREMIUM, TaskType.CODE): [
        "anthropic/claude-opus-4-6",
        "anthropic/claude-sonnet-4-6",
        "deepseek/deepseek-reasoner",   # quality 1.0, $0.0014 — leads at >85% pressure
        "openai/gpt-4o",
        "openai/o3",
    ],
    (RoutingProfile.PREMIUM, TaskType.IMAGE): [
        "gemini/imagen-3",
        "openai/dall-e-3",
        "fal/flux-pro",
        "stability/stable-diffusion-3-ultra",
    ],
    (RoutingProfile.PREMIUM, TaskType.VIDEO): [
        "gemini/veo-2",
        "runway/gen3a",
        "fal/kling-video",
    ],
    (RoutingProfile.PREMIUM, TaskType.AUDIO): [
        "elevenlabs/eleven_multilingual_v2",
        "openai/tts-1-hd",
    ],
}


# ── Classifier model preferences (cheapest/fastest first) ────────────────────
# These models are used exclusively by the complexity classifier, NOT for
# user-facing responses. They are ordered cheapest-first because classification
# is a low-stakes, structured-output task that doesn't need frontier quality.
#
# IMPORTANT: Non-thinking models are strongly preferred here. Thinking models
# (e.g. gemini-2.5-flash, deepseek-reasoner) spend most of their output budget
# on internal chain-of-thought reasoning, which often causes the actual JSON
# response to be truncated — triggering the _parse_truncated_json fallback.
CLASSIFIER_MODELS: list[str] = [
    # Haiku is the preferred classifier — fast, cheap, accurate structured output.
    # Skipped automatically when no ANTHROPIC_API_KEY is configured (subscription mode).
    "anthropic/claude-haiku-4-5-20251001",
    "gemini/gemini-2.5-flash-lite",  # non-thinking, fastest, cheapest external
    "groq/llama-3.3-70b-versatile",
    "openai/gpt-4o-mini",
    "deepseek/deepseek-chat",
    "mistral/mistral-small-latest",
]
# Ollama models (local, free) are prepended by router.py when ollama_base_url
# is configured, so they are tried before any cloud model.

# ── Complexity -> Profile mapping ─────────────────────────────────────────────
# Maps classifier output to routing profile. The rationale is straightforward:
# simple tasks don't need expensive models (budget), moderate tasks benefit
# from mid-tier quality (balanced), and complex tasks warrant frontier models
# (premium). This mapping is the bridge between the classifier and the
# routing table.
COMPLEXITY_TO_PROFILE: dict[Complexity, RoutingProfile] = {
    Complexity.SIMPLE: RoutingProfile.BUDGET,
    Complexity.MODERATE: RoutingProfile.BALANCED,
    Complexity.COMPLEX: RoutingProfile.PREMIUM,
}


def reorder_for_pressure(
    chain: list[str],
    pressure: float,
    profile: "RoutingProfile",
) -> list[str]:
    """Reorder the model chain based on Claude subscription pressure.

    Called for BALANCED and PREMIUM profiles only — BUDGET is excluded because
    Ollama (injected by the router) already handles the free-first rule for
    simple tasks.

    Strategy:
    - **Below 85%**: Claude models move to the front — they're effectively
      free under a Pro/Max subscription.
    - **85–98%**: Claude moves to the end; free models (Codex) first, then
      cheap, then paid externals. Claude stays as a last-resort fallback.
    - **≥ 99% (hard cap)**: Claude is removed entirely from the chain to
      guarantee the weekly/session limit is never crossed.

    RESEARCH chains are excluded (caller's responsibility) because Perplexity
    must stay first regardless of quota state.

    Args:
        chain: Ordered list of model IDs from the routing table.
        pressure: Current Claude ``highest_pressure`` (raw max of session/weekly,
            0.0–1.0). Use the raw value, not ``effective_pressure``, so the
            99% hard cap is enforced regardless of imminent resets.
        profile: Routing profile — BUDGET is a no-op (pass-through).

    Returns:
        Reordered list, possibly with Claude models removed at ≥ 99%.
    """
    claude_models = [m for m in chain if m in _CLAUDE_MODELS]
    other_models = [m for m in chain if m not in _CLAUDE_MODELS]

    if pressure >= 0.99:
        # Hard cap: never touch Claude quota.
        # Order: Codex (free) → Ollama injected by router → cheap → paid.
        try:
            from llm_router.codex_agent import is_codex_available
            codex_available = is_codex_available()
        except Exception:
            codex_available = False

        def _hard_cap_priority(m: str) -> int:
            if m in _FREE_EXTERNAL_MODELS and codex_available:
                return 0
            if m in _CHEAP_MODELS:
                return 1
            return 2

        other_models.sort(key=_hard_cap_priority)
        return other_models

    if pressure < 0.85:
        # Quota available: Claude is free, put it first
        return claude_models + other_models

    # 85–98%: quota tightening — externals first, Claude as last resort
    try:
        from llm_router.codex_agent import is_codex_available
        codex_available = is_codex_available()
    except Exception:
        codex_available = False

    def _priority(m: str) -> int:
        if m in _FREE_EXTERNAL_MODELS and codex_available:
            return 0   # Codex: free via OpenAI subscription
        if m in _CHEAP_MODELS:
            return 1   # Gemini Flash, Groq, DeepSeek, etc.
        return 2       # paid: GPT-4o, Gemini Pro, o3, etc.

    other_models.sort(key=_priority)
    return other_models + claude_models


def complexity_to_profile(complexity: Complexity) -> RoutingProfile:
    """Map a complexity level to the appropriate routing profile.

    Args:
        complexity: The classified complexity tier.

    Returns:
        The routing profile that best matches the complexity level.
    """
    return COMPLEXITY_TO_PROFILE[complexity]


def get_model_chain(
    profile: RoutingProfile,
    task_type: TaskType,
    failure_rates: dict[str, float] | None = None,
    latency_stats: "dict[str, dict] | None" = None,
) -> list[str]:
    """Get the ordered model preference chain for a profile + task type.

    Falls back to ``["anthropic/claude-sonnet-4-6"]`` if no entry exists.

    Applies two dynamic reorderings in sequence:
    1. Benchmark ordering — surface models with better benchmark scores,
       incorporating failure-rate and latency penalties when pre-fetched dicts
       are provided (avoids the sync/async conflict in penalty functions).
    2. Pressure reordering — when Claude quota is ≥ 85%, demote Claude
       models and promote free/cheap alternatives (see ``reorder_for_pressure``).

    RESEARCH chains apply pressure reordering only to the non-Perplexity tail
    so Perplexity stays first (the only web-grounded option) while still
    demoting Claude models when quota is tight.

    Args:
        profile: The routing profile (budget/balanced/premium).
        task_type: The task type.
        failure_rates: Pre-fetched dict of ``{model: failure_rate}`` from
            ``cost.get_model_failure_rates()``. Passed into benchmark ordering
            to enable penalty scoring without a sync DB call.
        latency_stats: Pre-fetched dict of ``{model: {"p50", "p95", "count"}}``
            from ``cost.get_model_latency_stats()``. Same purpose.

    Returns:
        Ordered list of model IDs to try, best-fit first.
    """
    static_chain = ROUTING_TABLE.get((profile, task_type), ["anthropic/claude-sonnet-4-6"])

    # Media tasks: no benchmark data, no pressure reordering — use static order.
    if task_type in {TaskType.IMAGE, TaskType.VIDEO, TaskType.AUDIO}:
        return static_chain

    try:
        from llm_router.claude_usage import get_claude_pressure
        pressure = get_claude_pressure()
    except Exception:
        pressure = 0.0

    # Research: Perplexity must stay first (web-grounded). Skip benchmark
    # reordering (benchmarks don't score Perplexity), but apply pressure
    # reordering to the non-Perplexity tail so Claude is demoted when quota
    # is tight.
    if task_type == TaskType.RESEARCH:
        perp = [m for m in static_chain if "perplexity" in m]
        tail = [m for m in static_chain if "perplexity" not in m]
        try:
            tail = reorder_for_pressure(tail, pressure, profile)
        except Exception as _e:
            log.warning("Pressure reordering failed for RESEARCH tail — using static order: %s", _e)
        return perp + tail

    # BUDGET: skip benchmark reordering — static chain already ordered correctly
    # (Haiku first for CODE, cheap-first for others). Ollama is prepended by the
    # router when configured; when it's not, Haiku must lead for CODE tasks.
    chain = static_chain
    if profile != RoutingProfile.BUDGET:
        try:
            from llm_router.benchmarks import apply_benchmark_ordering
            chain = apply_benchmark_ordering(
                chain, task_type, profile,
                failure_rates=failure_rates,
                latency_stats=latency_stats,
            )
        except Exception as _e:
            log.warning("Benchmark ordering failed — using static chain: %s", _e)

    try:
        chain = reorder_for_pressure(chain, pressure, profile)
    except Exception as _e:
        log.warning("Pressure reordering failed — using static chain order: %s", _e)

    return chain


def provider_from_model(model: str) -> str:
    """Extract the provider name from a ``provider/model`` string.

    Args:
        model: Model identifier (e.g. ``"openai/gpt-4o"``).

    Returns:
        Provider name (e.g. ``"openai"``), or ``"unknown"`` if the string
        has no ``/`` separator.
    """
    return model.split("/")[0] if "/" in model else "unknown"
