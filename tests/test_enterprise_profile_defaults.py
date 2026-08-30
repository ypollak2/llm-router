"""G-012 enterprise-profile default flip (redaction).

The ``LLM_ROUTER_PROFILE`` env decides whether the safety-on defaults
apply: Redaction ``_redaction_enabled``: enterprise + redaction env unset
→ on; developer → off (current behaviour preserved).

Historically this file also pinned the matching flips for G-001 (RBAC
``_resolve_mode``, in ``rbac_routing.py``) and G-003 (audit
``_audit_disabled``, in ``audit_routing.py``). Both modules depended
entirely on ``llm_router.enterprise``, which this distribution never
shipped — RBAC strict/warn mode crashed every routed call (GH#68) and the
routing audit trail never actually wrote a row (GH#71) — and were removed
rather than fixed. Their sections below were removed along with them
rather than left testing code that no longer exists.
"""
from __future__ import annotations

import pytest

from llm_router.profile import (
    PROFILE_ENV,
    Profile,
    is_enterprise,
    resolve_profile,
)
from llm_router.redaction_routing import _redaction_enabled



@pytest.fixture(autouse=True)
def _clean_env(monkeypatch) -> None:
    """Per-test env isolation. Strip every env this slice touches."""
    for env in (
        PROFILE_ENV,
        "LLM_ROUTER_REDACTION",
    ):
        monkeypatch.delenv(env, raising=False)


# ── 1. Profile resolution ────────────────────────────────────────────────────


def test_profile_unset_defaults_to_developer() -> None:
    assert resolve_profile() == Profile.DEVELOPER
    assert is_enterprise() is False


def test_profile_developer_explicit(monkeypatch) -> None:
    monkeypatch.setenv(PROFILE_ENV, "developer")
    assert resolve_profile() == Profile.DEVELOPER


def test_profile_enterprise_explicit(monkeypatch) -> None:
    monkeypatch.setenv(PROFILE_ENV, "enterprise")
    assert resolve_profile() == Profile.ENTERPRISE
    assert is_enterprise() is True


def test_profile_aliases_resolve(monkeypatch) -> None:
    """`prod` / `production` alias to enterprise; `dev` to developer."""
    for v in ("enterprise", "ENTERPRISE", "prod", "production"):
        monkeypatch.setenv(PROFILE_ENV, v)
        assert resolve_profile() == Profile.ENTERPRISE
    for v in ("developer", "dev"):
        monkeypatch.setenv(PROFILE_ENV, v)
        assert resolve_profile() == Profile.DEVELOPER


def test_unknown_profile_falls_back_to_developer(monkeypatch) -> None:
    """Typo in the env must NOT silently put us into a *less* safe
    mode. Defensive default: unknown → developer (the current default)."""
    monkeypatch.setenv(PROFILE_ENV, "entrprise")  # typo
    assert resolve_profile() == Profile.DEVELOPER


# ── 4. Redaction default flip (G-012) ────────────────────────────────────────


def test_redaction_default_is_off_in_developer_profile() -> None:
    assert _redaction_enabled() is False


def test_redaction_default_is_on_in_enterprise_profile(monkeypatch) -> None:
    monkeypatch.setenv(PROFILE_ENV, "enterprise")
    assert _redaction_enabled() is True


def test_redaction_explicit_off_overrides_enterprise(monkeypatch) -> None:
    """Documented operator opt-out remains possible — sometimes
    redaction has a high false-positive rate for a specific workload
    and the operator turns it off knowingly."""
    monkeypatch.setenv(PROFILE_ENV, "enterprise")
    monkeypatch.setenv("LLM_ROUTER_REDACTION", "off")
    assert _redaction_enabled() is False


def test_redaction_explicit_on_works_without_profile(monkeypatch) -> None:
    """Pre-G-012 explicit opt-in still works."""
    monkeypatch.setenv("LLM_ROUTER_REDACTION", "on")
    assert _redaction_enabled() is True
