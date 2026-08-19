"""Gate 18 (#30) — no unknown realization counted as verified realized.

A route's potential saving (baseline_equivalent − actual) is only REALIZED when
its result was verifiably used by the host (realization_status == verified_used).
Routes whose realization is `verified_overridden` or `unknown` — or that never
emit a realization event — contribute to `potential_savings_usd` but must NEVER
count toward `realized_savings_usd`.
"""
from __future__ import annotations

import uuid

import pytest

from llm_router.execution_ledger import (
    LedgerEvent,
    get_route_accounting,
    get_session_accounting,
    record_event,
)


@pytest.fixture()
def ledger_db(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_EXECUTION_LEDGER_DB", str(tmp_path / "usage.db"))
    return tmp_path / "usage.db"


def _completed(route, *, cost, baseline, realization=None, session_id="s1"):
    return LedgerEvent(
        session_id=session_id,
        route_id=route,
        attempt_id=str(uuid.uuid4()),
        event_type="attempt_completed",
        measured_cost_usd=cost,
        baseline_equivalent_cost_usd=baseline,
        realization_status=realization,
    )


def test_verified_used_route_saving_is_realized(ledger_db):
    record_event(_completed("r-used", cost=0.001, baseline=0.051, realization="verified_used"))
    acc = get_route_accounting("r-used")
    assert acc.potential_savings_usd == pytest.approx(0.05)
    assert acc.realized_savings_usd == pytest.approx(0.05)
    assert acc.realized_routes == 1


def test_unknown_realization_saving_is_potential_but_not_realized(ledger_db):
    record_event(_completed("r-unk", cost=0.001, baseline=0.051, realization="unknown"))
    acc = get_route_accounting("r-unk")
    assert acc.potential_savings_usd == pytest.approx(0.05), "still potential"
    assert acc.realized_savings_usd == 0.0, "unknown realization is NOT realized"
    assert acc.realization_unknown_routes == 1


def test_overridden_realization_saving_is_not_realized(ledger_db):
    record_event(_completed("r-ovr", cost=0.001, baseline=0.051, realization="verified_overridden"))
    acc = get_route_accounting("r-ovr")
    assert acc.potential_savings_usd == pytest.approx(0.05)
    assert acc.realized_savings_usd == 0.0
    assert acc.overridden_routes == 1


def test_no_realization_event_is_not_realized(ledger_db):
    record_event(_completed("r-none", cost=0.001, baseline=0.051, realization=None))
    acc = get_route_accounting("r-none")
    assert acc.potential_savings_usd == pytest.approx(0.05)
    assert acc.realized_savings_usd == 0.0, "no realization proof → not realized"


def test_session_realized_excludes_unknown_and_overridden(ledger_db):
    """Across a session: potential sums ALL routes; realized sums ONLY the
    verified_used one. This is the Gate-18 invariant end-to-end."""
    record_event(_completed("a", cost=0.001, baseline=0.051, realization="verified_used"))
    record_event(_completed("b", cost=0.002, baseline=0.052, realization="unknown"))
    record_event(_completed("c", cost=0.003, baseline=0.053, realization="verified_overridden"))
    acc = get_session_accounting("s1")
    # potential = (0.05) + (0.05) + (0.05) = 0.15 ; realized = only route a = 0.05
    assert acc.potential_savings_usd == pytest.approx(0.15)
    assert acc.realized_savings_usd == pytest.approx(0.05)
    assert acc.realized_savings_usd <= acc.potential_savings_usd
    assert acc.realized_routes == 1
    assert acc.realization_unknown_routes == 1
    assert acc.overridden_routes == 1
