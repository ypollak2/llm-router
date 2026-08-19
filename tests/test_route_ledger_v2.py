"""CF-1 end-to-end scenarios for the v2 route ledger (plan §13).

These lock the honesty contract the audit demanded:
  * Scenario 1 — a pure completion route records one row, verified/tool fields None
  * Scenario 2 — a technical fallback (timeout) is NOT a mis-route (mis_route=None)
  * Scenario 3 — a quality failure (gate_failed/low_quality) IS a quality escalation
  * Scenario 7 — legacy v1 rows load and never corrupt v2 metrics (in test_routing_quality)
plus unit coverage of derive_fallback_reason (the chain_errors → FallbackReason map).
"""
from __future__ import annotations

import pytest

from llm_router.routing_quality import (
    RouteLedgerRecord,
    derive_fallback_reason,
    load_records,
    record_route,
    summarize,
)


# ── derive_fallback_reason: the §4.2 mapping, per reason ──────────────────────

@pytest.mark.parametrize("reason, want_fb, want_mis", [
    # technical / infra → mis_route UNKNOWN (None)
    ("TimeoutError: deadline", "timeout", None),
    ("provider_unhealthy_skip", "health_skip", None),
    ("RateLimitError: 429", "rate_limit", None),
    ("budget exhausted", "budget_exhausted", None),
    ("premium_capped@0.85", "cost_cap", None),
    ("policy:turn_cost:0.9", "policy_rejection", None),
    ("ConnectionError: boom", "provider_failure", None),
    # quality / verification / capability → mis_route TRUE
    ("gate_failed: STRUCTURE 0 markers", "verification_failure", True),
    ("low_quality:0.41", "quality_failure", True),
    ("capability missing: write_files", "capability_failure", True),
])
def test_classify_single_reason(reason, want_fb, want_mis):
    fb, mis = derive_fallback_reason([("m", reason)])
    assert fb == want_fb and mis is want_mis


def test_empty_chain_errors_is_no_fallback_unknown_misroute():
    # no fallback and (for an unverified completion) mis_route is UNKNOWN, never False
    assert derive_fallback_reason([]) == (None, None)


def test_quality_failure_dominates_technical_in_trail():
    # a trail with a timeout THEN a gate failure → the quality failure wins (mis_route=True)
    fb, mis = derive_fallback_reason([("a", "TimeoutError: x"), ("b", "gate_failed: y")])
    assert fb == "verification_failure" and mis is True


def test_pure_technical_trail_reports_last_reason_unknown_misroute():
    fb, mis = derive_fallback_reason([("a", "RateLimitError"), ("b", "TimeoutError")])
    assert fb == "timeout" and mis is None


# ── Scenario 1: pure completion telemetry ─────────────────────────────────────

def test_scenario1_pure_completion_records_one_unverified_row(tmp_path):
    ledger = tmp_path / "rq.jsonl"
    record_route(RouteLedgerRecord(
        route_kind="completion", task_type="query",
        route_succeeded=True,
        tool_execution_attempted=False, tool_execution_succeeded=None,
        verification_attempted=False, verification_passed=None,
        fallback_occurred=False, fallback_reason=None, mis_route=None,
    ), path=str(ledger))
    rows = load_records(str(ledger))
    assert len(rows) == 1
    r = rows[0]
    assert r["route_kind"] == "completion"
    assert r["tool_execution_attempted"] is False and r["tool_execution_succeeded"] is None
    assert r["verification_attempted"] is False and r["verification_passed"] is None
    assert r["mis_route"] is None
    s = summarize(str(ledger))
    # an unverified completion must NOT inflate any verified-quality metric
    assert s["verification_pass_rate"] is None       # no verified rows at all
    assert s["unknown_quality_completion_rate"] == 1.0


# ── Scenario 2: technical provider fallback ───────────────────────────────────

def test_scenario2_timeout_fallback_is_not_a_misroute(tmp_path):
    ledger = tmp_path / "rq.jsonl"
    fb, mis = derive_fallback_reason([("ollama/x", "TimeoutError: 30s")])
    record_route(RouteLedgerRecord(
        route_kind="completion", task_type="analyze",
        route_succeeded=True, fallback_occurred=True, fallback_reason=fb,
        mis_route=mis, quality_escalation_occurred=(mis is True),
        chosen_model="ollama/x", final_model="openai/gpt-4o",
    ), path=str(ledger))
    s = summarize(str(ledger))
    assert s["technical_fallback_rate"] == 1.0
    assert s["quality_escalation_rate"] == 0.0
    # mis_route is UNKNOWN for a timeout, so the inferred-rate denominator is empty
    assert s["mis_route_rate_inferred"] is None


# ── Scenario 3: quality-driven escalation ─────────────────────────────────────

def test_scenario3_quality_failure_is_escalation_not_technical(tmp_path):
    ledger = tmp_path / "rq.jsonl"
    fb, mis = derive_fallback_reason([("ollama/x", "low_quality:0.40")])
    record_route(RouteLedgerRecord(
        route_kind="completion", task_type="code",
        route_succeeded=True, fallback_occurred=True, fallback_reason=fb,
        mis_route=mis, quality_escalation_occurred=(mis is True),
        chosen_tier=0, final_tier=3,
    ), path=str(ledger))
    s = summarize(str(ledger))
    assert s["quality_escalation_rate"] == 1.0
    assert s["technical_fallback_rate"] == 0.0     # quality failure ≠ technical fallback
    assert s["mis_route_rate_inferred"] == 1.0


# ── Double-count guard: substep rows never enter default metrics ──────────────

def test_delegate_substep_rows_excluded_from_default_summary(tmp_path):
    ledger = tmp_path / "rq.jsonl"
    # one parent delegate row + two substep rows (parent_route_id set)
    record_route(RouteLedgerRecord(route_kind="delegate", verification_attempted=True,
                                   verification_passed=True, saved_usd=0.5), path=str(ledger))
    record_route(RouteLedgerRecord(route_kind="delegate_substep",
                                   parent_route_id="p1", saved_usd=0.3), path=str(ledger))
    record_route(RouteLedgerRecord(route_kind="delegate_substep",
                                   parent_route_id="p1", saved_usd=0.2), path=str(ledger))
    s = summarize(str(ledger))
    # default summarize counts the ONE logical work item, not the internal calls
    assert s["schema_v2_rows"] == 1
    assert s["total_saved_usd"] == 0.5
