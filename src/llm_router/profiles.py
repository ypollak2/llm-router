"""Routing profiles — maps (profile, task_type) to ordered model preferences."""

from __future__ import annotations

from llm_router.types import RoutingProfile, TaskType

# Each entry is an ordered list of model IDs (LiteLLM format).
# Router tries them in order, falling back on failure.
ROUTING_TABLE: dict[tuple[RoutingProfile, TaskType], list[str]] = {
    # ── Budget: cheapest models, good enough for most tasks ──
    (RoutingProfile.BUDGET, TaskType.QUERY): [
        "gemini/gemini-2.0-flash",
        "openai/gpt-4o-mini",
    ],
    (RoutingProfile.BUDGET, TaskType.RESEARCH): [
        "perplexity/sonar",
        "gemini/gemini-2.0-flash",
    ],
    (RoutingProfile.BUDGET, TaskType.GENERATE): [
        "gemini/gemini-2.0-flash",
        "openai/gpt-4o-mini",
    ],
    (RoutingProfile.BUDGET, TaskType.ANALYZE): [
        "gemini/gemini-2.0-flash",
        "openai/gpt-4o-mini",
    ],
    (RoutingProfile.BUDGET, TaskType.CODE): [
        "gemini/gemini-2.0-flash",
        "openai/gpt-4o-mini",
    ],
    # ── Balanced: quality/cost sweet spot ──
    (RoutingProfile.BALANCED, TaskType.QUERY): [
        "openai/gpt-4o",
        "gemini/gemini-2.0-flash",
    ],
    (RoutingProfile.BALANCED, TaskType.RESEARCH): [
        "perplexity/sonar-pro",
        "perplexity/sonar",
    ],
    (RoutingProfile.BALANCED, TaskType.GENERATE): [
        "gemini/gemini-2.5-pro",
        "openai/gpt-4o",
    ],
    (RoutingProfile.BALANCED, TaskType.ANALYZE): [
        "openai/gpt-4o",
        "gemini/gemini-2.5-pro",
    ],
    (RoutingProfile.BALANCED, TaskType.CODE): [
        "openai/gpt-4o",
        "gemini/gemini-2.5-pro",
    ],
    # ── Premium: best available per task ──
    (RoutingProfile.PREMIUM, TaskType.QUERY): [
        "openai/o3",
        "gemini/gemini-2.5-pro",
    ],
    (RoutingProfile.PREMIUM, TaskType.RESEARCH): [
        "perplexity/sonar-pro",
        "perplexity/sonar",
    ],
    (RoutingProfile.PREMIUM, TaskType.GENERATE): [
        "gemini/gemini-2.5-pro",
        "openai/o3",
    ],
    (RoutingProfile.PREMIUM, TaskType.ANALYZE): [
        "openai/o3",
        "gemini/gemini-2.5-pro",
    ],
    (RoutingProfile.PREMIUM, TaskType.CODE): [
        "openai/o3",
        "openai/gpt-4o",
    ],
}


def get_model_chain(profile: RoutingProfile, task_type: TaskType) -> list[str]:
    """Get the ordered model preference chain for a profile + task type."""
    return ROUTING_TABLE.get((profile, task_type), ["openai/gpt-4o"])


def provider_from_model(model: str) -> str:
    """Extract provider name from LiteLLM model string."""
    return model.split("/")[0] if "/" in model else "unknown"
