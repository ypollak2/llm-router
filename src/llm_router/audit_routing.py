# Ported from Chuzom's audit_routing.py; env vars renamed to LLM_ROUTER_*; data source rewired to llm-router's layer.
"""Post-hoc misroute audit (WS6).

Chuzom's ``audit_routing.py`` is a *live*, per-turn compliance audit-trail
writer: it appends one row to an enterprise ``AuditLog`` (SQLite-backed,
tamper-evident) on every successful routed turn, gated by ``is_enterprise()``
and ``CHUZOM_AUDIT_DISABLED``. That capability — the enterprise ``AuditLog``
itself — is out of scope here: ``enterprise/`` is explicitly REJECTED for
llm-router (see the migration plan's capability table; llm-router's README
sells Chuzom's enterprise tier separately).

What *is* ported is the structural pattern, not the payload:

* the module name and env-disable convention (Chuzom's
  ``CHUZOM_AUDIT_DISABLED`` -> ``LLM_ROUTER_AUDIT_DISABLED``, same
  affirmative-value set),
* fail-open, best-effort semantics (an audit failure must never raise into
  a caller or interrupt anything else),
* the "audit is a side channel, not the main path" discipline.

The actual capability adapts the plan's C5 line: "Misroute detection /
audit ... feeds existing routing_decisions.was_good/reason_code, no
parallel accuracy store." Concretely, this module is a *post-hoc, offline*
sampler over already-recorded decisions:

1. Read a batch of ``routing_decisions`` rows that have not yet been
   audited (``audit_verdict IS NULL``).
2. Re-score each row using fields captured at decision time —
   ``judge_score`` (LLM-as-Judge quality signal, primary), and
   ``complexity_downgraded`` / ``was_downshifted`` (budget-pressure
   signals, secondary) — because ``routing_decisions`` deliberately never
   stores raw prompt text (only ``prompt_hash``), so true text
   re-classification against the live classifier is not possible for
   historical rows. See ``score_decision`` for the exact heuristic.
3. Write the verdict to two new additive columns, ``audit_verdict`` and
   ``audit_checked_at`` (see ``cost.MIGRATE_ROUTING_DECISIONS_ADD_AUDIT``),
   rather than the community-shared ``was_good`` column or the
   decision-time ``reason_code`` column. Those two columns already carry
   live, narrower meaning elsewhere: ``was_good`` is genuine human
   thumbs-up/down feedback consumed by ``community.py``'s acceptance-rate
   metric (which filters ``WHERE was_good IS NOT NULL`` assuming pure
   human provenance), and ``reason_code`` is the classifier's own
   decision-time reasoning text. Auto-populating either with a
   machine-derived audit guess would silently corrupt an existing signal.
   Recording the verdict as new additive columns on the SAME table still
   satisfies the plan's "no parallel accuracy store" constraint (same
   table, not a second store) while protecting the two existing columns'
   provenance guarantees. This is a deliberate, reasoned deviation from
   the plan's literal column names — see the migration constant's
   docstring in cost.py for the same rationale.
4. Never overwrite an existing verdict: the write-back UPDATE is guarded
   by ``WHERE audit_verdict IS NULL``, so re-running the audit on an
   already-audited row is a no-op. Repeated runs cannot flip-flop or
   double-count a decision's verdict.

This is fully offline / post-hoc: there is no live routing-path hook to
add here (unlike WS4's shadow-mode capability-routing hook in
``router.py``), so there is no live-routing invariance test required for
this workstream — the module is inert until explicitly invoked, and never
touches the request/response path.

Integration with prior workstreams:

* WS2 (``routing_quality.py``): ``run_audit``'s report includes
  ``routing_quality.summarize()["mis_route_rate_inferred"]`` as
  population-level context alongside this module's own sample-level
  verdict counts. There is no per-row join key between the SQL
  ``routing_decisions`` table and the JSONL routing-quality ledger, so
  this integrates at the aggregate level rather than inventing one.
* WS1 (``execution_ledger.py``): WS6 operates entirely on the
  decision-time table (``routing_decisions``, owned by ``cost.py``), never
  on the outcome-time ``execution_events`` table or its accounting
  functions. There is therefore no duplication risk with WS1 to satisfy —
  WS6 simply does not re-implement anything execution_ledger.py already
  owns.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from llm_router.cost import _get_db

log = logging.getLogger("llm_router.audit_routing")

# Ported 1:1 from Chuzom's CHUZOM_AUDIT_DISABLED env-disable convention.
_AUDIT_DISABLED_ENV = "LLM_ROUTER_AUDIT_DISABLED"
_AFFIRMATIVE = {"1", "on", "true", "yes"}

AuditVerdict = Literal["likely_misroute", "likely_correct", "insufficient_data"]

# Judge-score thresholds for the primary (highest-confidence) signal.
# judge_score is a 0.0-1.0 LLM-as-Judge quality score recorded on the row.
_MISROUTE_JUDGE_THRESHOLD = 0.5
_CORRECT_JUDGE_THRESHOLD = 0.75


def audit_disabled() -> bool:
    """True when LLM_ROUTER_AUDIT_DISABLED is set to an affirmative value."""
    return (os.environ.get(_AUDIT_DISABLED_ENV) or "").strip().lower() in _AFFIRMATIVE


@dataclass(frozen=True)
class AuditedDecision:
    """Result of re-scoring one routing_decisions row."""

    decision_id: int
    verdict: AuditVerdict
    reason: str


def score_decision(row: dict[str, Any]) -> AuditedDecision:
    """Re-score a single routing_decisions row from decision-time fields only.

    Not text re-classification — ``routing_decisions`` stores only
    ``prompt_hash``, never raw prompt text, so this is a structured-field
    heuristic over signals already captured when the decision was made:

    * ``judge_score`` (primary, highest confidence, when present): below
      ``_MISROUTE_JUDGE_THRESHOLD`` -> likely_misroute; at/above
      ``_CORRECT_JUDGE_THRESHOLD`` -> likely_correct; the ambiguous middle
      band falls through to the secondary signal.
    * ``complexity_downgraded`` (secondary): the complexity classification
      itself was pressured downward by budget pressure with no
      corroborating high judge_score -> likely_misroute.
    * ``was_downshifted`` (secondary, weaker): only the model choice (not
      the classification) was pressured -> insufficient_data, since this
      alone is not strong enough evidence of a bad outcome.
    * Otherwise: insufficient_data (no informative signal recorded).

    Pure function, no I/O — exhaustively unit-testable without a database.
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
        # Ambiguous middle band — fall through to the secondary signal.

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
    """Fetch up to `limit` routing_decisions rows that have not yet been audited.

    Fail-open: returns an empty list (never raises) on any DB error or when
    auditing is disabled, consistent with WS1/WS2/WS5 precedent — an audit
    failure must never surface to callers that expect this to be a harmless
    offline/background task.
    """
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
    """Write one verdict back, only if audit_verdict is currently NULL.

    Non-destructive by construction: the WHERE clause guards against
    overwriting a verdict written by an earlier (or concurrent) audit run,
    which is what makes re-running the audit idempotent.
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
    """Sample unaudited routing_decisions, score them, and write verdicts back.

    Returns a report dict with per-verdict counts plus, for WS2 integration,
    ``routing_quality.summarize()["mis_route_rate_inferred"]`` as a
    population-level baseline (there is no per-row join key between
    routing_decisions and the routing_quality JSONL ledger, so this
    integrates at the aggregate level).

    Fail-open throughout: never raises.
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
