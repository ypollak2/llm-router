"""North Star route-quality ledger (schema v2) — recording + honest summarize.

"Route to the cheapest capable model, escalate on failure" must be MEASURED, not
assumed — and measured HONESTLY: verified quality, technical fallback, and quality
escalation are never conflated. record_route() appends fail-open; summarize() reads
back split metrics with explicit denominators. The deprecated v1 record()/RouteRecord
remain for backward compat and are read as legacy rows that never pollute v2 metrics.
"""
from __future__ import annotations

import json

from llm_router.routing_quality import (
    RouteLedgerRecord,
    RouteRecord,
    load_records,
    record,
    record_delegation,
    record_route,
    summarize,
)


# ── v2 recording ──────────────────────────────────────────────────────────────

def test_record_route_appends_v2_row(tmp_path):
    ledger = tmp_path / "rq.jsonl"
    assert record_route(RouteLedgerRecord(route_kind="completion", task_type="query"),
                        path=str(ledger)) is True
    rows = [json.loads(ln) for ln in ledger.read_text().splitlines() if ln.strip()]
    assert len(rows) == 1
    assert rows[0]["schema_version"] == 2
    assert rows[0]["route_kind"] == "completion"
    assert rows[0]["route_id"]           # a uuid was stamped
    assert rows[0]["ts"] > 0             # stamped on write


def test_record_route_is_fail_open_on_bad_path(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    bad = blocker / "nested" / "rq.jsonl"
    assert record_route(RouteLedgerRecord(), path=str(bad)) is False  # swallowed


# ── honest summarize ──────────────────────────────────────────────────────────

def test_summarize_splits_verified_from_unverified(tmp_path):
    ledger = tmp_path / "rq.jsonl"
    # one unverified completion, one verified delegate that passed
    record_route(RouteLedgerRecord(
        route_kind="completion", verification_attempted=False,
        verification_passed=None, saved_usd=0.10), path=str(ledger))
    record_route(RouteLedgerRecord(
        route_kind="delegate", verification_attempted=True,
        verification_passed=True, saved_usd=0.20), path=str(ledger))
    s = summarize(path=str(ledger))
    assert s["schema_v2_rows"] == 2
    # pass rate is over VERIFIED rows only (the completion never enters it)
    assert s["verification_pass_rate"] == 1.0
    assert abs(s["verified_route_rate"] - 0.5) < 1e-9
    assert abs(s["unverified_route_rate"] - 0.5) < 1e-9
    assert abs(s["total_saved_usd"] - 0.30) < 1e-9
    assert s["cost_savings_by_route_kind"]["completion"] == 0.10
    assert s["cost_savings_by_route_kind"]["delegate"] == 0.20
    # no blended completion_rate key must ever exist
    assert "completion_rate" not in s


def test_summarize_missing_ledger_is_empty(tmp_path):
    s = summarize(path=str(tmp_path / "nope.jsonl"))
    assert s["total_rows"] == 0 and s["schema_v2_rows"] == 0


def test_unknown_quality_completion_rate(tmp_path):
    ledger = tmp_path / "rq.jsonl"
    record_route(RouteLedgerRecord(route_kind="completion",
                                   verification_attempted=False), path=str(ledger))
    record_route(RouteLedgerRecord(route_kind="completion",
                                   verification_attempted=True,
                                   verification_passed=True), path=str(ledger))
    s = summarize(path=str(ledger))
    # half the completion routes are unverified
    assert abs(s["unknown_quality_completion_rate"] - 0.5) < 1e-9


# ── delegate path emits a v2 delegate row (aggregate-delegation-only) ─────────

def test_record_delegation_weak_pass_local_only(tmp_path):
    """A delegation completed entirely on tier 0 is a weak pass, not an escalation."""
    ledger = tmp_path / "rq.jsonl"
    result = {"outcome": "complete",
              "milestones": [{"achieved_by": 0}, {"achieved_by": 0}],
              "savings": {"actual_usd": 0.0, "baseline_usd": 0.4, "saved_usd": 0.4}}
    assert record_delegation(result, path=str(ledger)) is True
    rows = load_records(str(ledger))
    assert len(rows) == 1 and rows[0]["schema_version"] == 2
    row = rows[0]
    assert row["route_kind"] == "delegate"
    assert row["weak_pass"] is True
    assert row["quality_escalation_occurred"] is False
    assert row["verification_attempted"] is True and row["verification_passed"] is True
    assert row["mis_route"] is False  # verified + no escalation → correctly routed


def test_record_delegation_escalation_is_quality_not_technical(tmp_path):
    ledger = tmp_path / "rq.jsonl"
    result = {"outcome": "complete",
              "milestones": [{"achieved_by": 0}, {"achieved_by": 1}],  # escalated
              "savings": {"saved_usd": 0.2}}
    record_delegation(result, path=str(ledger))
    s = summarize(path=str(ledger))
    assert s["quality_escalation_rate"] == 1.0
    # an MGEE escalation is quality-driven, NOT a technical fallback
    assert s["technical_fallback_rate"] == 0.0
    assert s["mis_route_rate_inferred"] == 1.0


# ── legacy v1 API still works and never pollutes v2 metrics ───────────────────

def test_legacy_record_writes_v1_row(tmp_path):
    ledger = tmp_path / "rq.jsonl"
    assert record(RouteRecord(task_type="code", chosen_tier=0, needed_escalation=False,
                              completion=True, tool_success=True, saved=0.2),
                 path=str(ledger)) is True
    rows = load_records(str(ledger))
    assert len(rows) == 1
    assert rows[0].get("schema_version") == 1  # normalized: legacy rows lack it


def test_legacy_rows_excluded_from_v2_quality(tmp_path):
    """Scenario 7: v1 + v2 rows coexist; legacy never enters v2 quality denominators."""
    ledger = tmp_path / "rq.jsonl"
    # 3 legacy v1 rows (old overloaded completion/tool_success/mis_route semantics)
    for _ in range(3):
        record(RouteRecord(task_type="code", chosen_tier=0, needed_escalation=True,
                           completion=True, tool_success=True, mis_route=True, saved=0.1),
              path=str(ledger))
    # 2 v2 rows: one verified, one unverified
    record_route(RouteLedgerRecord(route_kind="delegate", verification_attempted=True,
                                   verification_passed=True), path=str(ledger))
    record_route(RouteLedgerRecord(route_kind="completion",
                                   verification_attempted=False), path=str(ledger))
    s = summarize(path=str(ledger))
    assert s["total_rows"] == 5
    assert s["legacy_rows"] == 3
    assert s["schema_v2_rows"] == 2
    # verification pass rate is computed over the 1 verified v2 row ONLY — the 3
    # legacy rows with completion=True do NOT inflate it
    assert s["verification_pass_rate"] == 1.0
    # mis_route inferred only over v2 rows with non-None mis_route (there are none
    # here: the delegate row is unescalated→False, the completion is None) — the
    # 3 legacy mis_route=True rows must NOT appear
    assert s["mis_route_rate_inferred"] in (None, 0.0)


def test_malformed_row_never_crashes_summarize(tmp_path):
    ledger = tmp_path / "rq.jsonl"
    ledger.write_text('{"schema_version": 2, "route_kind": "completion"}\n'
                      "this is not json\n"
                      "\n")
    s = summarize(path=str(ledger))
    assert s["invalid_rows"] == 1 and s["schema_v2_rows"] == 1
