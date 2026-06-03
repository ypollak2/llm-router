"""Outcome telemetry — aggregate (profile, subject, model) stats from routing_decisions.

Plan 07 — Category E (Outcome telemetry & multi-armed bandit learning).

Cat E does not introduce a new ``routing_outcomes`` table. The existing
``routing_decisions`` table in :mod:`llm_router.cost` already captures every
routed call with ``profile``, ``complexity``, ``final_model``, ``success``,
``cost_usd``, and ``latency_ms``. The Plan 07 spec proposed a parallel table;
in practice that would duplicate ~7 columns and force two write-sites to stay
in sync.

This module is the *read side*: it groups ``routing_decisions`` rows by
``(profile, subject, final_model)`` and computes a tiny normalized
:class:`ModelStats` record per candidate that the bandit
(:mod:`llm_router.bandit`) consumes when reordering the candidate chain.

Design principles mirror :mod:`llm_router.calibration`:

* Pure read path — no global mutable state, no caches; the DB index
  (``idx_routing_bandit``) keeps the query cheap.
* Permissive on failure — DB unavailable returns an empty list. The bandit
  treats "no data" identically to "insufficient data" and falls back to the
  static workhorse order, so a degraded DB never breaks routing.
* Decoupled from the writer. The write-site (``cost.log_routing_decision``)
  already persists every column we read; this module never writes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import aiosqlite

from llm_router.cost import _get_db

__all__ = [
    "ModelStats",
    "aggregate_stats",
    "MIN_SAMPLES_FOR_SIGNAL",
]


log = logging.getLogger("llm_router.telemetry")


# Plan 07 says "use static workhorse order while n_samples < 30 per candidate".
# Below this threshold variance dominates the success_rate estimate and the
# bandit would just chase noise.
MIN_SAMPLES_FOR_SIGNAL = 30


@dataclass(frozen=True)
class ModelStats:
    """Empirical performance of a model on (profile, subject) routes.

    Computed by :func:`aggregate_stats` from rows in ``routing_decisions``.
    ``expected_value`` is the bandit's optimization target — it bakes both
    quality (``success_rate``) and cost into a single comparable scalar so
    candidates with different price points sort correctly.

    The cost denominator is floored at ``1e-9`` so free providers (Ollama,
    Codex) get a very large but finite ``expected_value`` rather than ``inf``.
    Without the floor, every successful Ollama call would dominate every
    rank-by-EV comparison and the bandit would never explore alternatives.
    """

    model: str
    n_samples: int
    success_rate: float
    avg_cost: float
    avg_latency_ms: float

    @property
    def expected_value(self) -> float:
        """Success-per-dollar — bandit ranks candidates by this."""
        return self.success_rate / max(self.avg_cost, 1e-9)


async def aggregate_stats(
    profile: str,
    subject: str,
    candidates: list[str],
    *,
    window_days: int = 30,
) -> list[ModelStats]:
    """Return per-candidate empirical stats for a (profile, subject) bucket.

    Args:
        profile: Routing profile name (e.g. ``"balanced"``). Strings, not enums,
            so callers from either the public API or internal hot paths can
            invoke without enum import gymnastics.
        subject: Subject name (e.g. ``"code"``, ``"general"``). Empty string or
            ``None`` is normalized to ``"general"`` so legacy rows written
            before the Cat E migration still aggregate cleanly.
        candidates: The model identifiers currently being considered. The
            query restricts to these so unrelated history is never loaded.
        window_days: How far back to aggregate. Older rows are excluded so
            stats track *current* model performance rather than ancient
            failures from deprecated checkpoints.

    Returns:
        ``ModelStats`` for each candidate that has at least one row in the
        window. Order matches the SQL grouping — *not* the input order — so
        callers must key by ``stats.model``. Candidates with zero rows are
        omitted; the bandit treats them as "insufficient data" and falls
        through to the static order.
    """
    if not candidates:
        return []

    subject_key = subject or "general"
    placeholders = ",".join("?" for _ in candidates)
    sql = f"""
        SELECT final_model,
               COUNT(*) AS n,
               AVG(CASE WHEN success = 1 THEN 1.0 ELSE 0.0 END) AS success_rate,
               COALESCE(AVG(cost_usd), 0.0) AS avg_cost,
               COALESCE(AVG(latency_ms), 0.0) AS avg_latency
          FROM routing_decisions
         WHERE profile = ?
           AND (subject = ? OR (subject IS NULL AND ? = 'general'))
           AND final_model IN ({placeholders})
           AND timestamp >= datetime('now', ?)
         GROUP BY final_model
    """
    params: tuple = (
        profile,
        subject_key,
        subject_key,
        *candidates,
        f"-{int(window_days)} days",
    )

    try:
        db = await _get_db()
    except Exception as err:  # pragma: no cover — DB unavailable path
        log.debug("aggregate_stats: db unavailable (%s) — empty stats", err)
        return []

    try:
        async with db.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
    except aiosqlite.OperationalError as err:
        # Pre-migration DBs lack the ``subject`` column. Returning empty is
        # correct: the bandit then falls through to static ordering, exactly
        # the behaviour we want before Cat E telemetry has accumulated.
        log.debug("aggregate_stats: query failed (%s) — empty stats", err)
        return []
    finally:
        await db.close()

    return [
        ModelStats(
            model=row[0],
            n_samples=int(row[1] or 0),
            success_rate=float(row[2] or 0.0),
            avg_cost=float(row[3] or 0.0),
            avg_latency_ms=float(row[4] or 0.0),
        )
        for row in rows
    ]
