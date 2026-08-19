"""G-F class C6 — `_aggregate` accumulates ACROSS rows, not just over the last one.

WHY THIS FILE EXISTS
--------------------
The G-F baseline left 51 mutants alive in `execution_ledger._aggregate`, nine of them
turning an accumulator into a plain assignment or a subtraction:

    acc.classifier_cost_usd_total += float(ctok)   ->   = float(ctok)
    acc.cost_unknown_attempts     += 1             ->   = 1
    acc.hook_output_tokens        += int(...)      ->   -= int(...)
    acc.realized_routes           += 1             ->   = 1

Every existing test in `test_phase0_aggregate.py` records exactly ONE attempt row per
route and ONE route per assertion. With a single row, `total = 0; total += x` and
`total = x` produce the same answer, so the whole class is invisible — not because the
tests are careless, but because one row cannot distinguish a sum from an assignment.

These are money fields. `= x` silently reports only the LAST attempt's cost on a route
that fell back twice, and `-= x` inverts the sign. Both would show up as a plausible
number on a dashboard rather than as an error.

WHAT MAKES THIS A BEHAVIOURAL TEST
----------------------------------
The expected values are arithmetic on constants chosen HERE (0.001 + 0.002 + 0.004 =
0.007), not a re-computation of `_aggregate`'s own logic. The test would still be correct
if the implementation were rewritten from scratch.

The three values per field are deliberately distinct and non-uniform (1, 2, 4 rather than
1, 1, 1) so that a sum, a last-write, a first-write and a difference are four different
numbers. With equal values, `= x` and `+= x` on the final row can coincide.
"""

from __future__ import annotations

import uuid

import pytest

from llm_router.execution_ledger import (
    LedgerEvent,
    get_route_accounting,
    get_turn_accounting,
    record_event,
)


def _attempt(
    route_id: str,
    *,
    turn_id: str = "",
    event_type: str = "attempt_completed",
    measured: float | None = 0.0,
    baseline: float | None = 0.0,
    classifier: float | None = None,
    failed: float | None = None,
    hook_in: int | None = None,
    hook_out: int | None = None,
    host_mode: str = "unknown",
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    provider: str = "",
) -> LedgerEvent:
    return LedgerEvent(
        session_id="s-c6",
        turn_id=turn_id,
        route_id=route_id,
        attempt_id=str(uuid.uuid4()),
        event_type=event_type,
        measured_cost_usd=measured,
        baseline_equivalent_cost_usd=baseline,
        classifier_cost_usd=classifier,
        failed_attempt_cost_usd=failed,
        hook_input_tokens=hook_in,
        hook_output_tokens=hook_out,
        host_mode=host_mode,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        provider=provider,
    )


def _realized(route_id: str, *, turn_id: str = "", status: str = "verified_used",
              adoption_method: str | None = "door_call") -> LedgerEvent:
    return LedgerEvent(
        session_id="s-c6",
        turn_id=turn_id,
        route_id=route_id,
        event_type="route_realized",
        realization_status=status,
        adoption_method=adoption_method,
        used_by_host=True,
        accepted=True,
    )


class TestAccumulatesAcrossAttempts:
    """One route, three billable attempts — a chain that fell back twice."""

    @pytest.fixture()
    def acc(self, tmp_path, monkeypatch):
        db_path = tmp_path / "usage.db"
        monkeypatch.setenv("LLM_ROUTER_EXECUTION_LEDGER_DB", str(db_path))
        rid = "r-c6-multi"
        for measured, baseline, classifier, failed, hook_out in (
            (0.010, 0.100, 0.001, 0.010, 100),
            (0.020, 0.200, 0.002, 0.020, 200),
            (0.040, 0.400, 0.004, 0.040, 400),
        ):
            record_event(
                _attempt(rid, measured=measured, baseline=baseline, classifier=classifier,
                         failed=failed, hook_in=hook_out, hook_out=hook_out,
                         host_mode="metered"),
                path=db_path,
            )
        return get_route_accounting(rid, path=db_path)

    def test_attempt_counts_are_summed(self, acc):
        assert acc.attempt_count == 3
        assert acc.billable_attempt_count == 3
        assert acc.accepted_attempt_count == 3

    def test_actual_cost_is_the_sum_not_the_last_attempt(self, acc):
        # 0.010 + 0.020 + 0.040. Last-write would give 0.040; first-write 0.010.
        assert acc.actual_cost_usd == pytest.approx(0.070)

    def test_baseline_equivalent_is_the_sum(self, acc):
        assert acc.baseline_equivalent_cost_usd == pytest.approx(0.700)

    def test_classifier_cost_total_is_the_sum(self, acc):
        # Kills `classifier_cost_usd_total = float(ctok)`: that reports 0.004.
        assert acc.classifier_cost_usd_total == pytest.approx(0.007)

    def test_failed_attempt_cost_total_is_the_sum(self, acc):
        # Kills `failed_attempt_cost_usd_total = float(ftok)`: that reports 0.040.
        assert acc.failed_attempt_cost_usd_total == pytest.approx(0.070)

    def test_hook_tokens_are_summed_and_positive(self, acc):
        # Kills both `hook_output_tokens = int(...)` (700 vs 400) and
        # `-= int(...)` (700 vs -700). The sign assertion is the point of the second:
        # a negative token count is not a small error, it is a nonsense value.
        assert acc.hook_output_tokens == 700
        assert acc.hook_input_tokens == 700
        assert acc.hook_output_tokens > 0


class TestUnknownCostAttemptsAreCounted:
    """`cost_unknown_attempts += 1` -> `= 1` is invisible unless two rows lack a cost."""

    def test_two_unpriced_attempts_count_as_two(self, tmp_path, monkeypatch):
        db_path = tmp_path / "usage.db"
        monkeypatch.setenv("LLM_ROUTER_EXECUTION_LEDGER_DB", str(db_path))
        rid = "r-c6-unknown"
        for _ in range(2):
            record_event(_attempt(rid, measured=None, baseline=0.05), path=db_path)
        record_event(_attempt(rid, measured=0.01, baseline=0.05), path=db_path)

        acc = get_route_accounting(rid, path=db_path)
        assert acc.cost_unknown_attempts == 2      # `= 1` would report 1
        assert acc.attempt_count == 3
        assert acc.actual_cost_usd == pytest.approx(0.01)  # only the priced row counts


class TestRouteCountersCountRoutesNotTheLastOne:
    """Three routes under one turn, one per realization status.

    This is the only place the per-route counters can be distinguished: with a single
    route, `realized_routes += 1` and `= 1` both yield 1.

    It also covers `get_turn_accounting`, which the G-F baseline found had NO test
    executing it at all (10 mutants, all no-coverage) despite being the per-turn money
    accounting entry point.
    """

    @pytest.fixture()
    def acc(self, tmp_path, monkeypatch):
        db_path = tmp_path / "usage.db"
        monkeypatch.setenv("LLM_ROUTER_EXECUTION_LEDGER_DB", str(db_path))
        turn = "t-c6"
        # TWO routes per status. The first version of this fixture used two realized
        # routes but only ONE overridden and ONE unknown, and the verification run
        # caught it: `overridden_routes = 1` and `+= 1` are the same number when there
        # is a single route, so those two mutants survived a test written to kill them.
        # The same one-sample blindness this file exists to fix, reintroduced one level
        # up.
        for rid, status in (("r-a", "verified_used"), ("r-b", "verified_used"),
                            ("r-c", "verified_overridden"), ("r-d", "verified_overridden"),
                            ("r-e", "unknown"), ("r-f", "unknown")):
            record_event(_attempt(rid, turn_id=turn, measured=0.01, baseline=0.05),
                         path=db_path)
            record_event(_realized(rid, turn_id=turn, status=status), path=db_path)
        return get_turn_accounting(turn, path=db_path)

    def test_turn_accounting_aggregates_every_route_in_the_turn(self, acc):
        assert acc.scope == "turn"
        assert acc.attempt_count == 6
        assert acc.actual_cost_usd == pytest.approx(0.06)

    def test_realized_routes_counts_both_realized_routes(self, acc):
        assert acc.realized_routes == 2            # `= 1` would report 1

    def test_overridden_and_unknown_routes_are_counted_separately(self, acc):
        assert acc.overridden_routes == 2
        assert acc.realization_unknown_routes == 2

    def test_the_three_statuses_do_not_share_a_counter(self, acc):
        # An implementation that funnelled every status into one bucket would satisfy
        # each assertion above in isolation but not their sum against the route count.
        total = (acc.realized_routes + acc.overridden_routes
                 + acc.realization_unknown_routes)
        assert total == 6


class TestQuotaTokensSavedSumAcrossRealizedRoutes:
    """`realized_quota_tokens_saved += quota` needs TWO quota-bearing realized routes.

    Quota is only credited when a route is realized with an adopting method AND its
    final provider is non-Claude — so a single route makes `+= quota` and `= quota`
    indistinguishable, and a Claude-final route contributes 0 and hides the difference
    entirely.
    """

    def test_quota_is_summed_over_two_non_claude_realized_routes(self, tmp_path, monkeypatch):
        db_path = tmp_path / "usage.db"
        monkeypatch.setenv("LLM_ROUTER_EXECUTION_LEDGER_DB", str(db_path))
        turn = "t-c6-quota"
        for rid, itok, otok in (("q-a", 1_000, 500), ("q-b", 2_000, 1_000)):
            record_event(
                _attempt(rid, turn_id=turn, measured=0.01, baseline=0.05,
                         input_tokens=itok, output_tokens=otok, provider="ollama"),
                path=db_path,
            )
            record_event(_realized(rid, turn_id=turn), path=db_path)

        acc = get_turn_accounting(turn, path=db_path)
        # 1500 + 3000. Last-write would report 3000; a single route could not tell them
        # apart at all.
        assert acc.realized_quota_tokens_saved == 4_500
        assert acc.realized_routes == 2
