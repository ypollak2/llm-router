"""T2-M3 (Track-2 budgets, Medium): soft / hard tiers on BudgetEnvelope.

Builds on T2-M2 (parent-child propagation) by adding a soft-tier
alerting threshold that operators can poll via ``tier_state(key)``.

Pins:

1. **Register-time validation.** ``soft_cap_usd`` must be positive and
   strictly less than ``cap_usd`` — equal or larger is rejected.
2. **Soft cap is alerting, not enforcement.** A reservation that
   crosses the soft cap succeeds; only the hard cap blocks. This is
   the headline contract — operators dashboard soft-breach as a
   leading indicator, not a circuit breaker.
3. **Rising-edge state.** ``tier_state.soft_breached`` flips True once
   consumed+pending ≥ soft cap and back to False on release that
   drops total back below.
4. **No-soft-cap backwards compatibility.** Envelopes without a soft
   cap report ``soft_breached=False`` always; T2-M2 callers see no
   behaviour change.
5. **Parent-chain propagation.** A child reservation that breaches
   the *parent's* soft cap flips the parent's ``soft_breached``.
6. **Snapshot shape.** ``tier_state`` returns a stable dict shape for
   dashboards, including the "no envelope" case.

Forecast / predictive tiers (third tier on the roadmap) are deferred
to T2-L1 — they need cross-instance burn-rate data that the
in-process manager can't compute correctly.

See: Docs/audit/post-remediation/GAP_ANALYSIS.md G-002.
"""
from __future__ import annotations

import pytest

from llm_router.budget_envelope import BudgetEnvelopeManager, reset_manager_for_tests
from llm_router.budget_key import BudgetKey


@pytest.fixture(autouse=True)
def _isolate_manager():
    reset_manager_for_tests()
    yield
    reset_manager_for_tests()


def _k(suffix: str = "", agent_id: str | None = None) -> BudgetKey:
    return BudgetKey(
        tenant_id="t1",
        org_id="o1",
        user_id="alice" + suffix,
        agent_id=agent_id,
    )


# ── 1. Register-time validation ───────────────────────────────────────────────


def test_register_rejects_soft_cap_equal_to_hard_cap() -> None:
    """soft == hard would never fire before the hard cap — nonsensical."""
    m = BudgetEnvelopeManager()
    with pytest.raises(ValueError, match="strictly less"):
        m.register(_k(), cap_usd=10.0, soft_cap_usd=10.0)


def test_register_rejects_soft_cap_greater_than_hard_cap() -> None:
    m = BudgetEnvelopeManager()
    with pytest.raises(ValueError, match="strictly less"):
        m.register(_k(), cap_usd=10.0, soft_cap_usd=15.0)


def test_register_rejects_non_positive_soft_cap() -> None:
    m = BudgetEnvelopeManager()
    with pytest.raises(ValueError, match="soft_cap_usd must be positive"):
        m.register(_k(), cap_usd=10.0, soft_cap_usd=0.0)
    with pytest.raises(ValueError, match="soft_cap_usd must be positive"):
        m.register(_k(), cap_usd=10.0, soft_cap_usd=-1.0)


def test_register_accepts_valid_soft_cap() -> None:
    m = BudgetEnvelopeManager()
    env = m.register(_k(), cap_usd=10.0, soft_cap_usd=8.0)
    assert env.cap_usd == pytest.approx(10.0)
    assert env.soft_cap_usd == pytest.approx(8.0)


# ── 2. Soft cap is alerting, not enforcement ─────────────────────────────────


@pytest.mark.asyncio
async def test_soft_cap_does_not_block_reservation() -> None:
    """Crossing the soft cap succeeds — the headline contract.
    A soft tier is observability, not a circuit breaker."""
    m = BudgetEnvelopeManager()
    m.register(_k(), cap_usd=10.0, soft_cap_usd=5.0)
    # 6 > soft cap of 5 but well under hard cap of 10.
    assert await m.try_reserve(_k(), 6.0) is True
    assert m.pending(_k()) == pytest.approx(6.0)


@pytest.mark.asyncio
async def test_hard_cap_still_blocks_above_soft_cap() -> None:
    """Hard-cap enforcement remains exactly as in T2-M2."""
    m = BudgetEnvelopeManager()
    m.register(_k(), cap_usd=10.0, soft_cap_usd=5.0)
    assert await m.try_reserve(_k(), 11.0) is False


# ── 3. Rising-edge state ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_soft_breached_flips_on_reservation_crossing_threshold() -> None:
    m = BudgetEnvelopeManager()
    m.register(_k(), cap_usd=10.0, soft_cap_usd=5.0)
    # Under threshold → not breached.
    await m.try_reserve(_k(), 3.0)
    assert m.tier_state(_k())["soft_breached"] is False
    # Crosses 5 → breached.
    await m.try_reserve(_k(), 3.0)  # total 6
    assert m.tier_state(_k())["soft_breached"] is True


@pytest.mark.asyncio
async def test_soft_breached_resets_after_release() -> None:
    m = BudgetEnvelopeManager()
    m.register(_k(), cap_usd=10.0, soft_cap_usd=5.0)
    await m.try_reserve(_k(), 6.0)
    assert m.tier_state(_k())["soft_breached"] is True
    await m.release(_k(), 6.0)
    assert m.tier_state(_k())["soft_breached"] is False


@pytest.mark.asyncio
async def test_soft_breached_persists_after_commit() -> None:
    """Commit moves pending to consumed; total stays at the same level
    so the soft-breach state must persist (not reset)."""
    m = BudgetEnvelopeManager()
    m.register(_k(), cap_usd=10.0, soft_cap_usd=5.0)
    await m.try_reserve(_k(), 6.0)
    await m.commit(_k(), 6.0)
    assert m.tier_state(_k())["soft_breached"] is True
    assert m.consumed(_k()) == pytest.approx(6.0)
    assert m.pending(_k()) == 0.0


@pytest.mark.asyncio
async def test_soft_breach_log_is_rising_edge_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The structured warning fires once when crossing the threshold,
    not on every subsequent reserve above it. Operators wire this to
    alerting; an N-spam-per-reserve hose would defeat the purpose.

    We monkeypatch the module-level ``log.warning`` to capture calls
    directly — this avoids depending on whether structlog routes to
    stdout or stderr (stdlib StreamHandler defaults to stderr; some
    pytest configs redirect). The behaviour we care about is the
    rising-edge dedup, not the transport.
    """
    from llm_router import budget_envelope as be_mod

    calls: list[str] = []

    def _capture(event: str, **kwargs: object) -> None:
        calls.append(event)

    monkeypatch.setattr(be_mod.log, "warning", _capture)

    m = BudgetEnvelopeManager()
    m.register(_k(), cap_usd=10.0, soft_cap_usd=5.0)

    # Cross the threshold on the second reservation.
    await m.try_reserve(_k(), 3.0)
    await m.try_reserve(_k(), 3.0)  # total 6 → breached
    # Reserve again above the threshold — should NOT re-emit.
    await m.try_reserve(_k(), 1.0)  # total 7

    assert calls.count("budget_soft_cap_breached") == 1


# ── 4. No-soft-cap backwards compatibility ───────────────────────────────────


@pytest.mark.asyncio
async def test_no_soft_cap_means_never_breached() -> None:
    """T2-M2 callers that don't set a soft cap must see no behaviour
    change — soft_breached is False even at the hard cap."""
    m = BudgetEnvelopeManager()
    m.register(_k(), cap_usd=10.0)  # no soft cap
    await m.try_reserve(_k(), 10.0)
    state = m.tier_state(_k())
    assert state["soft_breached"] is False
    assert state["soft_cap_usd"] is None


# ── 5. Parent-chain propagation ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_child_reservation_breaches_parent_soft_cap() -> None:
    """A child reserve that crosses the parent's soft cap must flip
    the parent's soft_breached — matches T2-M2's chain semantics.
    Child cap is intentionally loose so the parent is the binding
    soft-tier constraint."""
    m = BudgetEnvelopeManager()
    parent = _k()
    child = _k(agent_id="agent-7")
    m.register(parent, cap_usd=10.0, soft_cap_usd=5.0)
    m.register(child, cap_usd=20.0, parents=(parent,))

    # Under parent soft cap → no breach.
    await m.try_reserve(child, 3.0)
    assert m.tier_state(parent)["soft_breached"] is False
    assert m.tier_state(child)["soft_breached"] is False
    # Crosses parent soft cap → parent flips; child has no soft cap.
    await m.try_reserve(child, 3.0)  # parent total 6
    assert m.tier_state(parent)["soft_breached"] is True
    assert m.tier_state(child)["soft_breached"] is False


# ── 6. Snapshot shape ────────────────────────────────────────────────────────


def test_tier_state_unregistered_key_returns_no_enforcement_view() -> None:
    """Stable shape so dashboards don't crash on a key llm_router hasn't
    registered yet."""
    m = BudgetEnvelopeManager()
    state = m.tier_state(_k("orphan"))
    assert state == {
        "cap_usd": None,
        "soft_cap_usd": None,
        "consumed_usd": 0.0,
        "pending_usd": 0.0,
        "remaining_usd": float("inf"),
        "usage_pct": None,
        "soft_breached": False,
    }


@pytest.mark.asyncio
async def test_tier_state_reports_usage_pct_and_remaining() -> None:
    m = BudgetEnvelopeManager()
    m.register(_k(), cap_usd=10.0, soft_cap_usd=5.0)
    await m.try_reserve(_k(), 4.0)
    state = m.tier_state(_k())
    assert state["cap_usd"] == pytest.approx(10.0)
    assert state["soft_cap_usd"] == pytest.approx(5.0)
    assert state["consumed_usd"] == 0.0
    assert state["pending_usd"] == pytest.approx(4.0)
    assert state["remaining_usd"] == pytest.approx(6.0)
    assert state["usage_pct"] == pytest.approx(0.4)
    assert state["soft_breached"] is False
