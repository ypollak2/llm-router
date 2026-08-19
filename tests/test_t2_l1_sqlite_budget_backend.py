"""T2-L1 (Track 2 budget safety, Large): distributed-safe budget backend.

Phase 3a target: persist envelope state to a single SQLite file with
atomic check-then-charge under contention. T2-XL1 will add the
multi-instance coordination layer in Phase 3b; this PR ships the
single-instance backend + the ``BudgetBackend`` protocol so the in-memory
manager and the persistent backend share one contract.

Acceptance criteria (from G-002):

    100 concurrent calls against a budget of N → exactly N succeed,
    100 − N raise QuotaExceeded BEFORE any provider call.

That guarantee lives in ``test_tst003_concurrency_acceptance`` below.

See: Docs/audit/post-remediation/GAP_ANALYSIS.md G-002.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from llm_router.budget_backend import (
    BudgetBackend,
    SqliteBudgetBackend,
    get_budget_backend,
    reset_budget_backend_for_tests,
)
from llm_router.budget_envelope import BudgetEnvelopeManager
from llm_router.budget_key import SCOPE_TURN, BudgetKey


@pytest.fixture(autouse=True)
def _reset_singleton() -> None:
    """Clear the module-level backend singleton between tests."""
    reset_budget_backend_for_tests()
    yield
    reset_budget_backend_for_tests()


@pytest.fixture
def sqlite_backend(tmp_path: Path) -> SqliteBudgetBackend:
    return SqliteBudgetBackend(db_path=tmp_path / "budgets.db")


def _k(user: str = "alice", scope: str = SCOPE_TURN) -> BudgetKey:
    return BudgetKey(
        tenant_id="t1", org_id="o1", user_id=user, agent_id=None, scope=scope
    )


# ── 1. Protocol shape ──────────────────────────────────────────────────────


def test_protocol_is_runtime_checkable_and_existing_manager_satisfies_it() -> None:
    """Existing BudgetEnvelopeManager must satisfy BudgetBackend without
    inheritance — duck-typed Protocol. This lets the in-memory manager
    keep its existing public type while the new backend opts in."""
    assert isinstance(BudgetBackend, type) or hasattr(BudgetBackend, "_is_protocol")
    manager = BudgetEnvelopeManager()
    assert isinstance(manager, BudgetBackend)


def test_protocol_advertises_required_methods() -> None:
    required = {
        "register",
        "get",
        "consumed",
        "pending",
        "remaining",
        "try_reserve",
        "release",
        "commit",
        "tier_state",
    }
    # Sourced from __annotations__ + dir to handle both Protocol styles.
    advertised = set(dir(BudgetBackend))
    missing = required - advertised
    assert not missing, f"Protocol is missing: {missing}"


# ── 2. SqliteBudgetBackend basic lifecycle ─────────────────────────────────


def test_register_persists_envelope(sqlite_backend: SqliteBudgetBackend) -> None:
    key = _k()
    env = sqlite_backend.register(key, cap_usd=1.0)
    assert env.cap_usd == pytest.approx(1.0)
    assert sqlite_backend.get(key) is not None
    assert sqlite_backend.consumed(key) == pytest.approx(0.0)
    assert sqlite_backend.pending(key) == pytest.approx(0.0)
    assert sqlite_backend.remaining(key) == pytest.approx(1.0)


def test_register_rejects_non_positive_cap(sqlite_backend: SqliteBudgetBackend) -> None:
    with pytest.raises(ValueError, match="cap_usd must be positive"):
        sqlite_backend.register(_k(), cap_usd=0.0)
    with pytest.raises(ValueError, match="cap_usd must be positive"):
        sqlite_backend.register(_k(), cap_usd=-1.0)


def test_register_validates_soft_cap(sqlite_backend: SqliteBudgetBackend) -> None:
    key = _k()
    with pytest.raises(ValueError, match="strictly less"):
        sqlite_backend.register(key, cap_usd=1.0, soft_cap_usd=1.0)
    with pytest.raises(ValueError, match="strictly less"):
        sqlite_backend.register(key, cap_usd=1.0, soft_cap_usd=2.0)


@pytest.mark.asyncio
async def test_try_reserve_succeeds_under_cap(
    sqlite_backend: SqliteBudgetBackend,
) -> None:
    key = _k()
    sqlite_backend.register(key, cap_usd=1.0)
    assert await sqlite_backend.try_reserve(key, 0.4) is True
    assert sqlite_backend.pending(key) == pytest.approx(0.4)
    assert sqlite_backend.remaining(key) == pytest.approx(0.6)


@pytest.mark.asyncio
async def test_try_reserve_refuses_over_cap(
    sqlite_backend: SqliteBudgetBackend,
) -> None:
    key = _k()
    sqlite_backend.register(key, cap_usd=1.0)
    assert await sqlite_backend.try_reserve(key, 0.6) is True
    # 0.6 + 0.6 = 1.2 > 1.0 cap → refused, state unchanged
    assert await sqlite_backend.try_reserve(key, 0.6) is False
    assert sqlite_backend.pending(key) == pytest.approx(0.6)


@pytest.mark.asyncio
async def test_commit_moves_pending_to_consumed(
    sqlite_backend: SqliteBudgetBackend,
) -> None:
    key = _k()
    sqlite_backend.register(key, cap_usd=1.0)
    assert await sqlite_backend.try_reserve(key, 0.4) is True
    await sqlite_backend.commit(key, 0.4)
    assert sqlite_backend.consumed(key) == pytest.approx(0.4)
    assert sqlite_backend.pending(key) == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_release_reverts_reservation(
    sqlite_backend: SqliteBudgetBackend,
) -> None:
    key = _k()
    sqlite_backend.register(key, cap_usd=1.0)
    assert await sqlite_backend.try_reserve(key, 0.4) is True
    await sqlite_backend.release(key, 0.4)
    assert sqlite_backend.pending(key) == pytest.approx(0.0)
    assert sqlite_backend.remaining(key) == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_unregistered_key_is_unenforced(
    sqlite_backend: SqliteBudgetBackend,
) -> None:
    """No envelope registered → no cap → try_reserve always True."""
    key = _k(user="ghost")
    assert await sqlite_backend.try_reserve(key, 999.0) is True
    assert sqlite_backend.remaining(key) == float("inf")


# ── 3. Persistence — survives backend re-open ───────────────────────────────


@pytest.mark.asyncio
async def test_state_persists_across_backend_instances(tmp_path: Path) -> None:
    """Phase 3a guarantee: budget state survives a daemon restart.

    The whole point of T2-L1 is that the in-process BudgetEnvelopeManager
    loses everything on process exit. A persistent backend must round-trip
    consumed + pending across a fresh open of the same DB file."""
    db = tmp_path / "budgets.db"
    key = _k()

    backend_a = SqliteBudgetBackend(db_path=db)
    backend_a.register(key, cap_usd=1.0)
    assert await backend_a.try_reserve(key, 0.4) is True
    await backend_a.commit(key, 0.4)
    backend_a.close()

    backend_b = SqliteBudgetBackend(db_path=db)
    assert backend_b.consumed(key) == pytest.approx(0.4)
    assert backend_b.pending(key) == pytest.approx(0.0)
    # And new reservations must respect the persisted consumed total.
    assert await backend_b.try_reserve(key, 0.5) is True
    assert await backend_b.try_reserve(key, 0.5) is False  # 0.4+0.5+0.5 > 1.0
    backend_b.close()


# ── 4. Parent-child atomicity ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_parent_chain_atomicity(sqlite_backend: SqliteBudgetBackend) -> None:
    """If the parent cap would be breached, the child reservation is
    refused and NEITHER envelope is changed."""
    parent = _k(user="alice", scope="monthly")
    child = _k(user="alice", scope=SCOPE_TURN)
    sqlite_backend.register(parent, cap_usd=1.0)
    sqlite_backend.register(child, cap_usd=10.0, parents=(parent,))
    # Child cap allows 0.8, but parent only has 1.0; first 0.8 succeeds.
    assert await sqlite_backend.try_reserve(child, 0.8) is True
    # Second 0.8 would push parent to 1.6 > 1.0 → refused atomically.
    assert await sqlite_backend.try_reserve(child, 0.8) is False
    assert sqlite_backend.pending(parent) == pytest.approx(0.8)
    assert sqlite_backend.pending(child) == pytest.approx(0.8)


# ── 5. TST-003 — concurrency acceptance ────────────────────────────────────


@pytest.mark.asyncio
async def test_tst003_concurrency_acceptance(
    sqlite_backend: SqliteBudgetBackend,
) -> None:
    """100 concurrent reservations of $0.10 against a $5.00 cap →
    exactly 50 succeed, 50 are refused. From G-002 acceptance criteria.

    The in-memory BudgetEnvelopeManager already gives this guarantee
    under asyncio.Lock; the SQLite backend has to honour it too via
    BEGIN IMMEDIATE transactions.
    """
    key = _k()
    sqlite_backend.register(key, cap_usd=5.0)

    async def attempt() -> bool:
        return await sqlite_backend.try_reserve(key, 0.10)

    results = await asyncio.gather(*(attempt() for _ in range(100)))
    successes = sum(1 for r in results if r)
    assert successes == 50, f"Expected exactly 50, got {successes}"
    # Pending matches what was actually charged.
    assert sqlite_backend.pending(key) == pytest.approx(5.0)


# ── 6. Factory + env selection ─────────────────────────────────────────────


def test_factory_defaults_to_sqlite(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_ROUTER_BUDGET_BACKEND", raising=False)
    backend = get_budget_backend()
    assert isinstance(backend, SqliteBudgetBackend)


def test_factory_returns_memory_when_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_ROUTER_BUDGET_BACKEND", "memory")
    backend = get_budget_backend()
    assert isinstance(backend, BudgetEnvelopeManager)


def test_factory_invalid_value_falls_back_to_sqlite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Misconfigured env var must not break startup — fail-open to
    the safer default (persistent), mirroring the policy-mode pattern."""
    monkeypatch.setenv("LLM_ROUTER_BUDGET_BACKEND", "yolo")
    backend = get_budget_backend()
    assert isinstance(backend, SqliteBudgetBackend)


def test_factory_is_singleton_per_call_site(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_ROUTER_BUDGET_BACKEND", "memory")
    assert get_budget_backend() is get_budget_backend()
