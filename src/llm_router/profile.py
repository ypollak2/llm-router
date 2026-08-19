"""G-001 / G-003 / G-012: deployment-profile-driven defaults.

The audit captured a recurring pattern: enterprise-grade safety
features ship *implemented* but *off-by-default*. RBAC defaults to
``off``, audit can be silenced by an env var, redaction is opt-in.
Each was a deliberate choice for the single-developer ``Tier 1``
shape — and each becomes an "operationally a no-op" trap in
production.

This module introduces a single profile knob — ``LLM_ROUTER_PROFILE``
— that flips those defaults atomically:

* ``LLM_ROUTER_PROFILE`` unset or ``developer`` → behaviour unchanged
  vs. pre-G-001/3/12. Every safety-feature default stays where it
  was so existing installs are not broken by an upgrade.
* ``LLM_ROUTER_PROFILE=enterprise`` → safety-on defaults:
  - RBAC defaults to ``strict`` (G-001).
  - ``LLM_ROUTER_AUDIT_DISABLED`` is refused — audit cannot be silenced
    by the env (G-003).
  - Redaction defaults to ``on`` (G-012).

Explicit per-feature env vars still take precedence where they make
sense (operators can opt into ``warn`` mode for a staged RBAC
rollout, or explicitly disable redaction with a documented reason)
— except for ``LLM_ROUTER_AUDIT_DISABLED`` under enterprise, which is
silently dropped. That asymmetry is intentional: audit cannot be
optional for an enterprise deployment.

See: ``docs/audit/post-remediation/GAP_ANALYSIS.md`` G-001, G-003,
G-012, and the NORTHERN_STAR decisions log entry for this slice.
"""
from __future__ import annotations

import os
from enum import Enum


PROFILE_ENV = "LLM_ROUTER_DEPLOYMENT_PROFILE"
# Loop-5 #1 — kill the env-name collision documented in
# tests/test_sec004_status_resource_gate.py. The old name
# (``LLM_ROUTER_PROFILE``) shadowed the routing config's own
# Pydantic-validated ``llm_router_profile`` field (which only accepts
# routing-profile values like ``balanced`` / ``premium`` /
# ``subscription_local``). Reading ``"enterprise"`` from
# ``LLM_ROUTER_PROFILE`` then crashed ``get_config()`` whenever the
# resource handler or any other caller exercised it.
#
# Backward compat: the old env is still read AFTER the new one and
# emits a one-shot stderr deprecation warning. This is a soft
# migration — operators can flip the env name on their schedule.
_LEGACY_PROFILE_ENV = "LLM_ROUTER_PROFILE"

_DEVELOPER_VALUES = {"", "developer", "dev"}
_ENTERPRISE_VALUES = {"enterprise", "prod", "production"}

# Module-level latch so the deprecation warning fires once per process
# instead of on every ``resolve_profile`` call.
_legacy_warning_emitted = False


class Profile(str, Enum):
    DEVELOPER = "developer"
    ENTERPRISE = "enterprise"


def resolve_profile() -> Profile:
    """Return the active deployment profile.

    Resolution order (Loop-5 #1):

    1. ``LLM_ROUTER_DEPLOYMENT_PROFILE`` (the new name).
    2. ``LLM_ROUTER_PROFILE`` (legacy — emits a one-shot deprecation
       warning the first time it's consulted).
    3. Default → ``Profile.DEVELOPER``.

    Unknown values fall back to ``DEVELOPER`` rather than raising —
    a typo in an env var must not be able to silently put the
    deployment into the *less* safe mode. (If we ever flip the default
    to ENTERPRISE, this rule reverses: unknown → DEVELOPER becomes
    unknown → fail-closed-refuse-to-start.)
    """
    raw = (os.environ.get(PROFILE_ENV) or "").strip().lower()
    if not raw:
        legacy = (os.environ.get(_LEGACY_PROFILE_ENV) or "").strip().lower()
        if legacy:
            _maybe_emit_legacy_warning(legacy)
            raw = legacy
    if raw in _ENTERPRISE_VALUES:
        return Profile.ENTERPRISE
    return Profile.DEVELOPER


def _maybe_emit_legacy_warning(value: str) -> None:
    """Print a one-shot deprecation warning when the old env name is
    read. The latch is module-level so a long-running process emits
    the message once, not per resolution."""
    global _legacy_warning_emitted
    if _legacy_warning_emitted:
        return
    _legacy_warning_emitted = True
    import sys
    sys.stderr.write(
        f"[llm_router] DEPRECATED: {_LEGACY_PROFILE_ENV}={value!r} read; "
        f"this env collides with llm_router's routing-config "
        f"{_LEGACY_PROFILE_ENV} field. Rename your env to "
        f"{PROFILE_ENV} (Loop-5 / Item 1). Backward-compat support "
        "will be removed in a future release.\n"
    )


def _reset_legacy_warning_latch() -> None:
    """Test affordance — reset the one-shot latch so the warning
    can be re-observed in subsequent tests. Not part of the public
    API; tests import directly from the module."""
    global _legacy_warning_emitted
    _legacy_warning_emitted = False


def is_enterprise() -> bool:
    """Fast-path accessor used by safety-feature modules."""
    return resolve_profile() == Profile.ENTERPRISE


__all__ = ["PROFILE_ENV", "Profile", "is_enterprise", "resolve_profile"]
