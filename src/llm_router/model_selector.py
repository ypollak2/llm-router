"""Smart model selector — picks the optimal Claude Code model based on
complexity, budget pressure, quality mode, and minimum model floor.

This module implements the decision tree for selecting among Claude's three
model tiers (haiku/sonnet/opus) when used within Claude Code subscriptions.
It is separate from the multi-provider routing in ``router.py`` — this handles
the *internal* Claude model choice, while ``router.py`` handles the *external*
multi-provider fallback chain.

Decision priority: quality_mode override > complexity base > budget downshift > min_model floor.
"""

from __future__ import annotations

from llm_router.types import (
    CLAUDE_MODELS, COMPLEXITY_BASE_MODEL, ClassificationResult,
    QualityMode, RoutingRecommendation,
)


# Budget pressure thresholds — a late safety net, NOT the primary routing signal.
#
# Primary routing is complexity-based (simple->haiku, moderate->sonnet, complex->opus).
# These thresholds only activate when the daily token budget is running low,
# preventing expensive opus calls from exhausting the budget entirely.
#
# The 85% threshold was chosen because Claude Code subscription limits are
# generous enough that most users never reach it — but when they do, an early
# downshift preserves enough budget for the rest of the day.
#
# Format: (upper_bound_pct, tiers_to_downshift)
PRESSURE_THRESHOLDS = [
    (0.85, 0),   # 0-85% used: no downshift — complexity routing handles it
    (0.95, 1),   # 85-95% used: downshift by 1 (opus->sonnet, sonnet->haiku)
    (1.00, 2),   # 95-100%: downshift by 2 (opus->haiku)
]


def _model_index(model: str) -> int:
    """Map a model name to its rank in CLAUDE_MODELS (0=haiku, 1=sonnet, 2=opus).

    Falls back to sonnet (index 1) for unknown model names, which is the
    safest middle-ground default.

    Args:
        model: Short model name (e.g. ``"haiku"``, ``"sonnet"``, ``"opus"``).

    Returns:
        Integer index into ``CLAUDE_MODELS``.
    """
    try:
        return CLAUDE_MODELS.index(model)
    except ValueError:
        return 1  # default to sonnet


def _downshift_amount(budget_pct: float) -> int:
    """Calculate how many model tiers to downshift based on budget consumption.

    Walks ``PRESSURE_THRESHOLDS`` to find the first bracket that contains
    the current usage percentage, and returns the corresponding shift value.

    Args:
        budget_pct: Fraction of daily token budget consumed (0.0 to 1.0+).

    Returns:
        Number of tiers to subtract from the base model index (0, 1, or 2).
    """
    for threshold, shift in PRESSURE_THRESHOLDS:
        if budget_pct <= threshold:
            return shift
    return 2  # max downshift if over 100%


async def _get_quality_floor(model: str, task_type: str) -> str | None:
    """Return min_model override if model quality is degraded (v6.2 Quality Guard).

    Hard threshold enforcement: if rolling judge score < 0.6 for 5+ calls,
    force min_model up one tier to maintain quality standards.

    Args:
        model: Model identifier (e.g., 'claude-sonnet-4-6')
        task_type: Task type for context (e.g., 'code', 'research')

    Returns:
        Upgraded min_model string if quality floor violated, else None.
    """
    try:
        from llm_router.judge import get_judge_scores_for_model
        scores = await get_judge_scores_for_model(model, days=3)

        # Quality Guard threshold: < 0.6 avg with >= 5 samples
        sample_count = scores.get("sample_count", 0)
        avg_score = scores.get("avg_score", 1.0)

        if sample_count >= 5 and avg_score < 0.6:
            # Escalate to next tier
            current_idx = _model_index(model.split("-")[-1].lower())
            if current_idx < 2:  # Not already at opus
                upgraded_idx = min(current_idx + 1, 2)  # upgrade by one tier, cap at opus
                return CLAUDE_MODELS[upgraded_idx]
    except Exception:
        # Quality floor check is best-effort — never block routing
        pass

    return None


async def select_model(
    classification: ClassificationResult,
    budget_pct_used: float,
    quality_mode: QualityMode = QualityMode.BALANCED,
    min_model: str = "haiku",
    trend_pressure: float = 0.0,
) -> RoutingRecommendation:
    """Select the optimal Claude Code model tier (haiku/sonnet/opus).

    Decision tree:
      1. **Quality mode override** — ``BEST`` always returns opus;
         ``CONSERVE`` returns the cheapest model that meets complexity needs.
      2. **Complexity base** — in ``BALANCED`` mode, complexity picks the
         starting model (simple->haiku, moderate->sonnet, complex->opus).
      3. **Budget + trend downshift** — if ``budget_pct_used > 0`` or
         ``trend_pressure > 0`` and thresholds are exceeded, shift down 1-2 tiers.
         Trend pressure (quality declining) is blended with budget pressure.
      4. **Min model floor** — the final result is never below ``min_model``,
         ensuring a quality floor even under heavy budget pressure.

    Args:
        classification: The complexity classification result from the classifier.
        budget_pct_used: Fraction of daily token budget consumed (0.0-1.0+).
            A value of 0.0 means unlimited budget (no downshift applied).
        quality_mode: User's quality preference — controls whether complexity
            or cost is prioritized.
        min_model: Minimum model floor — the selector never routes below this
            tier, even under heavy budget pressure.
        trend_pressure: Optional quality trend pressure (0.0-0.3). When accuracy
            is declining, soft-escalates model tier independent of budget.

    Returns:
        A ``RoutingRecommendation`` containing the selected model, the base
        model that complexity alone would have chosen, whether a budget
        downshift occurred, and a human-readable reasoning string.
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
        # Balanced: complexity picks the model, budget/trend are late safety nets
        recommended_idx = base_idx
        reasoning = f"{base_model} matches {complexity} complexity"

        # Apply budget pressure (primary) and trend pressure (soft boost)
        # Trend adds to budget pressure to escalate when quality is declining
        # Formula: keep budget_pct as-is, add trend as additional pressure
        effective_pressure = budget_pct_used + (trend_pressure * 0.2)
        effective_pressure = min(1.0, effective_pressure)  # cap at 100%
        
        if effective_pressure > 0:
            shift = _downshift_amount(effective_pressure)
            if shift > 0:
                recommended_idx = max(0, base_idx - shift)
                pressure_source = []
                if budget_pct_used > 0:
                    pressure_source.append(f"budget {budget_pct_used:.0%}")
                if trend_pressure > 0:
                    pressure_source.append(f"trend {trend_pressure:.2f}")
                reasoning = (
                    f"{', '.join(pressure_source)}: "
                    f"downshifted {base_model}→{CLAUDE_MODELS[recommended_idx]}"
                )

    # Quality Guard: check if recommended model has degraded quality (v6.2)
    # Upgrade min_model floor if quality is below 0.6 with sufficient samples
    recommended_name = CLAUDE_MODELS[recommended_idx]
    task_type = classification.inferred_task_type.value if classification.inferred_task_type else "query"
    quality_floor_upgrade = await _get_quality_floor(recommended_name, task_type)
    if quality_floor_upgrade:
        quality_floor_idx = _model_index(quality_floor_upgrade)
        if quality_floor_idx > min_idx:
            min_idx = quality_floor_idx
            min_model = quality_floor_upgrade
            reasoning += f" (quality floor: {recommended_name} degraded, raised min_model to {min_model})"

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
