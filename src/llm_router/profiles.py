"""Routing profiles — maps (profile, task_type) to ordered model preferences."""

from __future__ import annotations

from llm_router.types import Complexity, RoutingProfile, TaskType

# Each entry is an ordered list of model IDs (LiteLLM format for text,
# provider/model for media). Router tries them in order, falling back on failure.

ROUTING_TABLE: dict[tuple[RoutingProfile, TaskType], list[str]] = {
    # ═══════════════════════════════════════════════════════════════════
    # BUDGET — cheapest models, good enough for most tasks
    # ═══════════════════════════════════════════════════════════════════
    (RoutingProfile.BUDGET, TaskType.QUERY): [
        "gemini/gemini-2.5-flash",
        "groq/llama-3.3-70b-versatile",
        "deepseek/deepseek-chat",
        "openai/gpt-4o-mini",
    ],
    (RoutingProfile.BUDGET, TaskType.RESEARCH): [
        "perplexity/sonar",
        "gemini/gemini-2.5-flash",
    ],
    (RoutingProfile.BUDGET, TaskType.GENERATE): [
        "gemini/gemini-2.5-flash",
        "deepseek/deepseek-chat",
        "mistral/mistral-small-latest",
        "openai/gpt-4o-mini",
    ],
    (RoutingProfile.BUDGET, TaskType.ANALYZE): [
        "gemini/gemini-2.5-flash",
        "deepseek/deepseek-reasoner",
        "groq/llama-3.3-70b-versatile",
        "openai/gpt-4o-mini",
    ],
    (RoutingProfile.BUDGET, TaskType.CODE): [
        "deepseek/deepseek-chat",
        "gemini/gemini-2.5-flash",
        "groq/llama-3.3-70b-versatile",
        "openai/gpt-4o-mini",
    ],
    (RoutingProfile.BUDGET, TaskType.IMAGE): [
        "fal/flux-dev",
        "stability/stable-diffusion-3",
        "openai/dall-e-2",
    ],
    (RoutingProfile.BUDGET, TaskType.VIDEO): [
        "fal/minimax-video",
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
        "openai/dall-e-3",
        "stability/stable-diffusion-3",
    ],
    (RoutingProfile.BALANCED, TaskType.VIDEO): [
        "fal/kling-video",
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
        "openai/dall-e-3",
        "fal/flux-pro",
        "stability/stable-diffusion-3-ultra",
    ],
    (RoutingProfile.PREMIUM, TaskType.VIDEO): [
        "runway/gen3a",
        "fal/kling-video",
    ],
    (RoutingProfile.PREMIUM, TaskType.AUDIO): [
        "elevenlabs/eleven_multilingual_v2",
        "openai/tts-1-hd",
    ],
}


# ── Classifier model preferences (cheapest/fastest first) ────────────────────
CLASSIFIER_MODELS: list[str] = [
    "groq/llama-3.3-70b-versatile",
    "openai/gpt-4o-mini",
    "deepseek/deepseek-chat",
    "mistral/mistral-small-latest",
    "gemini/gemini-2.5-flash",  # thinking model — truncated JSON handled by parser
]

# ── Complexity → Profile mapping ─────────────────────────────────────────────
COMPLEXITY_TO_PROFILE: dict[Complexity, RoutingProfile] = {
    Complexity.SIMPLE: RoutingProfile.BUDGET,
    Complexity.MODERATE: RoutingProfile.BALANCED,
    Complexity.COMPLEX: RoutingProfile.PREMIUM,
}


def complexity_to_profile(complexity: Complexity) -> RoutingProfile:
    """Map a complexity level to the appropriate routing profile."""
    return COMPLEXITY_TO_PROFILE[complexity]


def get_model_chain(profile: RoutingProfile, task_type: TaskType) -> list[str]:
    """Get the ordered model preference chain for a profile + task type."""
    return ROUTING_TABLE.get((profile, task_type), ["openai/gpt-4o"])


def provider_from_model(model: str) -> str:
    """Extract provider name from model string (e.g. 'openai/gpt-4o' → 'openai')."""
    return model.split("/")[0] if "/" in model else "unknown"
