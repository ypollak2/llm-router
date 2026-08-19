"""Post-hoc misroute audit — offline re-scoring of already-recorded decisions.

WHY THIS IS NOT ``audit_routing.py``
====================================

``llm_router.audit_routing`` already exists and is a different feature: a *live*,
per-turn compliance audit-trail writer that appends to the enterprise
``AuditLog`` on every routed turn. This module is a *post-hoc, offline sampler*
that re-scores decisions already written to ``routing_decisions`` and asks
whether each one looks like it went to the wrong model.

The two shared a filename in the downstream package, which is how the gap that
produced this port was found: a file-level copy between the repositories would
have overwritten one feature with the other — same path, no merge conflict, no
import error, no failing test — and the only signal would have been a test
somewhere failing to import a symbol that used to exist. Named apart here so
that cannot happen in either direction.

WHAT IT DOES
============

1. Read ``routing_decisions`` rows not yet audited (``audit_verdict IS NULL``).
2. Re-score each from fields captured at decision time.
3. Write the verdict back to ``audit_verdict`` / ``audit_checked_at``.

**Not text re-classification.** ``routing_decisions`` stores ``prompt_hash``
and never the prompt itself, deliberately. So this is a heuristic over
structured signals already on the row, in confidence order:

* ``judge_score`` — the LLM-as-Judge quality score, primary signal. Below
  ``_MISROUTE_JUDGE_THRESHOLD`` reads as a misroute; at or above
  ``_CORRECT_JUDGE_THRESHOLD`` reads as correct. The band between them is
  genuinely ambiguous and falls through rather than being forced into a
  verdict.
* ``complexity_downgraded`` — the classification itself was pressured downward
  by budget pressure, with no corroborating high judge score. Secondary.
* ``was_downshifted`` — only the model choice was pressured, not the
  classification. Weaker still, and deliberately scored ``insufficient_data``
  rather than ``likely_misroute``: choosing a cheaper model is what this system
  is *for*, so on its own it is not evidence of a bad outcome. Counting it as
  one would make the misroute rate rise every time the router did its job.

IDEMPOTENT AND FAIL-OPEN
========================

Idempotent: the sampler selects on ``audit_verdict IS NULL`` and the writeback
carries the same condition in its ``WHERE``, so re-running never overwrites an
existing verdict, including under a concurrent run.

Fail-open throughout — every path returns rather than raises. This is a
side-channel over historical rows; it must never be able to affect, delay, or
break live routing. ``LLM_ROUTER_AUDIT_DISABLED`` turns it off entirely, and it is
inert until explicitly invoked.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from llm_router.cost import _get_db
from llm_router.logging import get_logger

log = get_logger("llm_router.misroute_audit")

_AUDIT_DISABLED_ENV = "LLM_ROUTER_AUDIT_DISABLED"
_AFFIRMATIVE = {"1", "on", "true", "yes"}

AuditVerdict = Literal["likely_misroute", "likely_correct", "insufficient_data"]

#: judge_score is 0.0-1.0. The gap between these two is the ambiguous band.
_MISROUTE_JUDGE_THRESHOLD = 0.5
_CORRECT_JUDGE_THRESHOLD = 0.75


def audit_disabled() -> bool:
    """True when ``LLM_ROUTER_AUDIT_DISABLED`` is set to an affirmative value."""
    return (os.environ.get(_AUDIT_DISABLED_ENV) or "").strip().lower() in _AFFIRMATIVE


@dataclass(frozen=True)
class AuditedDecision:
    """The verdict for one ``routing_decisions`` row, with its justification."""

    decision_id: int
    verdict: AuditVerdict
    reason: str


def score_decision(row: dict[str, Any]) -> AuditedDecision:
    """Re-score one row from decision-time fields only.

    Pure function, no I/O — every branch is unit-testable without a database,
    which is the point of keeping the scoring separate from the sampling.
    """
    decision_id = int(row["id"])
    judge_score = row.get("judge_score")

    if judge_score is not None:
        judge_score = float(judge_score)
        if judge_score < _MISROUTE_JUDGE_THRESHOLD:
            return AuditedDecision(
                decision_id,
                "likely_misroute",
                f"judge_score={judge_score:.2f} below threshold {_MISROUTE_JUDGE_THRESHOLD}",
            )
        if judge_score >= _CORRECT_JUDGE_THRESHOLD:
            return AuditedDecision(
                decision_id,
                "likely_correct",
                f"judge_score={judge_score:.2f} at/above threshold {_CORRECT_JUDGE_THRESHOLD}",
            )
        # Ambiguous middle band — fall through to the secondary signal rather
        # than forcing a verdict the evidence does not support.

    if row.get("complexity_downgraded"):
        return AuditedDecision(
            decision_id,
            "likely_misroute",
            "complexity_downgraded under budget pressure without a corroborating high judge_score",
        )
    if row.get("was_downshifted"):
        return AuditedDecision(
            decision_id,
            "insufficient_data",
            "was_downshifted to a cheaper model but no judge_score was recorded",
        )
    return AuditedDecision(
        decision_id,
        "insufficient_data",
        "no judge_score, complexity_downgraded, or was_downshifted signal recorded",
    )


async def sample_unaudited_decisions(limit: int = 100) -> list[dict[str, Any]]:
    """Up to ``limit`` rows with no verdict yet. Returns [] rather than raising."""
    if audit_disabled():
        return []
    try:
        db = await _get_db()
        try:
            cursor = await db.execute(
                "SELECT id, judge_score, complexity_downgraded, was_downshifted "
                "FROM routing_decisions WHERE audit_verdict IS NULL "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            rows = await cursor.fetchall()
            columns = [d[0] for d in cursor.description]
            return [dict(zip(columns, r, strict=True)) for r in rows]
        finally:
            await db.close()
    except Exception as exc:  # noqa: BLE001 - fail-open, see module docstring
        log.warning("audit_sample_failed error=%s", exc)
        return []


async def _write_verdict(decision: AuditedDecision) -> bool:
    """Write one verdict back, only where ``audit_verdict`` is still NULL.

    The NULL condition in the WHERE is what makes re-running safe: an earlier
    or concurrent run's verdict is never overwritten, and ``rowcount`` tells
    the caller whether this run was the one that recorded it.
    """
    try:
        db = await _get_db()
        try:
            checked_at = datetime.now(timezone.utc).isoformat()
            cursor = await db.execute(
                "UPDATE routing_decisions SET audit_verdict = ?, audit_checked_at = ? "
                "WHERE id = ? AND audit_verdict IS NULL",
                (decision.verdict, checked_at, decision.decision_id),
            )
            await db.commit()
            return cursor.rowcount > 0
        finally:
            await db.close()
    except Exception as exc:  # noqa: BLE001 - fail-open, see module docstring
        log.warning("audit_write_failed error=%s", exc)
        return False


async def run_audit(limit: int = 100) -> dict[str, Any]:
    """Sample, score, and write back. Returns a report; never raises.

    The report also carries ``routing_quality.summarize()``'s
    ``mis_route_rate_inferred`` as population-level context. There is no
    per-row join key between ``routing_decisions`` (SQL) and the routing-quality
    ledger (JSONL), so the two integrate at the aggregate level rather than by
    inventing a key that would silently mismatch.
    """
    if audit_disabled():
        return {"disabled": True, "sampled": 0, "audited": 0, "verdict_counts": {}}

    rows = await sample_unaudited_decisions(limit=limit)
    verdict_counts: dict[str, int] = {
        "likely_misroute": 0,
        "likely_correct": 0,
        "insufficient_data": 0,
    }
    audited = 0
    for row in rows:
        decision = score_decision(row)
        verdict_counts[decision.verdict] += 1
        if await _write_verdict(decision):
            audited += 1

    report: dict[str, Any] = {
        "disabled": False,
        "sampled": len(rows),
        "audited": audited,
        "verdict_counts": verdict_counts,
    }

    try:
        from llm_router.routing_quality import summarize as _summarize_quality

        report["mis_route_rate_inferred_baseline"] = _summarize_quality().get(
            "mis_route_rate_inferred"
        )
    except Exception as exc:  # noqa: BLE001 - fail-open, context-only field
        log.warning("audit_quality_baseline_failed error=%s", exc)
        report["mis_route_rate_inferred_baseline"] = None

    return report


__all__ = [
    "AuditVerdict",
    "AuditedDecision",
    "audit_disabled",
    "score_decision",
    "sample_unaudited_decisions",
    "run_audit",
]
