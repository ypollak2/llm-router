"""Regression: RED1-7-01 — commit_envelope must settle atomically (single txn).

release(est) then commit(actual) were two separate transactions; a concurrent
try_reserve landing in the gap saw pending decremented but consumed not yet
incremented and could be admitted past a shared cap. commit_envelope now calls a
single atomic settle(key, est, actual). These verify settle's accounting and that
commit_envelope routes through it, preserving a concurrent sibling's reservation.
"""
from __future__ import annotations

import types
from pathlib import Path

import pytest

from llm_router.budget_backend import SqliteBudgetBackend
from llm_router.budget_envelope import BudgetEnvelopeManager
from llm_router.budget_key import BudgetKey, SCOPE_TURN, budget_key_from_identity
from llm_router.quota_envelope_routing import commit_envelope, reserve_envelope


def _identity(user_id="alice", org_id="acme"):
    return types.SimpleNamespace(tenant_id=org_id, org_id=org_id, user_id=user_id, agent_id=None)


@pytest.fixture(autouse=True)
def _strict(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_ENVELOPE_MODE", "strict")


@pytest.mark.asyncio
async def test_settle_does_pending_minus_est_consumed_plus_actual(tmp_path: Path):
    be = SqliteBudgetBackend(db_path=tmp_path / "e.db")
    key = BudgetKey(tenant_id="acme", org_id="acme", user_id="alice", agent_id=None, scope=SCOPE_TURN)
    be.register(key, cap_usd=10.0)
    await be.try_reserve(key, 2.0)          # pending = 2.0 (est)
    await be.settle(key, 2.0, 0.5)          # undo est 2.0, record actual 0.5
    assert be.pending(key) == pytest.approx(0.0), "reservation not fully undone"
    assert be.consumed(key) == pytest.approx(0.5), "actual spend not recorded"


@pytest.mark.asyncio
async def test_commit_envelope_preserves_sibling_via_settle(tmp_path: Path):
    be = SqliteBudgetBackend(db_path=tmp_path / "e.db")
    key = budget_key_from_identity(_identity())
    be.register(key, cap_usd=2.0)
    _, _, kA = await reserve_envelope(_identity(), 1.0, backend=be)
    await reserve_envelope(_identity(), 1.0, backend=be)  # B outstanding
    await commit_envelope(kA, 1.0, 1.0, backend=be)       # A settles atomically
    assert be.pending(key) == pytest.approx(1.0), "sibling B's reservation eroded"
    assert be.consumed(key) == pytest.approx(1.0)
    # Third caller must still be refused (cap fully committed to A + B).
    _, okC, _ = await reserve_envelope(_identity(), 1.0, backend=be)
    assert not okC


@pytest.mark.asyncio
async def test_commit_envelope_uses_atomic_settle_not_two_steps(tmp_path: Path):
    """Spy backend: commit_envelope must call settle() once, not release()+commit()."""
    calls = []

    class Spy:
        async def settle(self, key, est, actual):
            calls.append(("settle", est, actual))
        async def release(self, key, cost):
            calls.append(("release", cost))
        async def commit(self, key, cost, *, settle_pending=True):
            calls.append(("commit", cost))

    await commit_envelope("k", 1.0, 0.7, backend=Spy())
    assert calls == [("settle", 1.0, 0.7)], f"expected a single atomic settle, got {calls}"


@pytest.mark.asyncio
async def test_in_memory_settle_accounting():
    mgr = BudgetEnvelopeManager()
    key = BudgetKey(tenant_id="acme", org_id="acme", user_id="alice", agent_id=None, scope=SCOPE_TURN)
    mgr.register(key, cap_usd=10.0)
    await mgr.try_reserve(key, 2.0)
    await mgr.settle(key, 2.0, 1.5)
    assert mgr.pending(key) == pytest.approx(0.0)
    assert mgr.consumed(key) == pytest.approx(1.5)


@pytest.mark.asyncio
async def test_legacy_backend_without_settle_falls_back(tmp_path: Path):
    """A backend that predates settle() must still work via release()+commit()."""
    calls = []

    class Legacy:
        async def release(self, key, cost):
            calls.append(("release", cost))
        async def commit(self, key, cost, *, settle_pending=True):
            calls.append(("commit", cost, settle_pending))

    await commit_envelope("k", 1.0, 0.7, backend=Legacy())
    assert calls == [("release", 1.0), ("commit", 0.7, False)]
