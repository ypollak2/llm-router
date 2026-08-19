"""Tier-1 identity resolution: ``current_identity`` precedence + fallbacks.

The routing path calls :func:`llm_router.identity.current_identity` on every
turn. It must:

* Honour ``LLM_ROUTER_USER_ID`` / ``LLM_ROUTER_USER_EMAIL`` / ``LLM_ROUTER_ORG_ID``
  when they're set.
* Fall back through ``getpass.getuser()`` to a sentinel ``"unknown"``
  when env is empty.
* Never raise — an unset environment cannot be allowed to break a
  routed turn.

See: Tier-1 of the three-tier Phase 2 plan (closes the implicit
prerequisite for INV-010 attribution).
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from llm_router.identity import (
    LLM_ROUTER_ORG_ID_ENV,
    LLM_ROUTER_USER_EMAIL_ENV,
    LLM_ROUTER_USER_ID_ENV,
    DEFAULT_ORG_ID,
    TurnIdentity,
    current_identity,
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch):
    """Strip every LLM_ROUTER_* env var the resolver looks at."""
    for var in (LLM_ROUTER_USER_ID_ENV, LLM_ROUTER_USER_EMAIL_ENV, LLM_ROUTER_ORG_ID_ENV):
        monkeypatch.delenv(var, raising=False)


# ── Env precedence ───────────────────────────────────────────────────────────


def test_env_user_id_wins(clean_env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LLM_ROUTER_USER_ID_ENV, "alice@corp")
    ident = current_identity()
    assert ident.user_id == "alice@corp"


def test_env_user_email_wins(clean_env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LLM_ROUTER_USER_ID_ENV, "alice")
    monkeypatch.setenv(LLM_ROUTER_USER_EMAIL_ENV, "alice@corp.io")
    ident = current_identity()
    assert ident.user_email == "alice@corp.io"


def test_env_org_id_wins(clean_env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LLM_ROUTER_USER_ID_ENV, "alice")
    monkeypatch.setenv(LLM_ROUTER_ORG_ID_ENV, "acme")
    ident = current_identity()
    assert ident.org_id == "acme"


def test_all_env_vars_set(clean_env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LLM_ROUTER_USER_ID_ENV, "alice")
    monkeypatch.setenv(LLM_ROUTER_USER_EMAIL_ENV, "alice@corp.io")
    monkeypatch.setenv(LLM_ROUTER_ORG_ID_ENV, "acme")
    # T1-M1 (Q-P-2 Phase 3a): tenant_id defaults to org_id when
    # LLM_ROUTER_TENANT_ID is unset, so the resolver returns "acme" here.
    assert current_identity() == TurnIdentity(
        user_id="alice",
        user_email="alice@corp.io",
        org_id="acme",
        tenant_id="acme",
    )


# ── Empty / whitespace env values fall through to defaults ───────────────────


@pytest.mark.parametrize("value", ["", "   ", "\t"])
def test_blank_user_id_falls_back_to_getuser(
    clean_env, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(LLM_ROUTER_USER_ID_ENV, value)
    with patch("llm_router.identity.getpass.getuser", return_value="bob"):
        ident = current_identity()
    assert ident.user_id == "bob"


@pytest.mark.parametrize("value", ["", "  "])
def test_blank_user_email_synthesises_local(
    clean_env, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(LLM_ROUTER_USER_ID_ENV, "alice")
    monkeypatch.setenv(LLM_ROUTER_USER_EMAIL_ENV, value)
    ident = current_identity()
    assert ident.user_email == "alice@local"


def test_blank_org_id_falls_back_to_default(
    clean_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(LLM_ROUTER_USER_ID_ENV, "alice")
    monkeypatch.setenv(LLM_ROUTER_ORG_ID_ENV, "  ")
    ident = current_identity()
    assert ident.org_id == DEFAULT_ORG_ID


# ── Total-fallback path ──────────────────────────────────────────────────────


def test_unset_env_uses_getuser(clean_env) -> None:
    """No env, no monkeypatch — relies on ``getpass.getuser()``.

    The test machine always has a user, so getuser() succeeds. The
    important assertion is that *something non-empty* comes back AND
    derived fields are populated.
    """
    ident = current_identity()
    assert ident.user_id  # non-empty
    assert ident.user_email == f"{ident.user_id}@local"
    assert ident.org_id == DEFAULT_ORG_ID


def test_getuser_exception_falls_through_to_unknown(
    clean_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tty-less / restricted-container scenarios: getuser raises.

    The resolver must absorb the exception and return ``"unknown"`` —
    routing the turn is more important than precise attribution when
    the environment is degraded.
    """
    monkeypatch.delenv(LLM_ROUTER_USER_ID_ENV, raising=False)
    with patch("llm_router.identity.getpass.getuser", side_effect=OSError("tty-less")):
        ident = current_identity()
    assert ident.user_id == "unknown"
    assert ident.user_email == "unknown@local"


def test_resolver_never_raises(clean_env) -> None:
    """Resilience meta-test: current_identity() may not propagate."""
    with patch("llm_router.identity.getpass.getuser", side_effect=RuntimeError("boom")):
        ident = current_identity()  # would explode if not caught
    assert isinstance(ident, TurnIdentity)


# ── Dataclass invariants ─────────────────────────────────────────────────────


def test_turn_identity_is_frozen(clean_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Identity must be immutable so it can be shared across the routing call."""
    monkeypatch.setenv(LLM_ROUTER_USER_ID_ENV, "alice")
    ident = current_identity()
    with pytest.raises((AttributeError, Exception)):
        ident.user_id = "bob"  # type: ignore[misc]
