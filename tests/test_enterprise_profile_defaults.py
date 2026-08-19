"""G-001 / G-003 / G-012 enterprise-profile default flips.

The ``LLM_ROUTER_PROFILE`` env decides whether the safety-on defaults
apply atomically:

* RBAC ``_resolve_mode``: enterprise + RBAC env unset → ``strict``;
  developer → ``off`` (current behaviour preserved).
* Audit ``_audit_disabled``: enterprise refuses
  ``LLM_ROUTER_AUDIT_DISABLED`` regardless of the env value; developer
  preserves the pre-G-003 env-driven bypass.
* Redaction ``_redaction_enabled``: enterprise + redaction env unset
  → on; developer → off (current behaviour preserved).

The asymmetry on audit (refuses env even when set) is deliberate
and matches the GAP_ANALYSIS G-003 closure criterion.
"""
from __future__ import annotations

import pytest

from llm_router.audit_routing import _audit_disabled
from llm_router.profile import (
    PROFILE_ENV,
    Profile,
    is_enterprise,
    resolve_profile,
)
from llm_router.rbac_routing import _resolve_mode as _rbac_resolve_mode
from llm_router.redaction_routing import _redaction_enabled



@pytest.fixture(autouse=True)
def _clean_env(monkeypatch) -> None:
    """Per-test env isolation. Strip every env this slice touches."""
    for env in (
        PROFILE_ENV,
        "LLM_ROUTER_RBAC_MODE",
        "LLM_ROUTER_AUDIT_DISABLED",
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


# ── 2. RBAC default flip (G-001) ─────────────────────────────────────────────


def test_rbac_default_is_off_in_developer_profile() -> None:
    """Pre-G-001 behaviour preserved when profile unset."""
    assert _rbac_resolve_mode() == "off"


def test_rbac_default_is_strict_in_enterprise_profile(monkeypatch) -> None:
    monkeypatch.setenv(PROFILE_ENV, "enterprise")
    assert _rbac_resolve_mode() == "strict"


def test_rbac_explicit_env_overrides_enterprise_default(monkeypatch) -> None:
    """Operator canary: enterprise + explicit `warn` keeps warn mode."""
    monkeypatch.setenv(PROFILE_ENV, "enterprise")
    monkeypatch.setenv("LLM_ROUTER_RBAC_MODE", "warn")
    assert _rbac_resolve_mode() == "warn"


def test_rbac_explicit_off_overrides_enterprise(monkeypatch) -> None:
    """Operator can explicitly disable RBAC even under enterprise
    (e.g. emergency debug). Logged-and-noisy elsewhere; here we just
    check the resolver respects the explicit choice."""
    monkeypatch.setenv(PROFILE_ENV, "enterprise")
    monkeypatch.setenv("LLM_ROUTER_RBAC_MODE", "off")
    assert _rbac_resolve_mode() == "off"


def test_rbac_explicit_strict_works_without_profile(monkeypatch) -> None:
    """Pre-G-001 explicit opt-in still works."""
    monkeypatch.setenv("LLM_ROUTER_RBAC_MODE", "strict")
    assert _rbac_resolve_mode() == "strict"


# ── 3. Audit-disable refusal (G-003) ─────────────────────────────────────────


def test_audit_disable_env_works_in_developer_profile(monkeypatch) -> None:
    """Pre-G-003 behaviour: env-driven opt-out still works for tests
    and local dev under developer profile."""
    monkeypatch.setenv("LLM_ROUTER_AUDIT_DISABLED", "1")
    assert _audit_disabled() is True


def test_audit_disable_env_refused_in_enterprise(monkeypatch) -> None:
    """G-003 closure: enterprise refuses the env regardless of value."""
    monkeypatch.setenv(PROFILE_ENV, "enterprise")
    for v in ("1", "on", "true", "yes"):
        monkeypatch.setenv("LLM_ROUTER_AUDIT_DISABLED", v)
        assert _audit_disabled() is False, (
            f"audit must not be disable-able under enterprise even with "
            f"LLM_ROUTER_AUDIT_DISABLED={v!r}"
        )


def test_audit_not_disabled_when_neither_set() -> None:
    """Default off-the-shelf install: audit runs."""
    assert _audit_disabled() is False


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


# ── 5. Cross-feature smoke: enterprise profile flips ALL three together ─────


def test_enterprise_profile_flips_all_three_defaults(monkeypatch) -> None:
    """The whole point of LLM_ROUTER_PROFILE: one env flips three
    safety-feature defaults atomically."""
    monkeypatch.setenv(PROFILE_ENV, "enterprise")
    assert _rbac_resolve_mode() == "strict"
    assert _audit_disabled() is False
    assert _redaction_enabled() is True


def test_developer_profile_preserves_all_three_defaults() -> None:
    """Existing installs without LLM_ROUTER_PROFILE see zero behaviour
    change. This test pins that contract."""
    assert _rbac_resolve_mode() == "off"
    assert _audit_disabled() is False  # no env, so not disabled
    assert _redaction_enabled() is False
