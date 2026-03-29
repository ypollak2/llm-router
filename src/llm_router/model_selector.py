"""Smart model selector — picks the optimal Claude Code model based on
complexity, budget pressure, quality mode, and minimum model floor."""

from __future__ import annotations

from llm_router.types import (
    CLAUDE_MODELS, COMPLEXITY_BASE_MODEL, ClassificationResult,
    Complexity, QualityMode, RoutingRecommendation,
)


# Budget pressure thresholds — late safety net only.
# Primary routing is complexity-based (simple→haiku, moderate→sonnet, complex→opus).
# Downshift only kicks in when limits are genuinely running out.
PRESSURE_THRESHOLDS = [
    (0.85, 0),   # 0-85% used: no downshift — complexity routing handles it
    (0.95, 1),   # 85-95% used: downshift by 1 (opus→sonnet, sonnet→haiku)
    (1.00, 2),   # 95-100%: downshift by 2 (opus→haiku)
]


def _model_index(model: str) -> int:
    """Get index of a model in CLAUDE_MODELS (0=haiku, 1=sonnet, 2=opus)."""
    try:
        return CLAUDE_MODELS.index(model)
    except ValueError:
        return 1  # default to sonnet


def _downshift_amount(budget_pct: float) -> int:
    """How many tiers to downshift based on budget usage percentage."""
    for threshold, shift in PRESSURE_THRESHOLDS:
        if budget_pct <= threshold:
            return shift
    return 2  # max downshift if over 100%


def select_model(
    classification: ClassificationResult,
    budget_pct_used: float,
    quality_mode: QualityMode = QualityMode.BALANCED,
    min_model: str = "haiku",
) -> RoutingRecommendation:
    """Select the optimal Claude Code model.

    Args:
        classification: The complexity classification result.
        budget_pct_used: 0.0-1.0+ how much of the daily token budget is used.
            0.0 means unlimited budget (no downshift).
        quality_mode: User's quality preference.
        min_model: Minimum model floor — never route below this.
    """
    complexity = classification.complexity.value
    base_model = COMPLEXITY_BASE_MODEL[complexity]
    base_idx = _model_index(base_model)
    min_idx = _model_index(min_model)

    # Quality mode overrides
    if quality_mode == QualityMode.BEST:
        # Always use opus
        recommended_idx = 2
        reasoning = "quality_mode=best: using strongest model"
    elif quality_mode == QualityMode.CONSERVE:
        # Use min_model unless complexity demands more
        recommended_idx = max(min_idx, base_idx - 1)
        reasoning = f"quality_mode=conserve: using cheapest viable ({CLAUDE_MODELS[recommended_idx]})"
    else:
        # Balanced: complexity picks the model, budget is a late safety net
        recommended_idx = base_idx
        reasoning = f"{base_model} matches {complexity} complexity"

        if budget_pct_used > 0:
            shift = _downshift_amount(budget_pct_used)
            if shift > 0:
                recommended_idx = max(0, base_idx - shift)
                reasoning = (
                    f"budget safety ({budget_pct_used:.0%} used): "
                    f"downshifted {base_model}→{CLAUDE_MODELS[recommended_idx]}"
                )

    # Enforce minimum model floor
    if recommended_idx < min_idx:
        recommended_idx = min_idx
        reasoning += f" (raised to min_model={min_model})"

    recommended = CLAUDE_MODELS[recommended_idx]
    was_downshifted = recommended != base_model and quality_mode == QualityMode.BALANCED

    return RoutingRecommendation(
        classification=classification,
        recommended_model=recommended,
        base_model=base_model,
        budget_pct_used=budget_pct_used,
        was_downshifted=was_downshifted,
        quality_mode=quality_mode,
        min_model=min_model,
        reasoning=reasoning,
    )
