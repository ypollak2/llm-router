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
import os
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


#: What one correct answer is worth, in dollars — the exchange rate between
#: quality and cost in the bandit's reward (T-09).
#:
#: Defaults to roughly the cost of having the premium baseline model answer,
#: which is the honest reference: if routing to a cheaper model cannot beat
#: "just use the good model", it should not route. Raising it makes the bandit
#: more quality-seeking, lowering it more cost-seeking; at 0 it ranks purely by
#: cheapness, which is what the old ratio effectively did.
#: Graded samples required before `judge_mean` outranks the weak usable-rate
#: signal. Below this the mean is noise; above it, it is the better measurement.
MIN_JUDGED_FOR_SIGNAL: int = 20

ANSWER_VALUE_USD: float = float(os.environ.get("LLM_ROUTER_ANSWER_VALUE_USD", "0.05") or 0.05)


@dataclass(frozen=True)
class ModelStats:
    """Empirical performance of a model on (profile, subject) routes.

    Computed by :func:`aggregate_stats` from rows in ``routing_decisions``.
    ``expected_value`` is the bandit's optimization target — it bakes both
    quality (``success_rate``) and cost into a single comparable scalar so
    candidates with different price points sort correctly.
    """

    model: str
    n_samples: int
    #: Share of calls whose response was NON-EMPTY AND NOT A DEFERRAL.
    #:
    #: T-09: this is not a quality measurement and the bandit must not be
    #: described as optimising quality on it. `_response_is_usable` reads the
    #: text for emptiness and refusal markers; a confident, fluent, entirely
    #: wrong answer scores 1.0. It rules out the two worst outcomes and says
    #: nothing about the rest.
    success_rate: float
    avg_cost: float
    avg_latency_ms: float
    #: How many of `n_samples` carry a judge score, and their mean.
    #:
    #: Measured 2026-09-22 on the live ledger: **0 of 1,598**. The judge is
    #: wired (`judge.evaluate_response_async`) and has never produced a row, so
    #: ranking on `judge_mean` alone would be a filter that drops everything.
    #: Carried here so the gap is visible in the data rather than discovered
    #: again, and so the reward upgrades itself the moment grading starts.
    judged_samples: int = 0
    judge_mean: float | None = None

    @property
    def quality_signal(self) -> tuple[float, str]:
        """The best available quality estimate, and WHICH one it is.

        Returns `(value, source)` where source is ``"judge"`` or ``"usable"``.
        The caller gets the number and the provenance together, because a 0.97
        from a grader and a 0.97 from "it wasn't empty" are not the same claim.
        """
        if self.judge_mean is not None and self.judged_samples >= MIN_JUDGED_FOR_SIGNAL:
            return float(self.judge_mean), "judge"
        return self.success_rate, "usable"

    @property
    def success_per_dollar(self) -> float:
        """The OLD optimisation target. Kept for reporting, not for ranking.

        T-09 (audit 2026-09-22). This was ``success_rate / max(avg_cost, 1e-9)``,
        and the 1e-9 floor was documented as a feature — "free providers get a
        very large but finite expected_value". Very large is the problem:

            free,  success 0.50, cost $0        -> 0.50 / 1e-9 = 5.0e8
            paid,  success 0.99, cost $0.01     -> 0.99 / 0.01 =  99

        A ratio makes price lexicographically dominant. No quality difference
        that can exist — 0.99 against 0.50, or 1.00 against 0.01 — can close a
        1e8 gap, so the bandit was not trading quality against cost at all. It
        was ranking by "is it free", with success_rate as an unreachable
        tiebreaker, and it ran AFTER the complexity-aware ordering that
        deliberately puts Ollama last for deep reasoning.
        """
        return self.success_rate / max(self.avg_cost, 1e-9)

    @property
    def expected_value(self) -> float:
        """Expected net value of one call, in dollars. Bandit ranks by this.

        A DIFFERENCE, not a ratio::

            expected_value = success_rate * ANSWER_VALUE_USD - avg_cost

        Read it as: a correct answer is worth ``ANSWER_VALUE_USD``; a call costs
        ``avg_cost`` whether or not it succeeds. What is left is what routing
        actually gained. That is the quantity the product exists to maximise,
        and unlike a ratio it is bounded, has units, and lets quality win::

            free, 0.50: 0.50*0.05 - 0      = +0.0250
            paid, 0.99: 0.99*0.05 - 0.0200 = +0.0295   <- paid wins on quality
            paid, 0.99: 0.99*0.05 - 0.1000 = -0.0505   <- and loses when overpriced

        A free model is still preferred at equal quality (its cost term is 0),
        which is the behaviour the old formula was reaching for. What changes is
        that "free" is no longer worth 1e8 times "correct".
        """
        quality, _source = self.quality_signal
        return quality * ANSWER_VALUE_USD - self.avg_cost


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
               COALESCE(AVG(latency_ms), 0.0) AS avg_latency,
               -- T-09: the GRADED signal, and how much of it exists. Pulled
               -- alongside `success` so a reader can see the difference between
               -- "models score 0.97" and "0 of 1,598 rows were ever graded".
               COUNT(judge_score) AS n_judged,
               AVG(judge_score) AS judge_mean
          FROM routing_decisions
         -- S4a: only rows whose origin was RECORDED train the bandit.
         --
         -- `provenance` is written by `_write_provenance()` at insert time and
         -- deliberately has NO DEFAULT, because (in cost.py's own words) a
         -- default "asserts the very thing it should be recording". NULL means
         -- "written before this column existed" and is excluded, fail-closed,
         -- exactly as the money surfaces exclude unmeasured provenance.
         --
         -- Measured on the development ledger when this filter was added:
         --
         --     NULL      1387 rows, 1 distinct latency value   (placeholders:
         --                                                      500ms, $0.01)
         --     runtime    214 rows, 214 distinct latencies      (measured)
         --
         -- 87% of what trained the bandit was placeholder data from a period
         -- when latency and cost were not being recorded. The column that
         -- identifies it has existed all along and nothing read it.
         --
         -- Excluding them does NOT remove a model from routing: `reorder()`
         -- explores from `candidates`, not from `eligible`, so a model with no
         -- trusted rows becomes under-sampled and still gets exploration
         -- calls. Measured delta on that ledger: the top pick was unchanged;
         -- the two models dropped were already ranked last on placeholder data.
         WHERE provenance = 'runtime'
           AND profile = ?
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
            judged_samples=int(row[5] or 0),
            judge_mean=(None if row[6] is None else float(row[6])),
        )
        for row in rows
    ]
