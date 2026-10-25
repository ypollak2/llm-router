"""Unified Scorer — phase 5 of the Adaptive Universal Router v5.0.

Computes a single composite score [0.0–1.0] for each available model given:
  - task type and complexity
  - benchmark quality score (from Phase 4 registry)
  - budget pressure (from Phase 2 Budget Oracle)
  - measured latency (from local routing history)
  - user acceptance rate (from llm_rate feedback)

The scoring formula weights these four dimensions according to ``COMPLEXITY_WEIGHTS``
from ``types.py``.  Simple tasks prioritise budget availability (cheap is good
enough); complex reasoning tasks prioritise quality (wrong answer costs more than
a better model).

Public API
----------
score_model(model_id, task_type, complexity)
    → ScoredModel   (single model, sync-safe)

score_all_models(models, task_type, complexity)
    → list[ScoredModel] sorted best-first   (async, fetches data in parallel)
"""

from __future__ import annotations

import asyncio
from typing import Sequence

from llm_router.benchmarks import (
    get_quality_score,
    get_model_failure_penalty,
    get_model_latency_penalty,
    get_model_acceptance_penalty,
)
from llm_router.budget import get_model_pressure
from llm_router.logging import get_logger
from llm_router.tracing import set_span_attributes, traced_span
from llm_router.types import (
    COMPLEXITY_WEIGHTS,
    ModelCapability,
    ScoredModel,
)

log = get_logger("llm_router.scorer")

# ── Score component bounds ────────────────────────────────────────────────────
# quality_score  → from get_quality_score()       0.0–1.0 (higher = better)
# budget_score   → 1.0 - budget_pressure          0.0–1.0 (higher = less pressure)
# latency_score  → 1.0 - latency_penalty          0.5–1.0 (1.0 = fast)
# acceptance_score → 1.0 - acceptance_penalty     0.6–1.0 (1.0 = loved by users)


def _budget_score(pressure: float) -> float:
    """Convert budget pressure [0.0–1.0] to an availability score [0.0–1.0]."""
    return max(0.0, 1.0 - pressure)


def _latency_score(model_id: str, task_type: str) -> float:
    """Compute latency component using the existing penalty function."""
    penalty = get_model_latency_penalty(model_id, task_type)
    return max(0.0, 1.0 - penalty)


def _acceptance_score(model_id: str, acceptance_scores: dict[str, float] | None) -> float:
    """Compute acceptance component from llm_rate user feedback."""
    penalty = get_model_acceptance_penalty(model_id, acceptance_scores=acceptance_scores)
    return max(0.0, 1.0 - penalty)


def _quality_score_adjusted(
    model_id: str,
    task_type: str,
    failure_rates: dict[str, float] | None = None,
) -> float:
    """Quality score after applying local failure-rate penalty."""
    base_quality = get_quality_score(model_id, task_type)
    failure_penalty = get_model_failure_penalty(model_id, task_type, failure_rates=failure_rates)
    return base_quality * (1.0 - failure_penalty)


def score_model(
    model_id: str,
    task_type: str,
    complexity: str,
    *,
    pressure: float = 0.0,
    failure_rates: dict[str, float] | None = None,
    latency_stats: dict[str, dict] | None = None,
    acceptance_scores: dict[str, float] | None = None,
    capability: ModelCapability | None = None,
) -> ScoredModel:
    """Compute a composite routing score for a single model (sync, no I/O).

    All budget/failure/latency data must be pre-fetched by the caller and
    passed as keyword arguments so this function contains zero I/O and can be
    called safely from any context (sync or async).

    The composite formula:

        score = (quality  × w.quality)
              + (budget   × w.budget)
              + (latency  × w.latency)
              + (accept   × w.acceptance)

    where weights ``w`` come from ``COMPLEXITY_WEIGHTS[complexity]``.

    Args:
        model_id: Full model ID (``"ollama/qwen3:32b"``, ``"openai/gpt-4o"``).
        task_type: Task type string (``"code"``, ``"analyze"``, …).
        complexity: Complexity key (``"simple"``, ``"moderate"``, ``"complex"``,
            ``"deep_reasoning"``).
        pressure: Budget pressure pre-fetched from Budget Oracle (0.0–1.0).
        failure_rates: Pre-fetched failure rates from ``cost.get_model_failure_rates()``.
        latency_stats: Pre-fetched latency stats from ``cost.get_model_latency_stats()``.
        acceptance_scores: Pre-fetched acceptance scores from ``cost.get_model_acceptance_scores()``.
        capability: ModelCapability for this model (used to populate ScoredModel).

    Returns:
        :class:`~llm_router.types.ScoredModel` with all component scores.
    """
    weights = COMPLEXITY_WEIGHTS.get(complexity, COMPLEXITY_WEIGHTS["moderate"])

    q_score = _quality_score_adjusted(model_id, task_type, failure_rates=failure_rates)
    b_score = _budget_score(pressure)
    l_score = _latency_score(model_id, task_type) if latency_stats is None else (
        max(0.0, 1.0 - get_model_latency_penalty(model_id, task_type, latency_stats=latency_stats))
    )
    a_score = _acceptance_score(model_id, acceptance_scores=acceptance_scores)

    composite = (
        q_score * weights.quality
        + b_score * weights.budget
        + l_score * weights.latency
        + a_score * weights.acceptance
    )
    composite = max(0.0, min(1.0, composite))

    # Build a minimal capability stub if none was provided.
    if capability is None:
        from llm_router.types import ProviderTier
        provider = model_id.split("/")[0] if "/" in model_id else model_id
        capability = ModelCapability(
            model_id=model_id,
            provider=provider,
            provider_tier=ProviderTier.CHEAP_PAID,
            task_types=frozenset(),
        )

    return ScoredModel(
        model_id=model_id,
        capability=capability,
        score=composite,
        quality_score=q_score,
        budget_score=b_score,
        latency_score=l_score,
        acceptance_score=a_score,
    )


async def score_all_models(
    models: Sequence[str | ModelCapability],
    task_type: str,
    complexity: str,
) -> list[ScoredModel]:
    """Score all models in parallel and return them sorted best-first.

    Fetches budget pressure, failure rates, latency stats, and acceptance
    scores concurrently, then calls ``score_model()`` for each model (sync,
    no I/O) using the pre-fetched data.

    This is the async entry point used by the Dynamic Chain Builder (Phase 6)
    to produce a ranked list for chain assembly.

    Args:
        models: Sequence of model IDs (strings) or
            :class:`~llm_router.types.ModelCapability` objects.
        task_type: Task type string (``"code"``, ``"analyze"``, …).
        complexity: Complexity key.

    Returns:
        List of :class:`~llm_router.types.ScoredModel` sorted by score
        descending (best first).  Never raises; returns empty list on error.
    """
    with traced_span(
        "score_all_models",
        tracer_name="llm_router.scorer",
        task_type=task_type,
        complexity=complexity,
        input_models=len(models),
    ) as span:
        if not models:
            set_span_attributes(span, scored_models=0)
            return []

        # Normalise to (model_id, capability | None) pairs.
        pairs: list[tuple[str, ModelCapability | None]] = []
        for m in models:
            if isinstance(m, str):
                pairs.append((m, None))
            else:
                pairs.append((m.model_id, m))

        model_ids = [p[0] for p in pairs]
        set_span_attributes(span, candidate_models=model_ids)

        try:
            # Fetch all supporting data concurrently.
            from llm_router.cost import (
                get_model_failure_rates,
                get_model_latency_stats,
                get_model_acceptance_scores,
            )

            failure_rates, latency_stats, acceptance_scores, pressures = await asyncio.gather(
                get_model_failure_rates(window_days=30),
                get_model_latency_stats(window_days=7),
                get_model_acceptance_scores(),
                _fetch_pressures(model_ids),
                return_exceptions=True,
            )

            # Degrade gracefully if any fetch fails.
            if isinstance(failure_rates, Exception):
                failure_rates = {}
            if isinstance(latency_stats, Exception):
                latency_stats = {}
            if isinstance(acceptance_scores, Exception):
                acceptance_scores = {}
            if isinstance(pressures, Exception):
                pressures = {m: 0.0 for m in model_ids}

        except Exception:
            failure_rates = latency_stats = acceptance_scores = {}
            pressures = {m: 0.0 for m in model_ids}

        # Score each model (pure compute, no I/O).
        scored: list[ScoredModel] = []
        for model_id, cap in pairs:
            try:
                sm = score_model(
                    model_id,
                    task_type,
                    complexity,
                    pressure=pressures.get(model_id, 0.0),
                    failure_rates=failure_rates,
                    latency_stats=latency_stats,
                    acceptance_scores=acceptance_scores,
                    capability=cap,
                )
                scored.append(sm)
            except Exception as e:
                log.debug("score_model failed for %s: %s", model_id, e)

        ranked = sorted(scored, key=lambda s: s.score, reverse=True)
        top = ranked[0] if ranked else None
        set_span_attributes(
            span,
            scored_models=len(ranked),
            top_model=top.model_id if top else None,
            top_score=top.score if top else None,
        )
        return ranked


async def _fetch_pressures(model_ids: list[str]) -> dict[str, float]:
    """Fetch budget pressure for each model concurrently."""
    pressures = await asyncio.gather(
        *[get_model_pressure(m) for m in model_ids],
        return_exceptions=True,
    )
    return {
        m: p if isinstance(p, float) else 0.0
        for m, p in zip(model_ids, pressures)
    }
