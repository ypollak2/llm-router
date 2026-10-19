"""One eligibility rule (PLAN v16 P2.0 task 6).

``grade(evidence)`` is the single, pure definition of when a model is eligible
for a (task_type, tier) lane against the Sonnet reference:

    eligible  iff  n >= 60  AND  gap_pts <= 3.0  AND  lb_pts >= -10.0

Evidence shape (``Evidence``, frozen): ``n`` items per arm, ``gap_pts`` =
(reference rate - candidate rate) * 100 (positive = candidate is worse), and
``lb_pts`` = Newcombe (method 10, Wilson-based) 95% lower bound of
(candidate - reference), in points. ``evidence_from_counts`` builds it from
k/n counts. A dict with the same keys is accepted too, and so is a dict of raw
counts ``{k_cand, n_cand, k_ref, n_ref}``, which ``grade`` converts with
``evidence_from_counts``. A mapping may name the lower bound ``newcombe_lb``,
the key ``lane_quality.jsonl`` batch rows use; ``lane_quality.batch_passed``
grades shadow-pair rows through this function, so there is one rule.

Malformed evidence never raises and never passes: a count that is not an
integer (bools and fractional values included) or outside ``0 <= k <= n``, or
an ``n`` that is not an integer, grades ``unknown``; a gap or LB that is not a
finite number counts as missing (``probe``).

The LB treats the two arms as independent samples (Newcombe method 10). For
paired outcomes on the same prompts this is conservative (wider) when the arms
are positively correlated.

States:
  unknown   no evidence: None, empty mapping, or n <= 0.
  probe     some evidence but not enough to decide: 0 < n < 60, or n >= 60 with
            gap_pts or lb_pts missing.
  eligible  n >= 60 and gap_pts <= 3.0 and lb_pts >= -10.0 (both inclusive,
            unrounded). From counts the gap is computed exactly (Fraction), so
            an exact 3-point gap is eligible. Both comparisons allow 1e-9 pt,
            so a supplied gap of 3.0000000000000004 (float noise) is still 3.
  failed    n >= 60 and the rule does not hold.

Nothing here reads or writes ``local_eligibility.json``. ``eligibility_routing`` (F106) re-checks a
stored lane with ``grade`` before the router acts on it.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Literal, Mapping

from llm_router.lane_quality import PAIRS_MAX_GAP_PTS, PAIRS_MIN_LB, PAIRS_MIN_N

Grade = Literal["unknown", "probe", "eligible", "failed"]
_Z95 = 1.959964
# The LB comes from sqrt and a supplied gap from float arithmetic, so neither is
# exact; float noise is ~1e-14 pts. Both bounds allow 1e-9 pt. A true value
# within 1e-9 pt beyond a bound is therefore accepted: a deliberate tolerance,
# far below anything a decision should turn on.
_EPS_PTS = 1e-9


def _count(x: Any) -> int | None:
    return x if isinstance(x, int) and not isinstance(x, bool) else None


def _finite(x: Any) -> float | None:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    return float(x) if math.isfinite(x) else None


@dataclass(frozen=True)
class Evidence:
    n: int = 0
    gap_pts: float | None = None
    lb_pts: float | None = None


def _wilson(k: int, n: int, z: float) -> tuple[float, float]:
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def newcombe_diff_ci(k1: int, n1: int, k2: int, n2: int, z: float = _Z95) -> tuple[float, float]:
    """Newcombe method 10 CI for p1 - p2 (proportion units)."""
    if n1 <= 0 or n2 <= 0 or not (0 <= k1 <= n1) or not (0 <= k2 <= n2):
        raise ValueError("need n > 0 and 0 <= k <= n for both arms")
    p1, p2 = k1 / n1, k2 / n2
    l1, u1 = _wilson(k1, n1, z)
    l2, u2 = _wilson(k2, n2, z)
    d = p1 - p2
    return d - math.sqrt((p1 - l1) ** 2 + (u2 - p2) ** 2), d + math.sqrt((u1 - p1) ** 2 + (p2 - l2) ** 2)


def evidence_from_counts(k_cand: int, n_cand: int, k_ref: int, n_ref: int) -> Evidence:
    """Evidence from candidate and reference successes; empty if either arm has n == 0."""
    if n_cand <= 0 or n_ref <= 0:
        return Evidence()
    lo, _ = newcombe_diff_ci(k_cand, n_cand, k_ref, n_ref)
    return Evidence(n=min(n_cand, n_ref),
                    # Exact rational gap; float() of it is correctly rounded and
                    # monotone, so an exact gap <= 3 stays <= 3.0 (floats gave 3.0000000000000004).
                    gap_pts=float((Fraction(k_ref, n_ref) - Fraction(k_cand, n_cand)) * 100),
                    lb_pts=lo * 100)


def grade(evidence: Evidence | Mapping[str, Any] | None) -> Grade:
    """Grade evidence as unknown, probe, eligible or failed (see module docstring)."""
    if not evidence:
        return "unknown"
    if isinstance(evidence, Mapping) and "n_cand" in evidence:
        counts = [_count(evidence.get(k)) for k in ("k_cand", "n_cand", "k_ref", "n_ref")]
        if any(c is None for c in counts):
            return "unknown"  # absent or non-integer counts are unmeasured, not zero
        k_cand, n_cand, k_ref, n_ref = counts
        if n_cand > 0 and n_ref > 0 and not (0 <= k_cand <= n_cand and 0 <= k_ref <= n_ref):
            return "unknown"  # impossible counts are corrupt evidence
        evidence = evidence_from_counts(k_cand, n_cand, k_ref, n_ref)
    get = evidence.get if isinstance(evidence, Mapping) else lambda k: getattr(evidence, k, None)
    n = _count(get("n"))
    if n is None or n <= 0:
        return "unknown"
    gap = _finite(get("gap_pts"))
    lb = _finite(get("lb_pts"))
    if lb is None and isinstance(evidence, Mapping):
        lb = _finite(evidence.get("newcombe_lb"))
    if n < PAIRS_MIN_N or gap is None or lb is None:
        return "probe"
    ok = gap <= PAIRS_MAX_GAP_PTS + _EPS_PTS and lb >= PAIRS_MIN_LB - _EPS_PTS
    return "eligible" if ok else "failed"
