"""T2-L2: forecast / predictive budget tier.

Refuses a reservation BEFORE the hard cap is hit, based on the rolling
burn rate observed via committed spend events. Goal: stop a runaway
workflow before it slams into the cap mid-turn.

Acceptance (from this session's plan): with a $1 cap, $0.50 already
consumed, a burn rate of $0.20/min, and a new $0.05 reservation
requested when ~2 minutes remain to the cap, the call should raise
``ForecastedBudgetBreach`` (under the strict forecast mode).

Three-mode gate (env ``LLM_ROUTER_BUDGET_FORECAST_MODE``):

* ``off`` (default — opt-in)
* ``warn`` — log forecasted breaches but proceed
* ``strict`` — raise ``ForecastedBudgetBreach``

Spend events are persisted via a new ``budget_spend_events`` table
populated by ``SqliteBudgetBackend.commit``; the burn-rate query
aggregates committed amounts in a rolling window.

See: Docs/audit/post-remediation/GAP_ANALYSIS.md G-002 (forecast slice).
"""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest

from llm_router.budget_backend import (
    ForecastedBudgetBreach,
    SqliteBudgetBackend,
    reset_budget_backend_for_tests,
)
from llm_router.budget_key import SCOPE_TURN, BudgetKey


@pytest.fixture(autouse=True)
def _reset_singleton() -> None:
    reset_budget_backend_for_tests()
    yield
    reset_budget_backend_for_tests()


@pytest.fixture
def sqlite_backend(tmp_path: Path) -> SqliteBudgetBackend:
    return SqliteBudgetBackend(db_path=tmp_path / "budgets.db")


def _k(user: str = "alice") -> BudgetKey:
    return BudgetKey(
        tenant_id="t1", org_id="o1", user_id=user, agent_id=None, scope=SCOPE_TURN
    )


# ── 1. Exception shape ─────────────────────────────────────────────────────


def test_forecasted_budget_breach_carries_diagnostic_fields() -> None:
    exc = ForecastedBudgetBreach(
        "would breach in 90s",
        key=_k(),
        burn_rate_usd_per_sec=0.0033,
        seconds_to_breach=90.0,
        horizon_seconds=300,
    )
    assert "would breach" in str(exc)
    assert exc.burn_rate_usd_per_sec == pytest.approx(0.0033)
    assert exc.seconds_to_breach == pytest.approx(90.0)
    assert exc.horizon_seconds == 300


# ── 2. Spend-events table is populated by commit ──────────────────────────


@pytest.mark.asyncio
async def test_commit_persists_spend_event(
    sqlite_backend: SqliteBudgetBackend,
) -> None:
    key = _k()
    sqlite_backend.register(key, cap_usd=1.0)
    assert await sqlite_backend.try_reserve(key, 0.10) is True
    await sqlite_backend.commit(key, 0.10)
    rate = sqlite_backend.get_burn_rate_usd_per_second(key, window_seconds=60)
    # 0.10 committed within the last 60s → > 0 burn rate
    assert rate > 0.0


@pytest.mark.asyncio
async def test_release_does_not_emit_spend_event(
    sqlite_backend: SqliteBudgetBackend,
) -> None:
    """A reservation that is released (not committed) must not move
    the burn rate — releases are cancellations, not spend."""
    key = _k()
    sqlite_backend.register(key, cap_usd=1.0)
    assert await sqlite_backend.try_reserve(key, 0.10) is True
    await sqlite_backend.release(key, 0.10)
    rate = sqlite_backend.get_burn_rate_usd_per_second(key, window_seconds=60)
    assert rate == pytest.approx(0.0)


# ── 3. Burn-rate window aggregation ────────────────────────────────────────


@pytest.mark.asyncio
async def test_burn_rate_ignores_events_outside_window(
    sqlite_backend: SqliteBudgetBackend,
) -> None:
    """An event 5 minutes old must not influence a 60-second window."""
    key = _k()
    sqlite_backend.register(key, cap_usd=10.0)
    # Inject an old event directly so we don't need to wait wall-clock.
    sqlite_backend._record_spend_event_for_tests(
        key, amount_usd=5.0, committed_at=time.time() - 300
    )
    rate = sqlite_backend.get_burn_rate_usd_per_second(key, window_seconds=60)
    assert rate == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_burn_rate_is_usd_per_second(
    sqlite_backend: SqliteBudgetBackend,
) -> None:
    """Burn rate semantics: sum of in-window spend ÷ window seconds.
    Pin the unit so a future refactor can't silently change it."""
    key = _k()
    sqlite_backend.register(key, cap_usd=10.0)
    now = time.time()
    sqlite_backend._record_spend_event_for_tests(key, 1.0, committed_at=now - 30)
    sqlite_backend._record_spend_event_for_tests(key, 1.0, committed_at=now - 10)
    rate = sqlite_backend.get_burn_rate_usd_per_second(key, window_seconds=60)
    # $2 over 60s = $0.033/sec
    assert rate == pytest.approx(2.0 / 60.0, rel=0.01)


# ── 4. Forecast gate — off / warn / strict ────────────────────────────────


@pytest.mark.asyncio
async def test_forecast_off_is_no_op(
    sqlite_backend: SqliteBudgetBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default off: a forecasted-breach scenario still succeeds because
    the hard cap permits it."""
    monkeypatch.delenv("LLM_ROUTER_BUDGET_FORECAST_MODE", raising=False)
    key = _k()
    sqlite_backend.register(key, cap_usd=1.0)
    sqlite_backend._record_spend_event_for_tests(
        key, amount_usd=0.50, committed_at=time.time() - 150
    )
    # Without the gate, the hard cap still allows a $0.05 reservation.
    assert await sqlite_backend.try_reserve(key, 0.05) is True


@pytest.mark.asyncio
async def test_forecast_strict_raises_breach(
    sqlite_backend: SqliteBudgetBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**G-002 acceptance**: cap $1, consumed $0.50, burn $0.20/min,
    new $0.05 reservation → ``ForecastedBudgetBreach`` under strict mode
    when the projection catches an exhaustion inside the horizon."""
    monkeypatch.setenv("LLM_ROUTER_BUDGET_FORECAST_MODE", "strict")
    monkeypatch.setenv("LLM_ROUTER_BUDGET_FORECAST_HORIZON_SECONDS", "300")
    monkeypatch.setenv("LLM_ROUTER_BUDGET_FORECAST_WINDOW_SECONDS", "60")

    key = _k()
    sqlite_backend.register(key, cap_usd=1.0)
    # Move $0.50 from pending → consumed.
    assert await sqlite_backend.try_reserve(key, 0.50) is True
    await sqlite_backend.commit(key, 0.50)
    # Synthesise a recent burn matching $0.20/min = $0.0033/sec.
    now = time.time()
    sqlite_backend._record_spend_event_for_tests(
        key, amount_usd=0.20, committed_at=now - 60
    )
    # Remaining = $0.50; burn ≈ $0.20/min; ~2.5min to cap < 300s horizon → refuse.
    with pytest.raises(ForecastedBudgetBreach) as exc:
        await sqlite_backend.try_reserve(key, 0.05)
    assert exc.value.seconds_to_breach < 300


@pytest.mark.asyncio
async def test_forecast_warn_proceeds_but_logs(
    sqlite_backend: SqliteBudgetBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Warn mode: forecast catches the breach but the reservation
    still proceeds (legacy soft posture)."""
    monkeypatch.setenv("LLM_ROUTER_BUDGET_FORECAST_MODE", "warn")
    monkeypatch.setenv("LLM_ROUTER_BUDGET_FORECAST_HORIZON_SECONDS", "300")
    monkeypatch.setenv("LLM_ROUTER_BUDGET_FORECAST_WINDOW_SECONDS", "60")

    key = _k()
    sqlite_backend.register(key, cap_usd=1.0)
    await sqlite_backend.try_reserve(key, 0.50)
    await sqlite_backend.commit(key, 0.50)
    sqlite_backend._record_spend_event_for_tests(
        key, 0.20, committed_at=time.time() - 60
    )
    with patch.object(
        __import__("llm_router.budget_backend", fromlist=["log"]).log, "warning"
    ) as warn_mock:
        result = await sqlite_backend.try_reserve(key, 0.05)
    assert result is True  # proceeded
    assert warn_mock.called
    # The structured event must name the gate so operators can wire alerts.
    event_names = [c.args[0] for c in warn_mock.call_args_list if c.args]
    assert any("forecast" in name.lower() for name in event_names)


@pytest.mark.asyncio
async def test_forecast_strict_does_not_fire_when_burn_is_negligible(
    sqlite_backend: SqliteBudgetBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A zero burn rate means no projection — reservation proceeds
    even under strict mode (no false positives on idle envelopes)."""
    monkeypatch.setenv("LLM_ROUTER_BUDGET_FORECAST_MODE", "strict")
    monkeypatch.setenv("LLM_ROUTER_BUDGET_FORECAST_HORIZON_SECONDS", "300")
    monkeypatch.setenv("LLM_ROUTER_BUDGET_FORECAST_WINDOW_SECONDS", "60")
    key = _k()
    sqlite_backend.register(key, cap_usd=1.0)
    # No spend events ⇒ burn = 0.
    assert await sqlite_backend.try_reserve(key, 0.05) is True


@pytest.mark.asyncio
async def test_forecast_strict_skips_when_horizon_safe(
    sqlite_backend: SqliteBudgetBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If projected breach is beyond the horizon (e.g. burn is very low
    relative to remaining cap), strict mode does not fire."""
    monkeypatch.setenv("LLM_ROUTER_BUDGET_FORECAST_MODE", "strict")
    monkeypatch.setenv("LLM_ROUTER_BUDGET_FORECAST_HORIZON_SECONDS", "60")
    monkeypatch.setenv("LLM_ROUTER_BUDGET_FORECAST_WINDOW_SECONDS", "60")
    key = _k()
    sqlite_backend.register(key, cap_usd=100.0)
    sqlite_backend._record_spend_event_for_tests(
        key, amount_usd=0.01, committed_at=time.time() - 30
    )
    # Burn ≈ $0.00033/s; remaining = $100 → seconds to breach ≫ horizon.
    assert await sqlite_backend.try_reserve(key, 0.01) is True


# ── 5. Mode parsing parity with policy mode ───────────────────────────────


def test_forecast_mode_invalid_value_falls_back_to_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from llm_router.budget_backend import _forecast_mode
    monkeypatch.setenv("LLM_ROUTER_BUDGET_FORECAST_MODE", "yolo")
    assert _forecast_mode() == "off"


def test_forecast_mode_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    from llm_router.budget_backend import _forecast_mode
    monkeypatch.setenv("LLM_ROUTER_BUDGET_FORECAST_MODE", "STRICT")
    assert _forecast_mode() == "strict"


def test_forecast_mode_defaults_to_off(monkeypatch: pytest.MonkeyPatch) -> None:
    from llm_router.budget_backend import _forecast_mode
    monkeypatch.delenv("LLM_ROUTER_BUDGET_FORECAST_MODE", raising=False)
    assert _forecast_mode() == "off"
