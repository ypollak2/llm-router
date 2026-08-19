"""P0-3 — the distributed budget envelope actually enforces on the routing path.

Exercises the ``quota_envelope_routing`` bridge against the REAL
``SqliteBudgetBackend`` (the same backend + key derivation ``route_and_call``
uses), proving a routed turn decrements a shared envelope and an over-cap turn
is refused — the audit's acceptance criterion for wiring the envelope backend.
"""
from __future__ import annotations

import types
from pathlib import Path

import pytest

from llm_router.budget_backend import SqliteBudgetBackend
from llm_router.budget_key import budget_key_from_identity
from llm_router.quota_envelope_routing import (
    commit_envelope,
    release_envelope,
    reserve_envelope,
)


def _identity(user_id="alice", org_id="acme"):
    return types.SimpleNamespace(
        tenant_id=org_id, org_id=org_id, user_id=user_id, agent_id=None,
    )


@pytest.fixture
def backend(tmp_path: Path) -> SqliteBudgetBackend:
    return SqliteBudgetBackend(db_path=tmp_path / "envelopes.db")


@pytest.fixture(autouse=True)
def _strict(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_ENVELOPE_MODE", "strict")


@pytest.mark.asyncio
async def test_routed_turn_decrements_shared_envelope(backend):
    ident = _identity()
    key = budget_key_from_identity(ident)
    backend.register(key, cap_usd=1.0)

    mode, ok, rkey = await reserve_envelope(ident, 0.10, backend=backend)
    assert mode == "strict" and ok is True
    await commit_envelope(rkey, 0.10, 0.08, backend=backend)

    # Reservation undone, true spend recorded — pending clean even though the
    # estimate (0.10) differed from the actual (0.08).
    assert backend.consumed(key) == pytest.approx(0.08)
    assert backend.pending(key) == pytest.approx(0.0)
    assert backend.remaining(key) == pytest.approx(0.92)


@pytest.mark.asyncio
async def test_over_cap_reservation_refused(backend):
    ident = _identity()
    key = budget_key_from_identity(ident)
    backend.register(key, cap_usd=0.05)

    _, ok, _ = await reserve_envelope(ident, 0.10, backend=backend)
    assert ok is False  # caller refuses the turn — zero spend


@pytest.mark.asyncio
async def test_envelope_aggregates_across_turns(backend):
    """A shared envelope is decremented turn over turn until it's exhausted."""
    ident = _identity()
    key = budget_key_from_identity(ident)
    backend.register(key, cap_usd=1.0)

    _, ok1, k1 = await reserve_envelope(ident, 0.60, backend=backend)
    assert ok1
    await commit_envelope(k1, 0.60, 0.60, backend=backend)

    # 0.60 already committed; a second 0.60 turn would project to 1.20 > 1.0.
    _, ok2, _ = await reserve_envelope(ident, 0.60, backend=backend)
    assert ok2 is False
    assert backend.consumed(key) == pytest.approx(0.60)


@pytest.mark.asyncio
async def test_no_envelope_registered_is_noop(backend):
    """No registered cap → reserve passes and settle is harmless."""
    ident = _identity()
    _, ok, rkey = await reserve_envelope(ident, 5.00, backend=backend)
    assert ok is True
    await commit_envelope(rkey, 5.00, 5.00, backend=backend)  # no row → no-op
    assert backend.get(budget_key_from_identity(ident)) is None


@pytest.mark.asyncio
async def test_release_undoes_reservation(backend):
    ident = _identity()
    key = budget_key_from_identity(ident)
    backend.register(key, cap_usd=1.0)

    _, ok, rkey = await reserve_envelope(ident, 0.50, backend=backend)
    assert ok
    assert backend.pending(key) == pytest.approx(0.50)
    await release_envelope(rkey, 0.50, backend=backend)
    assert backend.pending(key) == pytest.approx(0.0)

    # Full cap available again after release.
    _, ok2, _ = await reserve_envelope(ident, 1.00, backend=backend)
    assert ok2 is True


@pytest.mark.asyncio
async def test_off_mode_is_noop_even_when_exhausted(backend, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_ENVELOPE_MODE", "off")
    ident = _identity()
    key = budget_key_from_identity(ident)
    backend.register(key, cap_usd=0.01)
    await backend.try_reserve(key, 0.01)  # exhaust it

    mode, ok, rkey = await reserve_envelope(ident, 100.0, backend=backend)
    assert mode == "off" and ok is True and rkey is None
