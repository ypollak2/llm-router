"""Regression: RED1-6-01 — parent-chain rollup must walk ALL levels, not one hop.

A cap registered 2+ levels above a reservation key (org → user → agent, each
pointing only at its immediate parent — the shape BudgetKey.rolls_up_to builds)
previously never saw the spend and was silently unenforceable. All three backends
now walk the chain transitively.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from llm_router.budget_backend import SqliteBudgetBackend
from llm_router.budget_envelope import BudgetEnvelopeManager
from llm_router.budget_key import BudgetKey, SCOPE_TURN


def _keys():
    org = BudgetKey(tenant_id="acme", org_id="acme", user_id=None, agent_id=None, scope=SCOPE_TURN)
    user = BudgetKey(tenant_id="acme", org_id="acme", user_id="alice", agent_id=None, scope=SCOPE_TURN)
    agent = BudgetKey(tenant_id="acme", org_id="acme", user_id="alice", agent_id="a1", scope=SCOPE_TURN)
    return org, user, agent


@pytest.mark.asyncio
async def test_sqlite_org_two_hops_up_sees_spend(tmp_path: Path):
    be = SqliteBudgetBackend(db_path=tmp_path / "e.db")
    org, user, agent = _keys()
    be.register(org, cap_usd=2.0)
    be.register(user, cap_usd=5.0, parents=(org,))
    be.register(agent, cap_usd=10.0, parents=(user,))

    assert await be.try_reserve(agent, 1.0) is True

    assert be.pending(agent) == pytest.approx(1.0)
    assert be.pending(user) == pytest.approx(1.0), "1 hop up not settled"
    assert be.pending(org) == pytest.approx(1.0), "RED1-6-01: org 2 hops up never saw the spend"


@pytest.mark.asyncio
async def test_sqlite_org_cap_enforced_two_hops_up(tmp_path: Path):
    """The org cap (tightest, 2 hops up) must actually refuse an over-cap reserve."""
    be = SqliteBudgetBackend(db_path=tmp_path / "e.db")
    org, user, agent = _keys()
    be.register(org, cap_usd=1.0)          # org cap is the binding constraint
    be.register(user, cap_usd=100.0, parents=(org,))
    be.register(agent, cap_usd=100.0, parents=(user,))

    assert await be.try_reserve(agent, 0.6) is True
    # Second 0.6 would push org to 1.2 > 1.0 → must be refused via the 2-hop parent.
    assert await be.try_reserve(agent, 0.6) is False, "RED1-6-01: org cap 2 hops up not enforced"


@pytest.mark.asyncio
async def test_sqlite_settle_reaches_org(tmp_path: Path):
    be = SqliteBudgetBackend(db_path=tmp_path / "e.db")
    org, user, agent = _keys()
    be.register(org, cap_usd=10.0)
    be.register(user, cap_usd=10.0, parents=(org,))
    be.register(agent, cap_usd=10.0, parents=(user,))
    await be.try_reserve(agent, 1.0)
    await be.release(agent, 1.0)
    await be.commit(agent, 1.0, settle_pending=False)
    assert be.pending(org) == pytest.approx(0.0)
    assert be.consumed(org) == pytest.approx(1.0), "org consumed not updated transitively"


@pytest.mark.asyncio
async def test_in_memory_manager_parity():
    mgr = BudgetEnvelopeManager()
    org, user, agent = _keys()
    mgr.register(org, cap_usd=2.0)
    mgr.register(user, cap_usd=5.0, parents=(org,))
    mgr.register(agent, cap_usd=10.0, parents=(user,))
    assert await mgr.try_reserve(agent, 1.0) is True
    assert mgr.pending(org) == pytest.approx(1.0), "in-memory manager: org 2 hops up not settled"
