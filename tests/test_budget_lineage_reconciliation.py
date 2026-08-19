"""#70 (core) — budget-lineage subtree spend-conservation reconciliation.

Unit tests pin the invariant on synthetic rows (including the leak and missing-
link failure modes, which the real backend can't produce because it always
debits the whole chain). One integration test drives the real SqliteBudgetBackend
end-to-end (register parent + child, spend on the child, enumerate, reconcile →
converged) so the enumeration projection and the reconciler agree in practice.
"""
from __future__ import annotations

import pytest

from llm_router.budget_backend import SqliteBudgetBackend
from llm_router.budget_key import SCOPE_TURN, BudgetKey
from llm_router.budget_lineage_reconciliation import (
    KIND_UNDERDEBITED,
    KIND_UNREGISTERED,
    reconcile_budget_lineage,
    reconcile_budget_lineage_audited,
)


# ── Unit tests on synthetic rows ──────────────────────────────────────────

def test_converged_when_parent_covers_children() -> None:
    # parent consumed 10 == child A (6) + child B (4).
    rows = [
        ("parent", 10.0, ()),
        ("childA", 6.0, ("parent",)),
        ("childB", 4.0, ("parent",)),
    ]
    r = reconcile_budget_lineage(rows)
    assert r["converged"] is True
    assert r["violations"] == []
    assert r["envelope_count"] == 3
    assert r["parent_count"] == 1


def test_parent_may_exceed_child_sum() -> None:
    # Parent also charged directly (12 > 6+4). Still converged — parent only
    # must be >= the sum, never below it.
    rows = [
        ("parent", 12.0, ()),
        ("childA", 6.0, ("parent",)),
        ("childB", 4.0, ("parent",)),
    ]
    assert reconcile_budget_lineage(rows)["converged"] is True


def test_underdebited_parent_flagged() -> None:
    # The G-009 leak: children spent 10 but the parent only shows 7.
    rows = [
        ("parent", 7.0, ()),
        ("childA", 6.0, ("parent",)),
        ("childB", 4.0, ("parent",)),
    ]
    r = reconcile_budget_lineage(rows)
    assert r["converged"] is False
    v = r["violations"][0]
    assert v["parent"] == "parent"
    assert v["kind"] == KIND_UNDERDEBITED
    assert v["shortfall"] == pytest.approx(3.0)


def test_unregistered_parent_flagged() -> None:
    rows = [("childA", 6.0, ("ghost_parent",))]
    r = reconcile_budget_lineage(rows)
    assert r["converged"] is False
    assert r["violations"][0]["kind"] == KIND_UNREGISTERED


def test_tolerance_absorbs_float_noise() -> None:
    rows = [
        ("parent", 10.0 - 1e-12, ()),
        ("child", 10.0, ("parent",)),
    ]
    assert reconcile_budget_lineage(rows)["converged"] is True


def test_empty_ledger_converges() -> None:
    r = reconcile_budget_lineage([])
    assert r["converged"] is True
    assert r["envelope_count"] == 0


def test_multi_level_chain() -> None:
    # grandparent >= parent >= leaf spend, all consistent.
    rows = [
        ("gp", 5.0, ()),
        ("parent", 5.0, ("gp",)),
        ("leaf", 5.0, ("parent", "gp")),
    ]
    # leaf debits both parent and gp; parent debits gp. Consistent chain:
    # gp child-sum = leaf(5) + parent(5) = 10 > gp consumed 5  → this SHOULD flag,
    # because a real chain would have gp consumed >= 10. Guards the math.
    r = reconcile_budget_lineage(rows)
    assert r["converged"] is False
    assert any(v["parent"] == "gp" for v in r["violations"])


# ── Integration through the real SQLite backend ───────────────────────────

def _k(user: str) -> BudgetKey:
    return BudgetKey(tenant_id="t1", org_id="o1", user_id=user,
                     agent_id=None, scope=SCOPE_TURN)


@pytest.mark.asyncio
async def test_backend_lineage_reconciles_after_real_spend(tmp_path) -> None:
    backend = SqliteBudgetBackend(db_path=tmp_path / "budgets.db")
    try:
        parent = _k("team")
        child = BudgetKey(tenant_id="t1", org_id="o1", user_id="team",
                          agent_id="agent-1", scope=SCOPE_TURN)
        backend.register(parent, cap_usd=100.0)
        backend.register(child, cap_usd=100.0, parents=(parent,))

        # Real spend on the child: reserve+commit debits the whole chain.
        assert await backend.try_reserve(child, 3.0) is True
        await backend.commit(child, 3.0)
        assert await backend.try_reserve(child, 2.0) is True
        await backend.commit(child, 2.0)

        # The backend always debits the parent, so the ledger must reconcile.
        r = reconcile_budget_lineage(backend.iter_lineage_consumed())
        assert r["converged"] is True, r["violations"]
        assert backend.consumed(parent) == pytest.approx(5.0)
        assert backend.consumed(child) == pytest.approx(5.0)
    finally:
        backend.close()


# ── Reconcile against the control-plane audit log (#70 titular half) ───────

@pytest.fixture()
def cp_audit(tmp_path, monkeypatch):
    """Isolated control-plane audit log for the audited reconciliation tests."""
    from llm_router.control_plane import audit as cpa

    monkeypatch.setenv("LLM_ROUTER_CP_AUDIT_PATH", str(tmp_path / "cp_audit.db"))
    cpa.reset_cp_audit_log_for_tests()
    yield cpa
    cpa.reset_cp_audit_log_for_tests()


@pytest.mark.asyncio
async def test_audited_reconcile_writes_tamper_evident_row(tmp_path, cp_audit) -> None:
    backend = SqliteBudgetBackend(db_path=tmp_path / "b.db")
    try:
        parent = _k("team")
        child = BudgetKey(tenant_id="t1", org_id="o1", user_id="team",
                          agent_id="a1", scope=SCOPE_TURN)
        backend.register(parent, cap_usd=50.0)
        backend.register(child, cap_usd=50.0, parents=(parent,))
        assert await backend.try_reserve(child, 4.0) is True
        await backend.commit(child, 4.0)

        r = reconcile_budget_lineage_audited(backend, scope="t1")
        assert r["converged"] is True
        assert r["audit_chain_status"] == "ok"
        # The reconciliation itself is now an auditable control-plane event.
        actions = {row["action"] for row in cp_audit.get_cp_audit_log().recent(limit=10)}
        assert cp_audit.ACTION_BUDGET_LINEAGE_RECON in actions
        # And the attestation chain verifies.
        cp_audit.verify_cp_audit_chain()
    finally:
        backend.close()


@pytest.mark.asyncio
async def test_audited_reconcile_surfaces_tampered_chain(tmp_path, cp_audit) -> None:
    import json
    import sqlite3

    backend = SqliteBudgetBackend(db_path=tmp_path / "b.db")
    try:
        backend.register(_k("solo"), cap_usd=10.0)
        # Seed two audit rows, then tamper the CP audit DB directly.
        reconcile_budget_lineage_audited(backend, scope="t1")
        reconcile_budget_lineage_audited(backend, scope="t1")
        cp_audit.reset_cp_audit_log_for_tests()
        db = tmp_path / "cp_audit.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "UPDATE audit_events SET detail = ? WHERE seq = (SELECT MIN(seq) FROM audit_events)",
            (json.dumps({"forged": True}),),
        )
        conn.commit()
        conn.close()
        cp_audit.reset_cp_audit_log_for_tests()

        # audit=False: do not extend a chain we just broke.
        r = reconcile_budget_lineage_audited(backend, scope="t1", audit=False)
        assert r["audit_chain_status"] == "tampered"
    finally:
        backend.close()
