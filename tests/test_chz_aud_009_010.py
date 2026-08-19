"""Regression tests for CHZ-AUD-009 and CHZ-AUD-010.

009: SqliteBudgetBackend must accept the LAST valid reservation. Float
     accumulation in SQLite REAL + strict '>' comparison rejected the
     final slot (49 succeed instead of 50 for cap/cost = 50).

010: In strict forecast mode a ForecastedBudgetBreach must NOT leave an
     orphaned pending reservation — the pending UPDATE/COMMIT was ordered
     BEFORE the forecast raise, so pending accumulated permanently.
"""

from __future__ import annotations

import asyncio

import pytest

from llm_router.budget_backend import ForecastedBudgetBreach, SqliteBudgetBackend
from llm_router.budget_key import BudgetKey


def _key(user: str = "alice") -> BudgetKey:
    return BudgetKey(
        tenant_id="t1", org_id="o1", user_id=user, agent_id=None, scope="turn"
    )


def _backend(tmp_path) -> SqliteBudgetBackend:
    return SqliteBudgetBackend(db_path=tmp_path / "budgets.db")


# ── CHZ-AUD-009 ───────────────────────────────────────────────────────────


def test_009_sequential_last_slot_accepted(tmp_path):
    """cap=$0.05, cost=$0.001 → exactly 50 reservations must succeed."""
    backend = _backend(tmp_path)
    key = _key()
    backend.register(key=key, cap_usd=0.05)

    async def run() -> int:
        successes = 0
        for _ in range(100):
            if await backend.try_reserve(key, 0.001):
                successes += 1
        return successes

    successes = asyncio.run(run())
    backend.close()
    assert successes == 50, f"expected 50, got {successes}"


def test_009_concurrent_exactly_N_succeed(tmp_path):
    """G-002: 100 concurrent reserves against cap/cost=50 → exactly 50 win."""
    backend = _backend(tmp_path)
    key = _key("bob")
    backend.register(key=key, cap_usd=0.05)

    async def run() -> int:
        results = await asyncio.gather(
            *[backend.try_reserve(key, 0.001) for _ in range(100)]
        )
        return sum(1 for r in results if r)

    successes = asyncio.run(run())
    backend.close()
    assert successes == 50, f"expected 50, got {successes}"


# ── CHZ-AUD-010 ───────────────────────────────────────────────────────────


def test_010_strict_breach_leaves_no_orphan_pending(tmp_path, monkeypatch):
    """Strict forecast mode: a breach must roll back the pending reservation."""
    monkeypatch.setenv("LLM_ROUTER_BUDGET_FORECAST_MODE", "strict")
    backend = _backend(tmp_path)
    key = _key("carol")
    backend.register(key=key, cap_usd=1.0)

    # Seed burn-rate history so the forecast gate fires: a high recent
    # burn rate projects a breach inside the horizon.
    import time as _time

    now = _time.time()
    for i in range(5):
        backend._record_spend_event_for_tests(key, 0.2, now - i)

    pending_before = backend.tier_state(key)["pending_usd"]

    async def run() -> None:
        await backend.try_reserve(key, 0.001)

    with pytest.raises(ForecastedBudgetBreach):
        asyncio.run(run())

    pending_after = backend.tier_state(key)["pending_usd"]
    backend.close()
    assert pending_after == pending_before, (
        f"orphaned pending: before={pending_before} after={pending_after}"
    )


def test_010_warn_mode_keeps_pending(tmp_path, monkeypatch):
    """Warn mode still commits the reservation (no raise, pending stands)."""
    monkeypatch.setenv("LLM_ROUTER_BUDGET_FORECAST_MODE", "warn")
    backend = _backend(tmp_path)
    key = _key("dave")
    backend.register(key=key, cap_usd=1.0)

    import time as _time

    now = _time.time()
    for i in range(5):
        backend._record_spend_event_for_tests(key, 0.2, now - i)

    async def run() -> bool:
        return await backend.try_reserve(key, 0.001)

    ok = asyncio.run(run())
    pending_after = backend.tier_state(key)["pending_usd"]
    backend.close()
    assert ok is True
    assert pending_after == pytest.approx(0.001)
