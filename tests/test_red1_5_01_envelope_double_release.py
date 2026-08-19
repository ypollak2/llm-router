"""Regression: RED1-5-01 — envelope commit must not double-decrement pending_usd.

`commit_envelope` settles a turn with `release(est)` (undo the reservation) then
`commit(actual)` (record real spend). Both used to subtract from `pending_usd`, so
on a SHARED envelope key one caller's settle erased a concurrent sibling's
still-outstanding reservation, letting a later caller be admitted past the hard cap.
`commit(..., settle_pending=False)` now records consumed only. These tests exercise
the REAL SqliteBudgetBackend + quota_envelope_routing bridge (the production path).
"""
from __future__ import annotations

import types
from pathlib import Path

import pytest

from llm_router.budget_backend import SqliteBudgetBackend
from llm_router.budget_key import budget_key_from_identity
from llm_router.quota_envelope_routing import commit_envelope, reserve_envelope


def _identity(user_id="alice", org_id="acme"):
    return types.SimpleNamespace(
        tenant_id=org_id, org_id=org_id, user_id=user_id, agent_id=None
    )


@pytest.fixture
def backend(tmp_path: Path) -> SqliteBudgetBackend:
    return SqliteBudgetBackend(db_path=tmp_path / "envelopes.db")


@pytest.fixture(autouse=True)
def _strict(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_ENVELOPE_MODE", "strict")


@pytest.mark.asyncio
async def test_sibling_reservation_survives_commit_on_shared_key(backend):
    """A committing must not erase B's outstanding reservation on a shared key."""
    key = budget_key_from_identity(_identity())
    backend.register(key, cap_usd=2.0)

    _, okA, kA = await reserve_envelope(_identity(), 1.0, backend=backend)
    _, okB, _ = await reserve_envelope(_identity(), 1.0, backend=backend)
    assert okA and okB
    assert backend.pending(key) == pytest.approx(2.0)

    # A settles via the production path (release est + commit actual).
    await commit_envelope(kA, 1.0, 1.0, backend=backend)

    # B is still in flight: pending must still reflect B's $1.00 reservation.
    assert backend.pending(key) == pytest.approx(1.0), (
        "RED1-5-01: A's commit eroded B's outstanding reservation"
    )
    assert backend.consumed(key) == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_third_caller_refused_past_cap_after_sibling_commit(backend):
    """With consumed(A)=1 + outstanding(B)=1 against cap 2, C must be refused."""
    key = budget_key_from_identity(_identity())
    backend.register(key, cap_usd=2.0)

    _, _, kA = await reserve_envelope(_identity(), 1.0, backend=backend)
    await reserve_envelope(_identity(), 1.0, backend=backend)  # B outstanding
    await commit_envelope(kA, 1.0, 1.0, backend=backend)

    _, okC, _ = await reserve_envelope(_identity(), 1.0, backend=backend)
    assert not okC, "RED1-5-01: C admitted past the hard cap (true exposure would be 3.0 > 2.0)"


@pytest.mark.asyncio
async def test_actual_below_estimate_settles_pending_exactly(backend):
    """release(est) undoes the full reservation; commit(actual<est) records only
    actual to consumed, leaving no pending residue and no over-release."""
    key = budget_key_from_identity(_identity())
    backend.register(key, cap_usd=10.0)

    _, _, kA = await reserve_envelope(_identity(), 2.0, backend=backend)  # est 2.0
    assert backend.pending(key) == pytest.approx(2.0)
    await commit_envelope(kA, 2.0, 0.5, backend=backend)  # actual 0.5

    assert backend.pending(key) == pytest.approx(0.0), "reservation not fully released"
    assert backend.consumed(key) == pytest.approx(0.5), "consumed must reflect actual spend"


@pytest.mark.asyncio
async def test_standalone_commit_still_moves_pending_to_consumed(backend):
    """settle_pending defaults True: a lone commit() (no prior release) still
    moves pending→consumed, preserving the standalone contract."""
    key = budget_key_from_identity(_identity())
    backend.register(key, cap_usd=5.0)

    await backend.try_reserve(key, 1.0)
    assert backend.pending(key) == pytest.approx(1.0)
    await backend.commit(key, 1.0)  # no settle_pending kwarg → default True
    assert backend.pending(key) == pytest.approx(0.0)
    assert backend.consumed(key) == pytest.approx(1.0)
