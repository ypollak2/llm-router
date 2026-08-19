"""Tier-2 identity: ``agent_id`` resolution + propagation.

The ``agent_id`` field on :class:`llm_router.identity.TurnIdentity` is
optional — when the routed turn isn't part of an agent run (a direct
MCP tool call, a CLI invocation, etc.), it stays None and downstream
consumers (audit detail, log contextvars) simply omit the field.

These tests pin two behaviours:

1. **Env precedence** — ``LLM_ROUTER_AGENT_ID`` env populates
   ``identity.agent_id``; blank/whitespace/unset collapse to None so
   ``if identity.agent_id:`` works as the natural guard.
2. **TurnIdentity dataclass shape** — agent_id is an optional field
   with a default of None so every Tier-1 call site continues to
   work without modification.

See: Tier-2 of the three-tier Phase 2 plan.
"""
from __future__ import annotations

import pytest

from llm_router.identity import (
    LLM_ROUTER_AGENT_ID_ENV,
    LLM_ROUTER_ORG_ID_ENV,
    LLM_ROUTER_USER_EMAIL_ENV,
    LLM_ROUTER_USER_ID_ENV,
    TurnIdentity,
    current_identity,
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch):
    for var in (
        LLM_ROUTER_USER_ID_ENV,
        LLM_ROUTER_USER_EMAIL_ENV,
        LLM_ROUTER_ORG_ID_ENV,
        LLM_ROUTER_AGENT_ID_ENV,
    ):
        monkeypatch.delenv(var, raising=False)


# ── Env precedence ───────────────────────────────────────────────────────────


def test_env_agent_id_is_picked_up(
    clean_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(LLM_ROUTER_USER_ID_ENV, "alice")
    monkeypatch.setenv(LLM_ROUTER_AGENT_ID_ENV, "agno-reviewer")
    ident = current_identity()
    assert ident.agent_id == "agno-reviewer"


def test_no_agent_env_yields_none(
    clean_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default Tier-1 turn: no agent attribution."""
    monkeypatch.setenv(LLM_ROUTER_USER_ID_ENV, "alice")
    ident = current_identity()
    assert ident.agent_id is None


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_blank_agent_env_collapses_to_none(
    clean_env, monkeypatch: pytest.MonkeyPatch, blank: str
) -> None:
    """An empty or whitespace env value must read as 'no agent', not
    as the literal string. Downstream code uses ``if identity.agent_id:``
    and that idiom relies on None / non-empty-string being the only
    two real outcomes.
    """
    monkeypatch.setenv(LLM_ROUTER_USER_ID_ENV, "alice")
    monkeypatch.setenv(LLM_ROUTER_AGENT_ID_ENV, blank)
    ident = current_identity()
    assert ident.agent_id is None


# ── Dataclass shape ──────────────────────────────────────────────────────────


def test_agent_id_defaults_to_none_when_constructed_directly() -> None:
    """Tier-1 call sites construct TurnIdentity(...) without agent_id.
    The dataclass default keeps every existing call site valid.
    """
    ident = TurnIdentity(
        user_id="alice",
        user_email="alice@corp.io",
        org_id="acme",
    )
    assert ident.agent_id is None


def test_agent_id_is_propagated_when_explicit() -> None:
    """When a caller knows the agent id, it should round-trip through
    the dataclass unchanged."""
    ident = TurnIdentity(
        user_id="alice",
        user_email="alice@corp.io",
        org_id="acme",
        agent_id="agno-reviewer",
    )
    assert ident.agent_id == "agno-reviewer"


def test_two_identities_with_different_agent_ids_are_not_equal() -> None:
    """Tier-2: agent_id is part of identity, not just metadata."""
    base = {
        "user_id": "alice",
        "user_email": "alice@corp.io",
        "org_id": "acme",
    }
    assert TurnIdentity(**base, agent_id="a") != TurnIdentity(**base, agent_id="b")
    assert TurnIdentity(**base) != TurnIdentity(**base, agent_id="a")


def test_full_resolver_round_trip(
    clean_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All four env vars together produce the expected TurnIdentity."""
    monkeypatch.setenv(LLM_ROUTER_USER_ID_ENV, "alice")
    monkeypatch.setenv(LLM_ROUTER_USER_EMAIL_ENV, "alice@corp.io")
    monkeypatch.setenv(LLM_ROUTER_ORG_ID_ENV, "acme")
    monkeypatch.setenv(LLM_ROUTER_AGENT_ID_ENV, "agno-reviewer")
    # T1-M1 (Q-P-2 Phase 3a): tenant_id defaults to org_id when
    # LLM_ROUTER_TENANT_ID is unset.
    assert current_identity() == TurnIdentity(
        user_id="alice",
        user_email="alice@corp.io",
        org_id="acme",
        agent_id="agno-reviewer",
        tenant_id="acme",
    )
