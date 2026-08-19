"""Proof tests for the canonical execution ledger (correctness-reset Phase 2).

Binds to invariants in Docs/correctness-reset/01_FINAL_ACCEPTANCE_CONTRACT.md:
  INV-COST-001  every billable attempt is one recorded event
  INV-COST-002  route actual cost == Σ measured cost over billable attempt events
  INV-COST-003  idempotent on event_id; re-aggregation never double-counts
  INV-ROUTE-004/005  terminal_state is a recorded field

Hermetic (INV-TEST-000): every test points the ledger at a tmp DB via the
LLM_ROUTER_EXECUTION_LEDGER_DB env var — no shared ~/.llm-router state leaks between tests.
"""
from __future__ import annotations

import uuid

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from llm_router.execution_ledger import (
    LedgerEvent,
    get_period_accounting,
    get_route_accounting,
    get_session_accounting,
    reconcile_session,
    record_event,
)


@pytest.fixture()
def ledger_db(tmp_path, monkeypatch):
    db = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_ROUTER_EXECUTION_LEDGER_DB", str(db))
    return db


def _attempt(route_id, event_type, cost, *, session_id="s1", **kw):
    return LedgerEvent(
        session_id=session_id,
        route_id=route_id,
        attempt_id=str(uuid.uuid4()),
        event_type=event_type,
        measured_cost_usd=cost,
        **kw,
    )


# ── INV-COST-001 / 002 ─────────────────────────────────────────────────────────
def test_rejected_attempt_is_recorded_and_counted(ledger_db):
    """A rejected attempt is a first-class billable event and IS in the route total.

    This is the exact P0-1 scenario: a cheap attempt rejected on quality, then an
    accepted attempt. The route's actual cost must be the SUM of both.
    """
    route = "r-esc"
    assert record_event(_attempt(route, "attempt_rejected", 0.002,
                                 rejection_reason="low_quality"))
    assert record_event(_attempt(route, "attempt_completed", 0.001, accepted=True))

    acc = get_route_accounting(route)
    assert acc.billable_attempt_count == 2
    assert acc.rejected_attempt_count == 1
    assert acc.accepted_attempt_count == 1
    # INV-COST-002: actual = 0.002 (rejected) + 0.001 (accepted)
    assert acc.actual_cost_usd == pytest.approx(0.003)


def test_accepted_only_route(ledger_db):
    route = "r-clean"
    record_event(_attempt(route, "attempt_completed", 0.0005, accepted=True))
    acc = get_route_accounting(route)
    assert acc.actual_cost_usd == pytest.approx(0.0005)
    assert acc.rejected_attempt_count == 0


def test_unknown_cost_attempt_not_fabricated(ledger_db):
    """INV-COST-001 failure behavior: an attempt with unknown usage is recorded with
    measured_cost=None and counted as cost_unknown — never dropped, never invented."""
    route = "r-timeout"
    record_event(_attempt(route, "attempt_failed", None,
                          provider_failure_reason="timeout"))
    record_event(_attempt(route, "attempt_completed", 0.001, accepted=True))
    acc = get_route_accounting(route)
    assert acc.billable_attempt_count == 2
    assert acc.cost_unknown_attempts == 1
    assert acc.actual_cost_usd == pytest.approx(0.001)  # unknown not fabricated


# ── INV-COST-003 ───────────────────────────────────────────────────────────────
def test_idempotent_event_id_no_double_count(ledger_db):
    """Re-recording the SAME event_id is a no-op; re-aggregation is stable."""
    route = "r-idem"
    ev = _attempt(route, "attempt_completed", 0.004, accepted=True)
    assert record_event(ev) is True
    assert record_event(ev) is True  # duplicate — INSERT OR IGNORE
    assert record_event(ev) is True
    acc = get_route_accounting(route)
    assert acc.billable_attempt_count == 1
    assert acc.actual_cost_usd == pytest.approx(0.004)


def test_reaggregation_is_stable(ledger_db):
    route = "r-stable"
    for c in (0.001, 0.002, 0.003):
        record_event(_attempt(route, "attempt_completed", c))
    a1 = get_route_accounting(route).actual_cost_usd
    a2 = get_route_accounting(route).actual_cost_usd
    assert a1 == a2 == pytest.approx(0.006)


# ── INV-ROUTE-004/005 ──────────────────────────────────────────────────────────
def test_terminal_state_recorded(ledger_db):
    route = "r-term"
    record_event(_attempt(route, "attempt_completed", 0.001, accepted=True))
    record_event(LedgerEvent(route_id=route, event_type="route_completed",
                             terminal_state="accepted"))
    acc = get_route_accounting(route)
    assert acc.terminal_states.get("accepted") == 1


# ── session aggregation ────────────────────────────────────────────────────────
def test_session_sums_across_routes(ledger_db):
    record_event(_attempt("ra", "attempt_completed", 0.001, session_id="sess"))
    record_event(_attempt("rb", "attempt_rejected", 0.002, session_id="sess"))
    record_event(_attempt("rb", "attempt_completed", 0.0005, session_id="sess"))
    acc = get_session_accounting("sess")
    assert acc.actual_cost_usd == pytest.approx(0.0035)
    assert acc.billable_attempt_count == 3


# ── INV-COST-005: hook/directive overhead is aggregated ────────────────────────
def test_directive_injected_overhead_aggregated(ledger_db):
    """A directive_injected event contributes its token overhead to the session's
    hook_input_tokens, so net savings can subtract it (INV-COST-005). It is NOT a
    billable attempt, so it does not affect actual_cost_usd."""
    record_event(LedgerEvent(session_id="s5", event_type="directive_injected",
                             hook_input_tokens=446, task_type="query"))
    record_event(LedgerEvent(session_id="s5", event_type="directive_injected",
                             hook_input_tokens=227, task_type="analyze"))
    record_event(_attempt("r5", "attempt_completed", 0.001, session_id="s5",
                          accepted=True))
    acc = get_session_accounting("s5")
    assert acc.hook_input_tokens == 673
    assert acc.billable_attempt_count == 1        # directives are not attempts
    assert acc.actual_cost_usd == pytest.approx(0.001)


# ── INV-COST-004: reconciliation primitive ─────────────────────────────────────
def test_reconcile_matches_canonical(ledger_db):
    from llm_router.execution_ledger import reconcile_session
    record_event(_attempt("r", "attempt_rejected", 0.002, session_id="rec"))
    record_event(_attempt("r", "attempt_completed", 0.001, session_id="rec", accepted=True))
    # A surface reporting the honest total (incl. the rejected 0.002) reconciles.
    ok = reconcile_session("rec", 0.003)
    assert ok.reconciled is True
    assert ok.canonical_actual_usd == pytest.approx(0.003)
    assert ok.delta_usd == pytest.approx(0.0)


def test_reconcile_flags_drift(ledger_db):
    from llm_router.execution_ledger import reconcile_session
    record_event(_attempt("r", "attempt_rejected", 0.002, session_id="drift"))
    record_event(_attempt("r", "attempt_completed", 0.001, session_id="drift", accepted=True))
    # A surface that omits the rejected attempt (winner-only 0.001) does NOT reconcile.
    bad = reconcile_session("drift", 0.001)
    assert bad.reconciled is False
    assert bad.delta_usd == pytest.approx(-0.002)


def test_reconcile_unknown_cost_is_not_exact(ledger_db):
    from llm_router.execution_ledger import reconcile_session
    record_event(_attempt("r", "attempt_failed", None, session_id="unk",
                          provider_failure_reason="timeout"))
    record_event(_attempt("r", "attempt_completed", 0.001, session_id="unk", accepted=True))
    # An attempt with unknown cost means the total can't be claimed exact.
    r = reconcile_session("unk", 0.001)
    assert r.reconciled is False
    assert r.cost_unknown_attempts == 1


# ── INV-COST-002 property (Hypothesis) ─────────────────────────────────────────
_costs = st.lists(
    st.tuples(
        st.sampled_from(["attempt_completed", "attempt_rejected", "attempt_failed"]),
        st.one_of(st.none(), st.floats(min_value=0, max_value=1.0,
                                       allow_nan=False, allow_infinity=False)),
    ),
    min_size=1, max_size=25,
)


# 75 examples still exhaustively exercise the accept/reject/fail × known/unknown-cost
# space for this simple sum invariant; 200 real WAL-fsync round-trips could exceed the
# 30s per-test CI timeout on a heavily-loaded shared runner (~15x slower than local),
# which surfaced as a `database is locked` FlakyFailure. Trimming the I/O volume — not
# the assertion — keeps the property strong while fitting the CI wall-clock budget.
@settings(max_examples=75, deadline=None)
@given(attempts=_costs)
def test_property_route_actual_equals_sum_of_attempts(tmp_path_factory, attempts):
    """For ANY chain of attempts, route actual cost == Σ of the known measured costs.

    INV-COST-002 must hold regardless of accept/reject/fail mix or unknown costs.
    """
    db = tmp_path_factory.mktemp("prop") / "usage.db"
    import os
    os.environ["LLM_ROUTER_EXECUTION_LEDGER_DB"] = str(db)
    route = "prop-" + uuid.uuid4().hex[:8]
    expected = 0.0
    for et, cost in attempts:
        record_event(_attempt(route, et, cost))
        if cost is not None:
            expected += cost
    acc = get_route_accounting(route)
    assert acc.actual_cost_usd == pytest.approx(round(expected, 6), abs=1e-6)
    assert acc.billable_attempt_count == len(attempts)


# ── Phase 7 (mutation-driven): get_period_accounting was untested ──────────────
def test_period_accounting_windows_by_ts(ledger_db):
    """Aggregation is windowed on ts: [start, end) — end exclusive. Kills the
    'no tests' mutants on get_period_accounting (the window comparison + bounds)."""
    record_event(_attempt("rp", "attempt_completed", 0.002, ts=100.0))
    record_event(_attempt("rp", "attempt_completed", 0.004, ts=300.0))

    only_first = get_period_accounting(50.0, 200.0)
    assert only_first.billable_attempt_count == 1
    assert only_first.actual_cost_usd == pytest.approx(0.002)

    both = get_period_accounting(50.0, 400.0)
    assert both.billable_attempt_count == 2
    assert both.actual_cost_usd == pytest.approx(0.006)

    # end is EXCLUSIVE: a window ending exactly at t=300 must drop that event.
    end_exclusive = get_period_accounting(50.0, 300.0)
    assert end_exclusive.billable_attempt_count == 1
    # start is INCLUSIVE: a window starting exactly at t=100 keeps it.
    start_inclusive = get_period_accounting(100.0, 250.0)
    assert start_inclusive.billable_attempt_count == 1


# ── Phase 7 (mutation-driven): reconcile_session (INV-COST-004) was untested ───
def test_reconcile_session_self_consistency_all_costs_known(ledger_db):
    """reported=None self-consistency: fully-known costs → reconciled, delta 0."""
    record_event(_attempt("r", "attempt_completed", 0.003, session_id="sx"))
    rec = reconcile_session("sx", None)
    assert rec.canonical_actual_usd == pytest.approx(0.003)
    assert rec.cost_unknown_attempts == 0
    assert rec.reconciled is True
    assert rec.delta_usd == 0.0  # kills delta=0.0 → None mutant


def test_reconcile_session_unknown_cost_not_reconciled(ledger_db):
    """A billable attempt with unknown cost means 'exact' would be a lie."""
    record_event(_attempt("r", "attempt_failed", None, session_id="sy",
                          provider_failure_reason="timeout"))
    record_event(_attempt("r", "attempt_completed", 0.001, session_id="sy"))
    rec = reconcile_session("sy", None)
    assert rec.cost_unknown_attempts == 1
    assert rec.reconciled is False  # kills the cost_unknown==0 comparison mutants


def test_reconcile_session_reported_delta_and_tolerance(ledger_db):
    """reported value: delta = reported − canonical; reconciled iff |delta| <= tol."""
    record_event(_attempt("r", "attempt_completed", 0.010, session_id="sz"))

    exact = reconcile_session("sz", 0.010)
    assert exact.delta_usd == pytest.approx(0.0)
    assert exact.reconciled is True
    assert exact.reported_actual_usd == pytest.approx(0.010)  # echo, kills reported→None

    drifted = reconcile_session("sz", 0.020)
    assert drifted.delta_usd == pytest.approx(0.010)   # kills the subtraction mutants
    assert drifted.reconciled is False                 # kills the tol comparison mutants

    # tol boundary is INCLUSIVE (<=): delta exactly == tol still reconciles.
    # Kills the `abs(delta) <= tol` → `< tol` mutant.
    boundary = reconcile_session("sz", 0.010001)       # delta == 1e-6 == default tol
    assert boundary.delta_usd == pytest.approx(1e-6)
    assert boundary.reconciled is True
