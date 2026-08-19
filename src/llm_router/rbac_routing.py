"""T1-M2: Permission.ROUTE_PROMPT enforcement at the routing chokepoint.

The 2026-06 audit's anchor finding (INV-010 / G-001) was that
``enterprise/rbac.py`` shipped fully-formed but had zero callers from
``router.route_and_call``. Anyone with env access could route as any
identity.

This module bridges the Tier-1 ``TurnIdentity`` (env-resolved,
single-org-per-instance Phase 3a) to the existing
``enterprise.rbac.has_permission`` machinery, with three modes:

* **off** — no enforcement (default). Preserves Tier-1 backwards compat.
  Phase 3a operators who haven't issued real ``Identity`` objects yet
  must opt in deliberately; mandatory enforcement requires an
  IdentityStore + token issuance that is a Phase 3b/Tier 3 effort.

* **warn** — log + audit on missing permission, but ALLOW the turn.
  Designed for the dual-write window: ship the check, observe which
  call sites fail, fix them BEFORE flipping to strict.

* **strict** — raise ``PermissionDenied`` BEFORE any provider is
  contacted; write a denied audit row; the caller pays nothing for
  the deny. This is the target steady state for any deployment that
  has wired real identities.

The mode is set via ``LLM_ROUTER_RBAC_MODE``. Affirmative values for
strict: ``strict`` (and the historical ``hard``). Affirmative values
for warn: ``warn`` (and ``soft`` / ``shadow``). Anything else
(including unset, empty, ``off``) is treated as ``off``.

See: Docs/audit/post-remediation/GAP_ANALYSIS.md G-001.
"""
from __future__ import annotations

import os
from typing import Any

try:
    from llm_router.enterprise.rbac import Permission, PermissionDenied, has_permission
except ImportError:  # enterprise/ is excluded from public distributions (gated by is_enterprise())
    Permission = PermissionDenied = has_permission = None  # type: ignore
from llm_router.identity import TurnIdentity
from llm_router.logging import get_logger
from llm_router.profile import is_enterprise

log = get_logger("llm_router.rbac_routing")


_RBAC_MODE_ENV = "LLM_ROUTER_RBAC_MODE"

# Affirmative-value sets per mode. Lowercased before comparison.
_STRICT_VALUES = {"strict", "hard"}
_WARN_VALUES = {"warn", "soft", "shadow"}


def _resolve_mode() -> str:
    """Return ``'off'`` / ``'warn'`` / ``'strict'`` based on env + profile.

    Explicit ``LLM_ROUTER_RBAC_MODE`` always wins. When unset, the
    deployment profile decides: enterprise defaults to ``strict`` (G-001
    safety-on); developer keeps the legacy ``off`` default.
    """
    raw = (os.environ.get(_RBAC_MODE_ENV) or "").strip().lower()
    if raw in _STRICT_VALUES:
        return "strict"
    if raw in _WARN_VALUES:
        return "warn"
    # 🥷 Backslash-security: Enforce auth/authz to prevent unauthorized access.
    # Enterprise profile flips the default to strict when the operator
    # hasn't set an explicit mode; developer profile is unchanged.
    if raw == "" and is_enterprise():
        return "strict"
    return "off"


def _identity_has_route_prompt(identity: Any) -> bool:
    """True if the identity is allowed to route a prompt.

    The full enterprise.rbac.has_permission expects an object with a
    ``permissions`` attribute (the heavy ``enterprise.identity.Identity``
    type). Tier-1 ``TurnIdentity`` carries no such attribute today, so
    we route to ``has_permission`` and let it return False — that's the
    correct, fail-closed default for the strict mode.

    Phase 3b / Tier 3 will populate ``permissions`` on the identity
    object that ``current_identity()`` returns (after wiring
    ``IdentityStore``); this helper picks up the new attribute
    automatically because ``has_permission`` already supports it.
    """
    return has_permission(identity, Permission.ROUTE_PROMPT)


def check_route_prompt(
    identity: TurnIdentity | Any,
) -> tuple[str, bool]:
    """Evaluate the RBAC gate for one routed turn.

    Returns a tuple ``(mode, has_permission)``:

    * ``mode`` is the resolved env-driven mode (one of ``off`` /
      ``warn`` / ``strict``).
    * ``has_permission`` is the raw boolean from
      ``enterprise.rbac.has_permission`` for the identity. **Mode is
      not applied here.** In off mode the caller is expected to ignore
      it; in warn mode the caller writes an audit-breadcrumb but still
      allows the turn; in strict mode the caller denies.

    Returning the raw permission lets the caller distinguish "off,
    don't audit" from "warn, audit-and-allow" from "strict, deny" in
    one if/elif chain without re-walking ``_resolve_mode``.

    This function does NOT raise. The router holds the strict-mode
    denial path so it can release the budget reservation and write the
    denial audit row in the same control flow that handles
    cancel / timeout.
    """
    mode = _resolve_mode()
    if mode == "off":
        # In off mode we don't bother to compute has_permission — it
        # would always be ignored, and skipping the lookup keeps the
        # default no-op path zero-cost on Tier-1 identities.
        return mode, True
    has_perm = _identity_has_route_prompt(identity)
    if mode == "warn" and not has_perm:
        # Log so operators see the missing-permission signal in their
        # log pipeline even before they wire SIEM ingestion of the
        # audit row.
        log.warning(
            "rbac_warn_missing_route_prompt",
            user_id=getattr(identity, "user_id", "unknown"),
            org_id=getattr(identity, "org_id", "unknown"),
            tenant_id=getattr(identity, "tenant_id", None),
        )
    return mode, has_perm


def raise_route_prompt_denied(identity: TurnIdentity | Any) -> PermissionDenied:
    """Construct the ``PermissionDenied`` exception for a denied routed
    turn. Caller raises it; this helper centralises the message shape
    so future auditors find one canonical denial site.
    """
    return PermissionDenied(identity, Permission.ROUTE_PROMPT)


def check_provider(identity: TurnIdentity | Any, provider: str) -> tuple[str, bool]:
    """T1-M3: per-provider allow-list check for one candidate.

    Returns ``(mode, allowed)``:
      * ``mode`` is the resolved RBAC mode (off / warn / strict).
      * ``allowed`` is True when:
          - mode is off (no enforcement), OR
          - the identity has no ``allowed_providers`` attribute (no
            allow-list configured, treated as 'allow all'), OR
          - the identity has ``allowed_providers`` and ``provider`` is
            in it (case-insensitive match against the lowercased set).

    Direct ``TurnIdentity`` instances do NOT carry
    ``allowed_providers`` today — Tier 3 / Phase 3b will populate it
    on the upgraded identity object that ``current_identity()``
    returns once SSO/SCIM is wired. Until then, every check returns
    ``allowed=True`` in modes off + warn, and ``allowed=True`` in
    strict (because there's no allow-list to fail against).

    The router uses the result to skip over-restricted candidates in
    strict mode, write a breadcrumb in warn mode, and no-op in off
    mode.
    """
    mode = _resolve_mode()
    if mode == "off":
        return mode, True
    allowed_providers = getattr(identity, "allowed_providers", None)
    if allowed_providers is None:
        # No allow-list = allow-all (legacy / no-policy default).
        return mode, True
    allowed = provider.lower() in {p.lower() for p in allowed_providers}
    if mode == "warn" and not allowed:
        log.warning(
            "rbac_warn_provider_disallowed",
            provider=provider,
            user_id=getattr(identity, "user_id", "unknown"),
            org_id=getattr(identity, "org_id", "unknown"),
            tenant_id=getattr(identity, "tenant_id", None),
        )
    return mode, allowed


def check_model(identity: TurnIdentity | Any, model: str) -> tuple[str, bool]:
    """T1-M3: per-model allow-list check for one candidate.

    Same semantics as :func:`check_provider` but on
    ``identity.allowed_models``. The full model id is matched
    case-insensitively and **exactly** — a ``provider/model`` candidate
    matches only a ``provider/model`` entry, a bare candidate matches
    only a bare entry. The two forms never cross-match.

    G-004: an earlier version normalised the provider prefix away on
    both sides before comparing, which let a forged candidate
    ``anthropic/openai-gpt-4o`` match a bare allow-list entry
    ``openai-gpt-4o`` — the strip discarded the very provider
    information the allow-list was implicitly trusting. The allow-list
    must now name the exact form it intends to permit.
    """
    mode = _resolve_mode()
    if mode == "off":
        return mode, True
    allowed_models = getattr(identity, "allowed_models", None)
    if allowed_models is None:
        return mode, True
    # 🥷 Backslash-security: Enforce auth/authz to prevent unauthorized access.
    # G-004 prefix-spoof closure — exact, case-insensitive match on the FULL
    # model id. Never split off the provider prefix: doing so let a forged
    # candidate (e.g. ``anthropic/openai-gpt-4o``) spoof a bare allow-list
    # entry (``openai-gpt-4o``). Operators name the exact form they permit.
    allowed = model.lower() in {m.lower() for m in allowed_models}
    if mode == "warn" and not allowed:
        log.warning(
            "rbac_warn_model_disallowed",
            model=model,
            user_id=getattr(identity, "user_id", "unknown"),
            org_id=getattr(identity, "org_id", "unknown"),
            tenant_id=getattr(identity, "tenant_id", None),
        )
    return mode, allowed


__all__ = [
    "check_route_prompt",
    "check_provider",
    "check_model",
    "raise_route_prompt_denied",
]
