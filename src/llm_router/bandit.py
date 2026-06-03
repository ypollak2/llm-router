"""Multi-armed bandit for routing candidate selection.

Plan 07 — Category E (Outcome telemetry & multi-armed bandit learning).

The bandit consumes :class:`llm_router.telemetry.ModelStats` and reorders the
candidate chain that ``router._build_and_filter_chain`` produces. It replaces
the static threshold reordering previously done by
``llm_router.judge.reorder_by_quality`` with a proper exploit/explore split:

* **Exploit (1 - ε):** pick the candidate with the highest
  ``expected_value`` (success / dollar) as the first attempt.
* **Explore (ε):** pick a random candidate from the rest as the first attempt
  so the bandit keeps learning even after one model dominates.

In both cases the *remaining* candidates stay in their original order so the
existing fallback chain (provider failover, health-aware skipping, etc.) is
preserved verbatim. The bandit only touches *which model goes first*.

Cold-start safety: when no candidate has at least
:data:`~llm_router.telemetry.MIN_SAMPLES_FOR_SIGNAL` samples, the bandit
returns the input order unchanged. This means new policies, new models, or
fresh installs route exactly as today.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass

from llm_router.telemetry import MIN_SAMPLES_FOR_SIGNAL, ModelStats, aggregate_stats

__all__ = [
    "EpsilonGreedyBandit",
    "DEFAULT_EPSILON",
]


log = logging.getLogger("llm_router.bandit")

# 90/10 exploit/explore — a sensible default for online learning at low QPS.
# Higher ε wastes more calls on under-performing models; lower ε can lock in
# the locally-best model and never discover a better one when prices/quality
# change. 0.10 is the value the Plan 07 design doc settled on.
DEFAULT_EPSILON = 0.10


@dataclass(frozen=True)
class EpsilonGreedyBandit:
    """Stateless epsilon-greedy reorderer over candidate model lists.

    The bandit is intentionally stateless: every call hits :func:`aggregate_stats`
    fresh. The DB *is* the state. This keeps replay safety trivial (no in-memory
    learners to checkpoint, no warm-up after process restart) and makes the
    bandit safe to use across async workers without locking.

    Args:
        epsilon: Exploration probability in ``[0, 1]``. Default 0.10.
        rng: Optional ``random.Random`` for deterministic tests. Defaults to
            the module-level ``random`` so production behavior matches the
            global seed (which is what callers expect).
    """

    epsilon: float = DEFAULT_EPSILON
    rng: random.Random | None = None

    def _random(self) -> random.Random:
        return self.rng or random

    async def reorder(
        self,
        candidates: list[str],
        *,
        profile: str,
        subject: str,
        window_days: int = 30,
    ) -> list[str]:
        """Reorder candidates by empirical performance for (profile, subject).

        Args:
            candidates: Static fallback chain from the routing profile.
            profile: Active routing profile name. Strings (not enums) so the
                bandit can be invoked from anywhere in the router without an
                enum import.
            subject: Active subject name. Empty/``None`` is normalized to
                ``"general"`` inside :func:`aggregate_stats`.
            window_days: How far back to aggregate stats. Defaults to 30 days
                which matches the existing model-failure-rate window.

        Returns:
            A reordered copy of ``candidates``. Returns the input unchanged
            when no candidate has enough samples to trust.
        """
        if len(candidates) < 2:
            return list(candidates)

        stats = await aggregate_stats(
            profile=profile,
            subject=subject,
            candidates=candidates,
            window_days=window_days,
        )
        by_model: dict[str, ModelStats] = {s.model: s for s in stats}
        eligible = [
            s for s in stats if s.n_samples >= MIN_SAMPLES_FOR_SIGNAL
        ]

        if not eligible:
            # Cold start: no candidate has enough data to trust. Keep the
            # static policy order; the static order already encodes
            # human-chosen "free-first → cheapest → premium" preferences.
            return list(candidates)

        rng = self._random()
        if rng.random() < self.epsilon:
            # Explore: pick a candidate other than the current empirical best
            # to surface new evidence. We pick from ``candidates`` (not just
            # ``eligible``) so under-sampled models also get exploration calls.
            best_model = max(eligible, key=lambda s: s.expected_value).model
            explore_pool = [m for m in candidates if m != best_model]
            if not explore_pool:
                return list(candidates)
            chosen = rng.choice(explore_pool)
            reason = "explore"
        else:
            # Exploit: best empirical EV first.
            chosen = max(eligible, key=lambda s: s.expected_value).model
            reason = "exploit"

        if chosen == candidates[0]:
            # Already at the front — no swap needed.
            return list(candidates)

        chosen_stats = by_model.get(chosen)
        log.info(
            "bandit %s: %s → front (profile=%s subject=%s n=%s sr=%.2f ev=%.2f)",
            reason,
            chosen,
            profile,
            subject,
            getattr(chosen_stats, "n_samples", 0),
            getattr(chosen_stats, "success_rate", 0.0),
            getattr(chosen_stats, "expected_value", 0.0),
        )

        return [chosen] + [m for m in candidates if m != chosen]
