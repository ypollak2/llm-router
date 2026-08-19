"""T3-XL1 (Track-3 agent safety, XL): agent-aware routing policy layer.

A ``RoutingPolicy`` rides on an ``AgentSession`` and biases routing for the
duration of that session:

* ``preferred_providers`` — provider names in priority order. The router
  reorders the candidate list to try these first.
* ``preferred_models_by_classification`` — per-classification model
  priority lists. Same idea, finer grain.
* ``max_cost_per_turn_usd`` — per-turn cost cap, distinct from the
  session-level budget. Refuses any single candidate whose worst-case
  cost would breach it.
* ``max_temperature`` — clamp on sampling temperature.
* ``inherits_from`` — explicit policy chain, merged child-over-parent.

Parent inheritance also happens implicitly via the session chain: when a
session's ``parent_session_id`` is set, ``SessionStore.effective_policy``
walks the chain from root to leaf and merges policies so the leaf wins on
conflicts.

See: Docs/audit/post-remediation/GAP_ANALYSIS.md G-008.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from llm_router.agents.base import AgentRoutingPolicy, AgentSession, SessionState
from llm_router.agents.session import SessionStore


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(db_path=tmp_path / "s.db")


# ── 1. Dataclass shape ──────────────────────────────────────────────────────


def test_policy_is_frozen() -> None:
    """Policy is a frozen dataclass — mutating raises FrozenInstanceError."""
    p = AgentRoutingPolicy(preferred_providers=("anthropic",))
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.preferred_providers = ("openai",)  # type: ignore[misc]


def test_policy_defaults_are_none_or_empty() -> None:
    """Bare construction is all-defaults; no field is required."""
    p = AgentRoutingPolicy()
    assert p.preferred_providers == ()
    assert p.preferred_models_by_classification == {}
    assert p.max_cost_per_turn_usd is None
    assert p.max_temperature is None
    assert p.inherits_from is None


def test_policy_construction_with_all_fields() -> None:
    p = AgentRoutingPolicy(
        preferred_providers=("anthropic", "openai"),
        preferred_models_by_classification={
            "simple": ("haiku-4-5",),
            "complex": ("opus-4-7",),
        },
        max_cost_per_turn_usd=0.05,
        max_temperature=0.7,
    )
    assert p.preferred_providers == ("anthropic", "openai")
    assert p.preferred_models_by_classification["simple"] == ("haiku-4-5",)
    assert p.max_cost_per_turn_usd == pytest.approx(0.05)
    assert p.max_temperature == pytest.approx(0.7)


# ── 2. merged_with: child overrides parent ──────────────────────────────────


def test_merge_child_overrides_parent_scalar_fields() -> None:
    """For scalar fields (None | value), the child wins when set."""
    parent = AgentRoutingPolicy(
        max_cost_per_turn_usd=1.00, max_temperature=0.5
    )
    child = AgentRoutingPolicy(max_cost_per_turn_usd=0.10)
    merged = child.merged_with(parent)
    assert merged.max_cost_per_turn_usd == pytest.approx(0.10)  # child wins
    # parent fills in unset child fields
    assert merged.max_temperature == pytest.approx(0.5)


def test_merge_child_preserves_unset_parent_fields_as_none() -> None:
    """If neither parent nor child sets a field, merged is None."""
    parent = AgentRoutingPolicy()
    child = AgentRoutingPolicy()
    merged = child.merged_with(parent)
    assert merged.max_cost_per_turn_usd is None
    assert merged.max_temperature is None


def test_merge_child_overrides_parent_provider_ordering() -> None:
    """Child's preferred_providers wins outright (no union/merge)."""
    parent = AgentRoutingPolicy(preferred_providers=("anthropic",))
    child = AgentRoutingPolicy(preferred_providers=("openai", "gemini"))
    merged = child.merged_with(parent)
    assert merged.preferred_providers == ("openai", "gemini")


def test_merge_child_uses_parent_providers_when_child_empty() -> None:
    """When the child leaves providers empty, parent's order survives."""
    parent = AgentRoutingPolicy(preferred_providers=("anthropic", "openai"))
    child = AgentRoutingPolicy()
    merged = child.merged_with(parent)
    assert merged.preferred_providers == ("anthropic", "openai")


def test_merge_classification_dict_per_key_child_wins() -> None:
    """Classification dict merges key-by-key; child's keys override parent's."""
    parent = AgentRoutingPolicy(
        preferred_models_by_classification={
            "simple": ("haiku-4-5",),
            "complex": ("opus-4-7",),
        }
    )
    child = AgentRoutingPolicy(
        preferred_models_by_classification={"simple": ("gemini-flash",)}
    )
    merged = child.merged_with(parent)
    assert merged.preferred_models_by_classification["simple"] == ("gemini-flash",)
    # parent's complex survives because child didn't set it
    assert merged.preferred_models_by_classification["complex"] == ("opus-4-7",)


# ── 3. inherits_from explicit chain ─────────────────────────────────────────


def test_inherits_from_chain_resolves_to_merged_policy() -> None:
    """A policy with inherits_from set merges as a chain when resolved."""
    grandparent = AgentRoutingPolicy(max_cost_per_turn_usd=10.0)
    parent = AgentRoutingPolicy(
        max_cost_per_turn_usd=1.0, inherits_from=grandparent
    )
    child = AgentRoutingPolicy(
        preferred_providers=("anthropic",), inherits_from=parent
    )
    resolved = child.resolved()
    assert resolved.preferred_providers == ("anthropic",)
    assert resolved.max_cost_per_turn_usd == pytest.approx(1.0)  # parent's


def test_resolved_with_no_inherits_returns_self_value() -> None:
    """Policy with no inherits_from resolves to itself unchanged."""
    p = AgentRoutingPolicy(max_cost_per_turn_usd=0.25)
    resolved = p.resolved()
    assert resolved.max_cost_per_turn_usd == pytest.approx(0.25)


# ── 4. AgentSession carries the policy ──────────────────────────────────────


def test_session_routing_policy_defaults_to_none() -> None:
    """T3-M3 baseline: AgentSession with no policy works unchanged."""
    s = AgentSession(
        session_id="x",
        agent_id="a",
        started_at=0.0,
        completed_at=None,
        parent_session_id=None,
        budget_cap_usd=1.0,
        consumed_usd=0.0,
        step_count=0,
        state=SessionState.ACTIVE,
    )
    assert s.routing_policy is None


# ── 5. SessionStore persistence + session-walk inheritance ──────────────────


def test_store_create_persists_routing_policy(store: SessionStore) -> None:
    """Policy stored on create() round-trips through get()."""
    policy = AgentRoutingPolicy(
        preferred_providers=("anthropic", "openai"),
        max_cost_per_turn_usd=0.20,
    )
    sess = store.create(agent_id="a", budget_usd=1.0, routing_policy=policy)
    reloaded = store.get(sess.session_id)
    assert reloaded.routing_policy is not None
    assert reloaded.routing_policy.preferred_providers == ("anthropic", "openai")
    assert reloaded.routing_policy.max_cost_per_turn_usd == pytest.approx(0.20)


def test_store_create_without_policy_round_trips_none(store: SessionStore) -> None:
    """Backwards compat: no policy passed → None persisted → None returned."""
    sess = store.create(agent_id="a", budget_usd=1.0)
    reloaded = store.get(sess.session_id)
    assert reloaded.routing_policy is None


def test_effective_policy_walks_parent_chain(store: SessionStore) -> None:
    """effective_policy walks parent_session_id chain and merges
    root → leaf so the deepest child wins on conflicts."""
    root_policy = AgentRoutingPolicy(
        preferred_providers=("anthropic",),
        max_cost_per_turn_usd=1.0,
    )
    root = store.create(
        agent_id="r", budget_usd=10.0, routing_policy=root_policy
    )
    child_policy = AgentRoutingPolicy(max_cost_per_turn_usd=0.10)
    child = store.create(
        agent_id="c",
        budget_usd=10.0,
        parent_session_id=root.session_id,
        routing_policy=child_policy,
    )
    eff = store.effective_policy(child.session_id)
    assert eff is not None
    assert eff.preferred_providers == ("anthropic",)  # inherited from root
    assert eff.max_cost_per_turn_usd == pytest.approx(0.10)  # child wins


def test_effective_policy_returns_none_when_chain_empty(store: SessionStore) -> None:
    """No policies anywhere in the chain → effective is None."""
    root = store.create(agent_id="r", budget_usd=1.0)
    child = store.create(
        agent_id="c", budget_usd=1.0, parent_session_id=root.session_id
    )
    assert store.effective_policy(child.session_id) is None


def test_effective_policy_root_only(store: SessionStore) -> None:
    """Policy on root, child has none → child inherits root's policy."""
    root_policy = AgentRoutingPolicy(preferred_providers=("anthropic",))
    root = store.create(
        agent_id="r", budget_usd=1.0, routing_policy=root_policy
    )
    child = store.create(
        agent_id="c", budget_usd=1.0, parent_session_id=root.session_id
    )
    eff = store.effective_policy(child.session_id)
    assert eff is not None
    assert eff.preferred_providers == ("anthropic",)
