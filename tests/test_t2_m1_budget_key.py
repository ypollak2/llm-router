"""T2-M1 (Track-2 budgets, Medium): ``BudgetKey`` + per-key accounting.

This PR introduces the *shape* of identity-aware budgets without
enforcing per-identity caps (that's T2-M2 parent-child Envelope and
T2-L1 atomic backend). It exists so the rest of the Phase-3 work can
use a stable key + accounting primitive instead of inventing one per
PR.

Pins:

1. **Key shape.** Frozen dataclass; equality is structural; hashable
   so it can be a dict key, a lock identifier, and an audit dimension.
2. **Derivation from identity.** Phase 3a: ``tenant_id`` falls back
   to ``org_id`` (via ``budget_key_from_identity``); ``agent_id``
   stays None when the turn isn't agent-driven.
3. **Per-key reserve / release.** Additive to the provider-scoped
   primitives. Negative costs clamp to zero; over-release floors at
   zero; entries garbage-collect when they hit zero.

See: Docs/audit/post-remediation/GAP_ANALYSIS.md G-002.
"""
from __future__ import annotations

import pytest

from llm_router.budget import (
    pending_spend_for,
    release_for,
    reserve_for,
    reset_pending_spend_for_tests,
)
from llm_router.budget_key import (
    SCOPE_AGENT_SESSION,
    SCOPE_DAILY,
    SCOPE_TURN,
    BudgetKey,
    budget_key_from_identity,
)
from llm_router.identity import TurnIdentity


@pytest.fixture(autouse=True)
def _isolate_pending_dict() -> None:
    """Each test starts with a fresh _pending_spend_by_key dict."""
    reset_pending_spend_for_tests()
    yield
    reset_pending_spend_for_tests()


# ── 1. Key shape ─────────────────────────────────────────────────────────────


def test_budget_key_is_frozen() -> None:
    key = BudgetKey(
        tenant_id="t1", org_id="o1", user_id="alice", agent_id=None
    )
    with pytest.raises((AttributeError, Exception)):
        key.tenant_id = "t2"  # type: ignore[misc]


def test_budget_key_is_hashable_and_usable_as_dict_key() -> None:
    k1 = BudgetKey("t1", "o1", "alice", None)
    k2 = BudgetKey("t1", "o1", "alice", None)
    k3 = BudgetKey("t1", "o1", "bob", None)
    d: dict[BudgetKey, int] = {}
    d[k1] = 1
    d[k3] = 2
    # k2 == k1, so the lookup hits the same slot.
    assert d[k2] == 1
    assert d[k3] == 2
    assert len(d) == 2


def test_budget_key_equality_is_structural() -> None:
    k1 = BudgetKey("t1", "o1", "alice", "agent-7", scope=SCOPE_TURN)
    k2 = BudgetKey("t1", "o1", "alice", "agent-7", scope=SCOPE_TURN)
    assert k1 == k2
    # Differ in scope → different key (a daily cap is a separate
    # budget from a per-turn cap on the same principal).
    k3 = BudgetKey("t1", "o1", "alice", "agent-7", scope=SCOPE_DAILY)
    assert k1 != k3


def test_budget_key_default_scope_is_turn() -> None:
    key = BudgetKey("t1", "o1", "alice", None)
    assert key.scope == SCOPE_TURN


# ── 2. rolls_up_to coarsens one axis ─────────────────────────────────────────


def test_rolls_up_drops_agent_id() -> None:
    k = BudgetKey("t1", "o1", "alice", "agent-7")
    parent = k.rolls_up_to(drop="agent_id")
    assert parent == BudgetKey("t1", "o1", "alice", None)


def test_rolls_up_drops_user_id() -> None:
    k = BudgetKey("t1", "o1", "alice", "agent-7")
    team_key = k.rolls_up_to(drop="user_id")
    assert team_key == BudgetKey("t1", "o1", None, "agent-7")


def test_rolls_up_to_rejects_scope() -> None:
    """The scope is not an identity axis; coarsening it doesn't make
    sense (a per-turn cap and a per-day cap measure different things).
    The helper must refuse to drop it."""
    k = BudgetKey("t1", "o1", "alice", "agent-7")
    with pytest.raises(ValueError, match="cannot drop"):
        k.rolls_up_to(drop="scope")


# ── 3. Derivation from TurnIdentity ──────────────────────────────────────────


def test_budget_key_from_identity_phase_3a_defaults() -> None:
    """Phase 3a: tenant_id resolved by current_identity() equals
    org_id; the derived key reflects that."""
    ident = TurnIdentity(
        user_id="alice",
        user_email="alice@local",
        org_id="acme",
        tenant_id="acme",  # Phase 3a default
        agent_id=None,
    )
    key = budget_key_from_identity(ident)
    assert key.tenant_id == "acme"
    assert key.org_id == "acme"
    assert key.user_id == "alice"
    assert key.agent_id is None
    assert key.scope == SCOPE_TURN


def test_budget_key_from_identity_with_explicit_tenant() -> None:
    """Phase 3b: LLM_ROUTER_TENANT_ID different from org_id."""
    ident = TurnIdentity(
        user_id="alice",
        user_email="alice@local",
        org_id="acme",
        tenant_id="tenant-42",
        agent_id=None,
    )
    key = budget_key_from_identity(ident)
    assert key.tenant_id == "tenant-42"
    assert key.org_id == "acme"


def test_budget_key_from_identity_falls_back_when_tenant_id_none() -> None:
    """A direct-constructed TurnIdentity may have tenant_id=None. The
    derivation falls back to org_id so production keys are never
    missing a tenant axis."""
    ident = TurnIdentity(
        user_id="alice",
        user_email="alice@local",
        org_id="acme",
        tenant_id=None,
        agent_id="agno-reviewer",
    )
    key = budget_key_from_identity(ident)
    assert key.tenant_id == "acme"
    assert key.agent_id == "agno-reviewer"


def test_budget_key_from_identity_alternate_scope() -> None:
    ident = TurnIdentity(
        user_id="alice",
        user_email="alice@local",
        org_id="acme",
        tenant_id="acme",
        agent_id="agno-reviewer",
    )
    key = budget_key_from_identity(ident, scope=SCOPE_AGENT_SESSION)
    assert key.scope == SCOPE_AGENT_SESSION


# ── 4. reserve_for / release_for accounting ──────────────────────────────────


def _k(suffix: str = "") -> BudgetKey:
    return BudgetKey(
        tenant_id="t1",
        org_id="o1",
        user_id="alice" + suffix,
        agent_id=None,
    )


def test_reserve_for_accumulates() -> None:
    k = _k()
    reserve_for(k, 0.001)
    reserve_for(k, 0.005)
    assert pending_spend_for(k) == pytest.approx(0.006)


def test_reserve_for_ignores_non_positive_amounts() -> None:
    k = _k()
    reserve_for(k, 0.0)
    reserve_for(k, -0.01)
    assert pending_spend_for(k) == 0.0


def test_release_for_decreases_pending() -> None:
    k = _k()
    reserve_for(k, 0.01)
    release_for(k, 0.004)
    assert pending_spend_for(k) == pytest.approx(0.006)


def test_release_for_floors_at_zero() -> None:
    """Over-release (release > reserve) does not push the entry
    negative — fail-safe accounting for buggy callers."""
    k = _k()
    reserve_for(k, 0.005)
    release_for(k, 0.05)  # 10x over
    assert pending_spend_for(k) == 0.0


def test_release_for_garbage_collects_zero_entry() -> None:
    """Once a key's reservation reaches zero, the entry is removed
    from the dict so long-running processes don't grow unboundedly
    as identities come and go."""
    from llm_router.budget import _pending_spend_by_key

    k = _k()
    reserve_for(k, 0.01)
    assert k in _pending_spend_by_key
    release_for(k, 0.01)
    assert k not in _pending_spend_by_key


def test_release_for_on_unknown_key_is_noop() -> None:
    """Releasing a key that was never reserved must not raise."""
    k = _k("never-reserved")
    release_for(k, 0.01)  # must not raise
    assert pending_spend_for(k) == 0.0


def test_reserve_for_isolates_keys() -> None:
    """Reserves on key A do not affect key B."""
    a, b = _k("a"), _k("b")
    reserve_for(a, 0.01)
    assert pending_spend_for(a) == pytest.approx(0.01)
    assert pending_spend_for(b) == 0.0


# ── 5. Provider-keyed primitives untouched ───────────────────────────────────


def test_existing_reserve_tokens_still_works() -> None:
    """The pre-T2-M1 provider-scoped accounting is left untouched —
    backwards compat for the 24+ existing call sites."""
    from llm_router.budget import _pending_tokens, release_tokens, reserve_tokens

    pre = _pending_tokens.get("openai", 0)
    reserve_tokens("openai", 500)
    assert _pending_tokens.get("openai", 0) == pre + 500
    release_tokens("openai", 500)
    assert _pending_tokens.get("openai", 0) == pre
