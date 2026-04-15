"""Dynamic Chain Builder — core routing engine for Adaptive Universal Router v5.0+.

Assembles a ranked model chain for a given (task_type, complexity) pair using
the output of the Unified Scorer.  This is the primary chain selection mechanism
(always-on as of v5.0; feature flag removed).

Design constraints
------------------
1. **Free-first ordering**: LOCAL tier models always appear before paid-API models
   of equal or lower quality.  This preserves the core cost-saving invariant.
2. **Minimum chain length**: Always include at least ``_MIN_CHAIN_LENGTH`` models
   so there is always a fallback, even if scores are low.
3. **Score floor**: Models below ``_SCORE_FLOOR`` are skipped unless we would
   fall below the minimum chain length.
4. **Offline-safe**: All failures return the static chain from ``profiles.py``
   so routing is never blocked when the dynamic system has an error.
5. **Always-on**: Dynamic chain building is now always active (v5.0).
   Gracefully falls back to static profiles if discovery hasn't populated yet.

Public API
----------
build_chain(task_type, complexity, profile)
    → list[str]   (async, returns model IDs best-first)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from llm_router.logging import get_logger
from llm_router.tracing import set_span_attributes, traced_span
from llm_router.types import LOCAL_PROVIDERS, ScoredModel, TaskType

if TYPE_CHECKING:
    from llm_router.types import RoutingProfile

log = get_logger("llm_router.chain_builder")

# Score below which a model is excluded from the dynamic chain (unless we need
# it to meet minimum length).
_SCORE_FLOOR = 0.30

# Guaranteed minimum number of models in any assembled chain.
_MIN_CHAIN_LENGTH = 2

# Maximum chain length — beyond this, the tail has negligible value.
_MAX_CHAIN_LENGTH = 8


async def build_chain(
    task_type: "TaskType",
    complexity: str,
    profile: "RoutingProfile",
) -> list[str]:
    """Build a ranked model chain for the given task context.

    Dynamic routing is always-on (v5.0+). Discovers available models,
    scores each, and ranks best-first. Falls back gracefully to static
    chain from ``profiles.py`` on any error or when cache is empty.

    Args:
        task_type: The task type being routed (``TaskType.CODE``, etc.).
        complexity: Complexity key (``"simple"``, ``"moderate"``, etc.).
        profile: Routing profile (``BUDGET``, ``BALANCED``, ``PREMIUM``).

    Returns:
        Ordered list of model IDs, best first.  Never empty (falls back to static).
    """
    with traced_span(
        "build_chain",
        tracer_name="llm_router.chain_builder",
        task_type=task_type,
        complexity=complexity,
        profile=profile,
    ) as span:
        try:
            chain = await _build_dynamic_chain(task_type, complexity, profile)
            top = chain[0] if chain else None
            set_span_attributes(span, chain_length=len(chain), top_model=top)
            return chain
        except Exception as e:
            log.debug("dynamic chain builder failed, using static: %s", e)
            chain = _static_chain(task_type, profile)
            set_span_attributes(
                span,
                fallback_reason="dynamic_chain_builder_failed",
                chain_length=len(chain),
                top_model=chain[0] if chain else None,
            )
            return chain


async def _build_dynamic_chain(
    task_type: "TaskType",
    complexity: str,
    profile: "RoutingProfile",
) -> list[str]:
    """Inner implementation — may raise, caller must handle."""
    from llm_router.discover import discover_available_models
    from llm_router.scorer import score_all_models

    # 1. Get available models (30-min cache, instant on warm path).
    capabilities = await discover_available_models()
    if not capabilities:
        return _static_chain(task_type, profile)

    # 2. Filter to models that support this task type.
    task_caps = [
        cap for cap in capabilities.values()
        if not cap.task_types or task_type in cap.task_types
    ]
    if not task_caps:
        task_caps = list(capabilities.values())  # all models as fallback

    # 3. Score all eligible models in parallel.
    scored: list[ScoredModel] = await score_all_models(
        task_caps,
        task_type.value,
        complexity,
    )

    # 4. Split into LOCAL (free) and paid tiers.
    local_scored = [s for s in scored if s.capability.provider in LOCAL_PROVIDERS]
    paid_scored  = [s for s in scored if s.capability.provider not in LOCAL_PROVIDERS]

    # 5. Merge: local first (free-first invariant), then paid, each sorted by score.
    merged = local_scored + paid_scored  # already sorted within groups by scorer

    # 6. Apply score floor (keep at least _MIN_CHAIN_LENGTH models).
    above_floor = [s for s in merged if s.score >= _SCORE_FLOOR]
    if len(above_floor) >= _MIN_CHAIN_LENGTH:
        merged = above_floor
    else:
        # Supplement with best-scoring below-floor models to meet minimum.
        below = [s for s in merged if s.score < _SCORE_FLOOR]
        needed = _MIN_CHAIN_LENGTH - len(above_floor)
        merged = above_floor + below[:needed]

    # 7. Merge scored results with the static fallback tail, then deduplicate
    # while preserving order so repeated models never waste chain slots.
    merged_chain = [s.model_id for s in merged] + _static_chain(task_type, profile)
    deduped: list[str] = []
    seen: set[str] = set()
    for model_id in merged_chain:
        if model_id in seen:
            continue
        seen.add(model_id)
        deduped.append(model_id)
        if len(deduped) >= _MAX_CHAIN_LENGTH:
            break

    return deduped if deduped else _static_chain(task_type, profile)


def _static_chain(task_type: "TaskType", profile: "RoutingProfile") -> list[str]:
    """Return the static chain from profiles.py for the given (task_type, profile)."""
    try:
        from llm_router.profiles import ROUTING_TABLE
        # ROUTING_TABLE keys are (RoutingProfile, TaskType) tuples.
        return list(ROUTING_TABLE.get((profile, task_type), []))
    except Exception:
        return []
