"""T2-M2 (Track-2 budgets, Medium): BudgetEnvelope + parent-child
propagation.

Pins:

1. **Register + cap.** Reserve up to the cap succeeds; reserve over
   the cap fails atomically.
2. **Parent-child propagation.** A child reservation debits the
   parent envelope too; if the parent would breach, the child reserve
   is refused — no envelope is changed.
3. **Atomic.** ``try_reserve`` either commits on all envelopes in the
   chain OR commits on none.
4. **release / commit.** ``release`` reverts pending; ``commit``
   moves pending to consumed; both walk the parent chain.
5. **Concurrent reserves.** Two concurrent reserves against a tight
   cap: exactly one succeeds, one fails (lock semantics).

See: Docs/audit/post-remediation/GAP_ANALYSIS.md G-002.
"""
from __future__ import annotations

import asyncio

import pytest

from llm_router.budget import _pending_spend_by_key
from llm_router.budget_envelope import (
    BudgetEnvelope,
    BudgetEnvelopeManager,
    get_manager,
    reset_manager_for_tests,
)
from llm_router.budget_key import BudgetKey


@pytest.fixture(autouse=True)
def _isolate_manager():
    reset_manager_for_tests()
    yield
    reset_manager_for_tests()


def _k(suffix: str = "") -> BudgetKey:
    return BudgetKey(
        tenant_id="t1",
        org_id="o1",
        user_id="alice" + suffix,
        agent_id=None,
    )


# ── 1. Register + simple cap ─────────────────────────────────────────────────


def test_register_returns_envelope_with_cap() -> None:
    m = BudgetEnvelopeManager()
    env = m.register(_k(), cap_usd=10.0)
    assert isinstance(env, BudgetEnvelope)
    assert env.cap_usd == pytest.approx(10.0)
    assert env.parents == ()


def test_register_rejects_non_positive_cap() -> None:
    m = BudgetEnvelopeManager()
    with pytest.raises(ValueError, match="cap_usd must be positive"):
        m.register(_k(), cap_usd=0.0)
    with pytest.raises(ValueError, match="cap_usd must be positive"):
        m.register(_k(), cap_usd=-1.0)


def test_remaining_before_register_is_infinite() -> None:
    """An unregistered key carries no cap → infinite remaining
    (no enforcement)."""
    m = BudgetEnvelopeManager()
    assert m.remaining(_k("orphan")) == float("inf")


# ── 2. try_reserve semantics ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reserve_below_cap_succeeds() -> None:
    m = BudgetEnvelopeManager()
    m.register(_k(), cap_usd=10.0)
    assert await m.try_reserve(_k(), 3.0) is True
    assert m.pending(_k()) == pytest.approx(3.0)
    assert m.remaining(_k()) == pytest.approx(7.0)


@pytest.mark.asyncio
async def test_reserve_at_cap_succeeds() -> None:
    """Hitting the cap exactly is allowed; over the cap is not."""
    m = BudgetEnvelopeManager()
    m.register(_k(), cap_usd=10.0)
    assert await m.try_reserve(_k(), 10.0) is True
    assert m.remaining(_k()) == 0.0


@pytest.mark.asyncio
async def test_reserve_over_cap_refused_atomically() -> None:
    m = BudgetEnvelopeManager()
    m.register(_k(), cap_usd=10.0)
    await m.try_reserve(_k(), 8.0)
    # Second reserve would push to 13 > 10.
    assert await m.try_reserve(_k(), 5.0) is False
    # No change — pending stays at 8.
    assert m.pending(_k()) == pytest.approx(8.0)


@pytest.mark.asyncio
async def test_reserve_unregistered_key_passes_through() -> None:
    """No envelope = no enforcement = always allowed."""
    m = BudgetEnvelopeManager()
    assert await m.try_reserve(_k("nowhere"), 100.0) is True


@pytest.mark.asyncio
async def test_reserve_non_positive_is_noop() -> None:
    m = BudgetEnvelopeManager()
    m.register(_k(), cap_usd=10.0)
    assert await m.try_reserve(_k(), 0.0) is True
    assert await m.try_reserve(_k(), -1.0) is True
    assert m.pending(_k()) == 0.0


# ── 3. release + commit ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_release_reverts_pending() -> None:
    m = BudgetEnvelopeManager()
    m.register(_k(), cap_usd=10.0)
    await m.try_reserve(_k(), 5.0)
    await m.release(_k(), 5.0)
    assert m.pending(_k()) == 0.0


@pytest.mark.asyncio
async def test_release_floors_at_zero() -> None:
    """Over-release does not push pending negative."""
    m = BudgetEnvelopeManager()
    m.register(_k(), cap_usd=10.0)
    await m.try_reserve(_k(), 1.0)
    await m.release(_k(), 5.0)  # 5x over
    assert m.pending(_k()) == 0.0


@pytest.mark.asyncio
async def test_commit_moves_pending_to_consumed() -> None:
    m = BudgetEnvelopeManager()
    m.register(_k(), cap_usd=10.0)
    await m.try_reserve(_k(), 4.0)
    await m.commit(_k(), 4.0)
    assert m.consumed(_k()) == pytest.approx(4.0)
    assert m.pending(_k()) == 0.0
    assert m.remaining(_k()) == pytest.approx(6.0)


# ── 4. Parent-child propagation ──────────────────────────────────────────────


def _parent_child_setup() -> tuple[BudgetEnvelopeManager, BudgetKey, BudgetKey]:
    """Return (manager, parent_key, child_key) with parent=$10 and
    child=$3 whose parents=(parent,)."""
    m = BudgetEnvelopeManager()
    parent_key = BudgetKey(
        tenant_id="t1", org_id="o1", user_id="alice", agent_id=None
    )
    child_key = BudgetKey(
        tenant_id="t1", org_id="o1", user_id="alice", agent_id="agent-7"
    )
    m.register(parent_key, cap_usd=10.0)
    m.register(child_key, cap_usd=3.0, parents=(parent_key,))
    return m, parent_key, child_key


@pytest.mark.asyncio
async def test_child_reserve_debits_parent() -> None:
    m, parent_key, child_key = _parent_child_setup()
    assert await m.try_reserve(child_key, 2.0) is True
    assert m.pending(child_key) == pytest.approx(2.0)
    assert m.pending(parent_key) == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_child_refused_when_parent_full() -> None:
    """Child cap=3 has room; parent cap=10 is at 9 already; a $2
    reservation breaches the parent and must be refused atomically —
    even though it fits the child's own cap."""
    m, parent_key, child_key = _parent_child_setup()
    # Fill the parent to within $1 of its cap.
    assert await m.try_reserve(parent_key, 9.0) is True
    # Child tries to reserve $2 — fits child cap (3), breaches parent
    # (9+2 > 10).
    assert await m.try_reserve(child_key, 2.0) is False
    # No child change AND no further parent change.
    assert m.pending(child_key) == 0.0
    assert m.pending(parent_key) == pytest.approx(9.0)


@pytest.mark.asyncio
async def test_child_refused_when_child_cap_too_small() -> None:
    """Reverse case: parent has room but child cap is the binding
    constraint."""
    m, parent_key, child_key = _parent_child_setup()
    assert await m.try_reserve(child_key, 5.0) is False
    assert m.pending(child_key) == 0.0
    assert m.pending(parent_key) == 0.0


@pytest.mark.asyncio
async def test_child_release_reverts_parent_too() -> None:
    m, parent_key, child_key = _parent_child_setup()
    await m.try_reserve(child_key, 2.0)
    await m.release(child_key, 2.0)
    assert m.pending(child_key) == 0.0
    assert m.pending(parent_key) == 0.0


@pytest.mark.asyncio
async def test_child_commit_moves_parent_too() -> None:
    m, parent_key, child_key = _parent_child_setup()
    await m.try_reserve(child_key, 2.0)
    await m.commit(child_key, 2.0)
    assert m.consumed(child_key) == pytest.approx(2.0)
    assert m.consumed(parent_key) == pytest.approx(2.0)
    assert m.pending(child_key) == 0.0
    assert m.pending(parent_key) == 0.0


# ── 5. Concurrent reserves race correctly under the lock ─────────────────────


@pytest.mark.asyncio
async def test_concurrent_reserves_against_tight_cap() -> None:
    """50 concurrent $1 reserves against a $10 cap → exactly 10 succeed."""
    m = BudgetEnvelopeManager()
    m.register(_k(), cap_usd=10.0)

    async def one():
        return await m.try_reserve(_k(), 1.0)

    results = await asyncio.gather(*(one() for _ in range(50)))
    assert sum(1 for r in results if r) == 10
    assert sum(1 for r in results if not r) == 40
    assert m.pending(_k()) == pytest.approx(10.0)


# ── 6. Singleton + global pending dict integration ───────────────────────────


@pytest.mark.asyncio
async def test_singleton_reset_clears_global_pending() -> None:
    """The reset_manager_for_tests helper must also clear the global
    _pending_spend_by_key dict so leakage between tests is impossible."""
    m = get_manager()
    m.register(_k(), cap_usd=10.0)
    await m.try_reserve(_k(), 3.0)
    assert _pending_spend_by_key.get(_k(), 0.0) > 0
    reset_manager_for_tests()
    assert _pending_spend_by_key.get(_k(), 0.0) == 0.0
