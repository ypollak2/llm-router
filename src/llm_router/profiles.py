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

from llm_router.types import Complexity, RoutingProfile, TaskType

# Master routing table: maps (profile, task_type) -> ordered model chain.
# Each entry is a list of model IDs in LiteLLM's "provider/model" format.
# The router tries models in order, falling back to the next on failure or
# rate-limiting. Models are ordered by preference within each tier (best
# fit first, broadest fallback last).

ROUTING_TABLE: dict[tuple[RoutingProfile, TaskType], list[str]] = {
    # ═══════════════════════════════════════════════════════════════════
    # BUDGET — cheapest models, good enough for most tasks
    # ═══════════════════════════════════════════════════════════════════
    (RoutingProfile.BUDGET, TaskType.QUERY): [
        "gemini/gemini-2.5-flash",
        "groq/llama-3.3-70b-versatile",
        "deepseek/deepseek-chat",
        "anthropic/claude-haiku-4-5-20251001",
        "openai/gpt-4o-mini",
    ],
    (RoutingProfile.BUDGET, TaskType.RESEARCH): [
        "perplexity/sonar",
        "gemini/gemini-2.5-flash",
        "anthropic/claude-haiku-4-5-20251001",
        "openai/gpt-4o-mini",
    ],
    (RoutingProfile.BUDGET, TaskType.GENERATE): [
        "gemini/gemini-2.5-flash",
        "deepseek/deepseek-chat",
        "anthropic/claude-haiku-4-5-20251001",
        "mistral/mistral-small-latest",
        "openai/gpt-4o-mini",
    ],
    (RoutingProfile.BUDGET, TaskType.ANALYZE): [
        "gemini/gemini-2.5-flash",
        "deepseek/deepseek-reasoner",
        "anthropic/claude-haiku-4-5-20251001",
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
    # ═══════════════════════════════════════════════════════════════════
    (RoutingProfile.BALANCED, TaskType.QUERY): [
        "openai/gpt-4o",
        "anthropic/claude-sonnet-4-6",
        "gemini/gemini-2.5-pro",
        "mistral/mistral-large-latest",
    ],
    (RoutingProfile.BALANCED, TaskType.RESEARCH): [
        "perplexity/sonar-pro",
        "perplexity/sonar",
        "gemini/gemini-2.5-pro",
        "openai/gpt-4o",
    ],
    (RoutingProfile.BALANCED, TaskType.GENERATE): [
        "gemini/gemini-2.5-pro",
        "anthropic/claude-sonnet-4-6",
        "openai/gpt-4o",
        "cohere/command-r-plus",
    ],
    (RoutingProfile.BALANCED, TaskType.ANALYZE): [
        "openai/gpt-4o",
        "anthropic/claude-sonnet-4-6",
        "gemini/gemini-2.5-pro",
        "deepseek/deepseek-reasoner",
    ],
    (RoutingProfile.BALANCED, TaskType.CODE): [
        "anthropic/claude-sonnet-4-6",
        "openai/gpt-4o",
        "gemini/gemini-2.5-pro",
        "deepseek/deepseek-chat",
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
    # ═══════════════════════════════════════════════════════════════════
    (RoutingProfile.PREMIUM, TaskType.QUERY): [
        "openai/o3",
        "anthropic/claude-opus-4-6",
        "gemini/gemini-2.5-pro",
        "xai/grok-3",
    ],
    (RoutingProfile.PREMIUM, TaskType.RESEARCH): [
        "perplexity/sonar-pro",
        "perplexity/sonar",
        "openai/o3",
        "gemini/gemini-2.5-pro",
    ],
    (RoutingProfile.PREMIUM, TaskType.GENERATE): [
        "anthropic/claude-opus-4-6",
        "gemini/gemini-2.5-pro",
        "openai/o3",
    ],
    (RoutingProfile.PREMIUM, TaskType.ANALYZE): [
        "openai/o3",
        "anthropic/claude-opus-4-6",
        "deepseek/deepseek-reasoner",
        "gemini/gemini-2.5-pro",
    ],
    (RoutingProfile.PREMIUM, TaskType.CODE): [
        "anthropic/claude-opus-4-6",
        "openai/o3",
        "openai/gpt-4o",
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
    "gemini/gemini-2.5-flash-lite",  # non-thinking, fastest, cheapest
    "groq/llama-3.3-70b-versatile",
    "openai/gpt-4o-mini",
    "deepseek/deepseek-chat",
    "mistral/mistral-small-latest",
]

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


def complexity_to_profile(complexity: Complexity) -> RoutingProfile:
    """Map a complexity level to the appropriate routing profile.

    Args:
        complexity: The classified complexity tier.

    Returns:
        The routing profile that best matches the complexity level.
    """
    return COMPLEXITY_TO_PROFILE[complexity]


def get_model_chain(profile: RoutingProfile, task_type: TaskType) -> list[str]:
    """Get the ordered model preference chain for a profile + task type.

    Falls back to ``["openai/gpt-4o"]`` if no entry exists in the routing
    table, which should only happen if a new TaskType is added without
    updating the table.

    Args:
        profile: The routing profile (budget/balanced/premium).
        task_type: The task type (query/research/generate/analyze/code/image/video/audio).

    Returns:
        Ordered list of model IDs to try, best-fit first.
    """
    return ROUTING_TABLE.get((profile, task_type), ["openai/gpt-4o"])


def provider_from_model(model: str) -> str:
    """Extract the provider name from a ``provider/model`` string.

    Args:
        model: Model identifier (e.g. ``"openai/gpt-4o"``).

    Returns:
        Provider name (e.g. ``"openai"``), or ``"unknown"`` if the string
        has no ``/`` separator.
    """
    return model.split("/")[0] if "/" in model else "unknown"
