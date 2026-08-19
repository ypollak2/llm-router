"""G-016 — forecast tier strict-by-default under enterprise profile.

Pre-fix the predictive-budget forecast tier defaulted to ``off``,
matching the original "opt-in safety feature" pattern the audit
specifically flagged (G-001, G-003, G-012, G-016). G-016 closes
this by switching the default to ``strict`` whenever
``LLM_ROUTER_PROFILE=enterprise`` is set, mirroring slice-3.

Developer profile (or no profile) preserves the pre-G-016 behaviour
— ``off`` by default — so existing developer workstation installs
are not surprised by an enforcement flip on upgrade.

Explicit ``LLM_ROUTER_BUDGET_FORECAST_MODE`` always wins (covers a
documented enterprise opt-out to ``warn`` during canary rollouts).

Unrecognised env values fall back to ``off`` defensively — a typo
must NOT silently flip strict on under any profile.
"""
from __future__ import annotations

import pytest

from llm_router.budget_backend import _forecast_mode



@pytest.fixture(autouse=True)
def _clean_env(monkeypatch) -> None:
    for env in (
        "LLM_ROUTER_PROFILE",
        "LLM_ROUTER_BUDGET_FORECAST_MODE",
    ):
        monkeypatch.delenv(env, raising=False)


# ── 1. Developer profile (default) — pre-G-016 behaviour ──────────────────


def test_developer_profile_defaults_off() -> None:
    """Pre-G-016 baseline: no profile, no env → off. Existing
    installs see zero behaviour change on upgrade."""
    assert _forecast_mode() == "off"


def test_developer_profile_explicit_off(monkeypatch) -> None:
    monkeypatch.setenv("LLM_ROUTER_PROFILE", "developer")
    assert _forecast_mode() == "off"


# ── 2. Enterprise profile — default flips to strict ───────────────────────


def test_enterprise_profile_defaults_strict(monkeypatch) -> None:
    monkeypatch.setenv("LLM_ROUTER_PROFILE", "enterprise")
    assert _forecast_mode() == "strict"


def test_enterprise_aliases_flip_too(monkeypatch) -> None:
    """Profile aliases (``prod`` / ``production``) flip the default
    the same way."""
    for value in ("enterprise", "ENTERPRISE", "prod", "production"):
        monkeypatch.setenv("LLM_ROUTER_PROFILE", value)
        assert _forecast_mode() == "strict", (
            f"profile value {value!r} should flip default to strict"
        )


# ── 3. Explicit env always wins ──────────────────────────────────────────


def test_explicit_warn_overrides_enterprise_default(monkeypatch) -> None:
    """Documented canary path: enterprise + explicit ``warn`` keeps
    audit-only enforcement during a staged rollout."""
    monkeypatch.setenv("LLM_ROUTER_PROFILE", "enterprise")
    monkeypatch.setenv("LLM_ROUTER_BUDGET_FORECAST_MODE", "warn")
    assert _forecast_mode() == "warn"


def test_explicit_off_overrides_enterprise_default(monkeypatch) -> None:
    """Operator can explicitly disable forecast even under enterprise
    (e.g. emergency debug). The asymmetric audit treatment (G-003 —
    audit cannot be disabled under enterprise) does NOT extend to
    forecast because the forecast tier is a soft predictor not a
    hard accountability gate."""
    monkeypatch.setenv("LLM_ROUTER_PROFILE", "enterprise")
    monkeypatch.setenv("LLM_ROUTER_BUDGET_FORECAST_MODE", "off")
    assert _forecast_mode() == "off"


def test_explicit_strict_works_without_profile(monkeypatch) -> None:
    """Pre-G-016 explicit opt-in still works under developer."""
    monkeypatch.setenv("LLM_ROUTER_BUDGET_FORECAST_MODE", "strict")
    assert _forecast_mode() == "strict"


# ── 4. Typo guard — unknown values default to off under ANY profile ───────


def test_typo_falls_back_to_off_under_developer(monkeypatch) -> None:
    monkeypatch.setenv("LLM_ROUTER_BUDGET_FORECAST_MODE", "stricter")
    assert _forecast_mode() == "off"


def test_typo_falls_back_to_off_under_enterprise(monkeypatch) -> None:
    """Critical defensive: a typo in the env must NOT silently
    enable strict either. Falls back to ``off`` so the typo is
    surfaced operationally rather than masquerading as enforcement."""
    monkeypatch.setenv("LLM_ROUTER_PROFILE", "enterprise")
    monkeypatch.setenv("LLM_ROUTER_BUDGET_FORECAST_MODE", "stricter")
    assert _forecast_mode() == "off"


def test_blank_value_uses_profile_default(monkeypatch) -> None:
    """Empty / whitespace env is treated as unset (covers shell
    config gotchas where an env exports an empty string)."""
    monkeypatch.setenv("LLM_ROUTER_PROFILE", "enterprise")
    monkeypatch.setenv("LLM_ROUTER_BUDGET_FORECAST_MODE", "")
    assert _forecast_mode() == "strict"


# ── 5. Cross-profile parity with slice 3 ─────────────────────────────────


def test_enterprise_profile_flips_forecast_alongside_other_safety_defaults(
    monkeypatch,
) -> None:
    """Smoke: setting LLM_ROUTER_PROFILE=enterprise flips the forecast
    default the same way it flips RBAC/audit/redaction (slice 3).
    One env, four safety-feature defaults aligned."""
    from llm_router.audit_routing import _audit_disabled
    from llm_router.rbac_routing import _resolve_mode as _rbac_resolve_mode
    from llm_router.redaction_routing import _redaction_enabled

    # All envs unset, profile=enterprise.
    monkeypatch.setenv("LLM_ROUTER_PROFILE", "enterprise")
    monkeypatch.delenv("LLM_ROUTER_RBAC_MODE", raising=False)
    monkeypatch.delenv("LLM_ROUTER_AUDIT_DISABLED", raising=False)
    monkeypatch.delenv("LLM_ROUTER_REDACTION", raising=False)
    monkeypatch.delenv("LLM_ROUTER_BUDGET_FORECAST_MODE", raising=False)

    assert _rbac_resolve_mode() == "strict"      # G-001
    assert _audit_disabled() is False             # G-003
    assert _redaction_enabled() is True           # G-012
    assert _forecast_mode() == "strict"           # G-016 (new)
