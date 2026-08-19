"""Identity resolution for the routing audit chain.

Two operating modes, switched by ``LLM_ROUTER_PROFILE`` (see
``llm_router.profile``):

* **Developer (default).** Trusts the operator's environment. Reads
  ``LLM_ROUTER_USER_ID`` / ``LLM_ROUTER_USER_EMAIL`` / ``LLM_ROUTER_ORG_ID`` /
  ``LLM_ROUTER_AGENT_ID`` / ``LLM_ROUTER_TENANT_ID`` with sane fallbacks
  (``getpass.getuser()``, ``<user>@local``, ``"local"``). Never
  raises — a single-user dev workstation cannot be allowed to fail
  on identity resolution.

* **Enterprise (G-002).** Refuses env-trust. Reads ``LLM_ROUTER_TOKEN``,
  resolves it through ``llm_router.enterprise.identity.IdentityStore.authenticate``,
  verifies the identity carries ``Permission.ROUTE_PROMPT``, and
  maps the authenticated ``Identity`` into a ``TurnIdentity`` for
  the rest of the routing pipeline. Missing / invalid / under-
  permissioned token raises ``EnterpriseIdentityRequired`` so the
  first routed call fails loudly rather than silently routing as
  an env-claimed admin.

The agent dimension (``LLM_ROUTER_AGENT_ID``) is still read from env in
both modes — agent_id is a workflow-attribution tag, not an auth
principal. A future slice can promote it to an attestation chain
once agent provisioning is in scope.
"""
from __future__ import annotations

import getpass
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from llm_router.enterprise.rbac import Permission


# Env keys — kept here so callers (tests, doctor, install scripts) can
# import them without hardcoding the literal string.
LLM_ROUTER_USER_ID_ENV = "LLM_ROUTER_USER_ID"
LLM_ROUTER_USER_EMAIL_ENV = "LLM_ROUTER_USER_EMAIL"
LLM_ROUTER_ORG_ID_ENV = "LLM_ROUTER_ORG_ID"
# Tier 2: agents running on the user's computer. Optional — when the
# routed turn isn't part of an agent run (e.g. a direct MCP tool call
# from Claude Code), agent_id stays None and the audit row simply omits
# the agent dimension.
LLM_ROUTER_AGENT_ID_ENV = "LLM_ROUTER_AGENT_ID"
# T1-M1 (Q-P-2 Phase 3a): typed-but-implicit tenant axis. When unset,
# the resolver defaults ``tenant_id`` to ``org_id`` so the field is
# never None in production — consumers (budget keys, audit detail,
# log contextvars) can rely on it. Set explicitly only in Phase 3b
# (sidecar-per-tenant) where one llm_router process serves a tenant
# distinct from its org. See Docs/audit/decisions/Q-P-2_multi-tenancy.md.
LLM_ROUTER_TENANT_ID_ENV = "LLM_ROUTER_TENANT_ID"
# P0-2: developer-mode team attribution for team-scope budgets. Enterprise /
# OIDC paths derive team from the authenticated user instead; this env only
# applies to the env-trust developer resolver and is usually unset (single-user).
LLM_ROUTER_TEAM_ID_ENV = "LLM_ROUTER_TEAM_ID"

# Default org for Tier-1 single-user mode. Picked to be obviously
# non-SSO-derived so a future audit reader can tell at a glance that the
# events were emitted before tenancy wiring landed.
DEFAULT_ORG_ID = "local"

# Sentinel fallback when even ``getpass.getuser()`` fails (tty-less
# environments, restricted containers). Never raise — the routing path
# must never block on identity resolution.
_FALLBACK_USER_ID = "unknown"

# G-002: enterprise-profile token env. Operators set this in CI /
# Docker / Kubernetes secret-injection. The token is validated via
# ``IdentityStore.authenticate`` on every routed turn; failure raises
# ``EnterpriseIdentityRequired`` so the deployment cannot accidentally
# route as an env-claimed admin.
LLM_ROUTER_TOKEN_ENV = "LLM_ROUTER_TOKEN"


class EnterpriseIdentityRequired(RuntimeError):
    """Raised when ``LLM_ROUTER_PROFILE=enterprise`` is set but no valid
    ``LLM_ROUTER_TOKEN`` can be resolved into an identity with
    ``Permission.ROUTE_PROMPT``.

    The exception text points at the documented setup path so an
    operator hitting this for the first time has a clear next step
    rather than a generic auth error.
    """


# G-002: lazy singleton IdentityStore for the enterprise resolver.
# Module-level rather than per-call so we don't reopen the SQLite
# connection on every routed turn. Tests monkeypatch this to inject
# a scoped store.
_enterprise_store = None  # type: ignore[var-annotated]


def _get_enterprise_store():
    """Resolve the process-wide ``IdentityStore`` for token validation.

    Opened with ``check_same_thread=False`` because the routing path
    is async / cross-task; the underlying SQLite serialises writes."""
    global _enterprise_store
    if _enterprise_store is None:
        from llm_router.enterprise.identity import IdentityStore
        _enterprise_store = IdentityStore(check_same_thread=False)
    return _enterprise_store


# OIDC federation env keys. When LLM_ROUTER_OIDC_ISSUER is set, a LLM_ROUTER_TOKEN that
# is NOT a llm_router-native ``tsr_`` token is validated as an IdP-issued JWT and the
# user is just-in-time provisioned. See llm_router.enterprise.oidc.
LLM_ROUTER_OIDC_ISSUER_ENV = "LLM_ROUTER_OIDC_ISSUER"
LLM_ROUTER_OIDC_DEFAULT_ORG_ENV = "LLM_ROUTER_OIDC_DEFAULT_ORG"
LLM_ROUTER_OIDC_DEFAULT_TEAM_ENV = "LLM_ROUTER_OIDC_DEFAULT_TEAM"

# Lazy singleton OIDC validator (mirrors the IdentityStore singleton). Built once
# from LLM_ROUTER_OIDC_* env; tests monkeypatch this to inject a JWKS-backed validator.
_oidc_validator = None  # type: ignore[var-annotated]


def _oidc_configured() -> bool:
    return bool((os.environ.get(LLM_ROUTER_OIDC_ISSUER_ENV) or "").strip())


def _get_oidc_validator():
    """Resolve the process-wide OIDC validator, or None when OIDC is unset."""
    global _oidc_validator
    if _oidc_validator is None:
        from llm_router.enterprise.oidc import OidcConfig, OidcValidator
        config = OidcConfig.from_env()
        if config is None:
            return None
        _oidc_validator = OidcValidator(config)
    return _oidc_validator


def _oidc_identity(token: str, store) -> "TurnIdentity":
    """Resolve a federated (OIDC JWT) ``LLM_ROUTER_TOKEN`` into a TurnIdentity.

    Validates the JWT, maps groups → role, just-in-time provisions the user
    under the configured default org/team, and verifies ROUTE_PROMPT. Raises
    ``EnterpriseIdentityRequired`` on any failure so a misconfigured federation
    fails loudly rather than routing unauthenticated.

    🥷 Backslash-security: Enforce auth/authz to prevent unauthorized access.
    """
    from llm_router.enterprise.oidc import OidcError
    from llm_router.enterprise.rbac import Permission, permissions_for_role

    validator = _get_oidc_validator()
    if validator is None:
        raise EnterpriseIdentityRequired(
            f"{LLM_ROUTER_TOKEN_ENV} is not a llm_router 'tsr_' token and OIDC is not "
            f"configured. Set {LLM_ROUTER_OIDC_ISSUER_ENV} (+ audience) to enable "
            "federated identity, or issue a llm_router token via the admin API."
        )
    try:
        claims = validator.validate_sync(token)
    except OidcError as exc:
        raise EnterpriseIdentityRequired(f"OIDC token rejected: {exc}.")

    role = validator.map_role(claims.groups)
    if Permission.ROUTE_PROMPT not in permissions_for_role(role):
        raise EnterpriseIdentityRequired(
            f"OIDC-mapped role {role.value!r} does not grant routing."
        )

    org_name = (os.environ.get(LLM_ROUTER_OIDC_DEFAULT_ORG_ENV) or "default").strip() or "default"
    team_name = (os.environ.get(LLM_ROUTER_OIDC_DEFAULT_TEAM_ENV) or "default").strip() or "default"
    org = store.get_or_create_org(org_name)
    team = store.get_or_create_team(org.id, team_name)
    user = store.get_or_create_by_external_id(
        external_id=claims.subject,
        email=claims.email,
        display_name=claims.email,
        role=role,
        org_id=org.id,
        team_id=team.id,
    )
    if not user.active:
        raise EnterpriseIdentityRequired(
            f"federated user {user.email!r} is deactivated."
        )

    agent_id = (os.environ.get(LLM_ROUTER_AGENT_ID_ENV) or "").strip() or None
    tenant_id = (os.environ.get(LLM_ROUTER_TENANT_ID_ENV) or "").strip() or user.org_id
    return TurnIdentity(
        user_id=user.id,
        user_email=user.email,
        org_id=user.org_id,
        agent_id=agent_id,
        tenant_id=tenant_id,
        team_id=user.team_id,
        permissions=frozenset(permissions_for_role(role)),
        allowed_providers=user.allowed_providers,
        allowed_models=user.allowed_models,
    )


def _enterprise_identity(store=None) -> "TurnIdentity":
    """G-002 enterprise-profile resolver. Reads ``LLM_ROUTER_TOKEN``,
    authenticates against ``IdentityStore``, requires
    ``Permission.ROUTE_PROMPT``, maps to ``TurnIdentity``.

    When OIDC is configured (``LLM_ROUTER_OIDC_ISSUER`` set) and the token is not a
    llm_router-native ``tsr_`` token, it is validated as an IdP JWT and the user is
    just-in-time provisioned (see ``_oidc_identity``).

    Raises ``EnterpriseIdentityRequired`` on any failure. The caller
    (``current_identity``) propagates the exception so the first
    routed turn fails loudly under a misconfigured enterprise
    deployment instead of silently falling back to env-trust.
    """
    token = (os.environ.get(LLM_ROUTER_TOKEN_ENV) or "").strip()
    if not token:
        raise EnterpriseIdentityRequired(
            f"LLM_ROUTER_PROFILE=enterprise requires {LLM_ROUTER_TOKEN_ENV} "
            "to be set to a valid bearer token issued via the admin "
            "API (POST /v1/admin/users/{user_id}/tokens). See "
            "docs/audit/post-remediation/GAP_ANALYSIS.md#g-002."
        )

    if store is None:
        store = _get_enterprise_store()

    # Federated (OIDC JWT) tokens don't carry the llm_router 'tsr_' prefix. Route
    # them to the OIDC resolver when federation is enabled.
    if not token.startswith("tsr_") and _oidc_configured():
        return _oidc_identity(token, store)

    from llm_router.enterprise.identity import InvalidToken
    from llm_router.enterprise.rbac import Permission

    try:
        identity = store.authenticate(token)
    except InvalidToken as exc:
        raise EnterpriseIdentityRequired(
            f"{LLM_ROUTER_TOKEN_ENV} is not a valid token: {exc}. "
            "Revoke and re-issue via the admin API."
        )

    if Permission.ROUTE_PROMPT not in identity.permissions:
        raise EnterpriseIdentityRequired(
            f"identity {identity.user.email!r} lacks Permission.ROUTE_PROMPT — "
            "issue a token for a role that grants routing (ADMIN / "
            "MANAGER / EMPLOYEE) or extend the token's permissions."
        )

    # Agent dimension is still env-derived in enterprise mode — see
    # module docstring for the rationale.
    agent_id_raw = (os.environ.get(LLM_ROUTER_AGENT_ID_ENV) or "").strip()
    agent_id = agent_id_raw or None
    tenant_id_raw = (os.environ.get(LLM_ROUTER_TENANT_ID_ENV) or "").strip()
    tenant_id = tenant_id_raw or identity.user.org_id

    return TurnIdentity(
        user_id=identity.user.id,
        user_email=identity.user.email,
        org_id=identity.user.org_id,
        agent_id=agent_id,
        tenant_id=tenant_id,
        team_id=identity.user.team_id,
        permissions=frozenset(identity.permissions),
        allowed_providers=identity.user.allowed_providers,
        allowed_models=identity.user.allowed_models,
    )


@dataclass(frozen=True)
class TurnIdentity:
    """Minimal identity used by Tier 1's audit-on-every-turn wiring.

    Carries just enough to populate one ``AuditEvent`` row. Promoted to
    ``llm_router.enterprise.identity.Identity`` in Tier 3 when RBAC lands.

    Attributes
    ----------
    user_id:
        The actor for audit attribution. Sourced from ``LLM_ROUTER_USER_ID``
        with a ``getpass.getuser()`` fallback. Never empty.
    user_email:
        Denormalised email for SIEM readability. Sourced from
        ``LLM_ROUTER_USER_EMAIL`` with a ``<user_id>@local`` fallback so the
        downstream column is never NULL.
    org_id:
        The org the event is bucketed under. Defaults to ``"local"`` for
        Tier 1 single-user mode; replaced by a real org slug in Tier 3.
    """

    user_id: str
    user_email: str
    org_id: str
    # Tier 2: the agent driving this turn, if any. None means "this turn
    # was not part of an agent run" (a direct MCP tool call, a CLI
    # invocation, etc.). When set, the audit row carries an ``agent_id``
    # field in ``detail`` and the log contextvars get an ``agent_id`` key.
    agent_id: str | None = None
    # T1-M1 (Q-P-2 Phase 3a): typed-but-implicit tenant axis. ``None`` on
    # the dataclass default for backwards compat with direct
    # ``TurnIdentity(...)`` construction; ``current_identity()`` always
    # populates it (env > org_id fallback) so production callers see a
    # non-None value. Downstream consumers should treat ``None`` as
    # "no tenant attribution available" — the resolver's job to avoid
    # producing that state in production.
    tenant_id: str | None = None
    # P0-2: the team the actor belongs to, for team-scope budget enforcement.
    # Enterprise / OIDC paths set this from the authenticated user's team;
    # developer mode reads ``LLM_ROUTER_TEAM_ID`` (usually None for single-user).
    # ``None`` means "no team attribution" — quota_routing then enforces the
    # user scope only, preserving pre-P0-2 behaviour.
    team_id: str | None = None
    # Phase 3b: the RBAC payload carried from the authenticated
    # enterprise/OIDC identity so the wired routing gates enforce on the
    # REAL principal. ``permissions`` feeds ``check_route_prompt`` (via
    # ``has_permission``); the allow-lists feed ``check_provider`` /
    # ``check_model``. Dev / Tier-1 env-trust leaves all three at their
    # permissive defaults — empty perms only matter under strict mode,
    # which the developer profile never activates.
    permissions: frozenset[Permission] = field(default_factory=frozenset)
    # ``None`` == unrestricted; a non-empty set restricts routing to those
    # providers / models.
    allowed_providers: frozenset[str] | None = None
    allowed_models: frozenset[str] | None = None


def current_identity() -> TurnIdentity:
    """Resolve the current identity.

    Dispatches on ``LLM_ROUTER_PROFILE`` (see ``llm_router.profile``):

    * **Enterprise** — delegates to ``_enterprise_identity``. Raises
      ``EnterpriseIdentityRequired`` on missing / invalid /
      under-permissioned ``LLM_ROUTER_TOKEN``. The first routed turn
      fails loudly; existing fail-open audit / RBAC paths take over
      from there (G-001/G-003 enterprise defaults).

    * **Developer (default)** — env-trust resolver below. Never
      raises; an unset environment falls back through
      ``getpass.getuser()`` to a sentinel.

    Resolution order in developer mode — first non-empty value wins:

    ``user_id``:
        1. ``LLM_ROUTER_USER_ID`` env var
        2. ``getpass.getuser()`` (works under cron, ssh, and containers
           where ``os.getlogin()`` would raise ``OSError``)
        3. Sentinel ``"unknown"``

    ``user_email``:
        1. ``LLM_ROUTER_USER_EMAIL`` env var
        2. ``"<user_id>@local"``

    ``org_id``:
        1. ``LLM_ROUTER_ORG_ID`` env var
        2. Sentinel ``"local"``
    """
    # G-002: enterprise profile refuses env-trust. The Tier-3 path
    # is the *only* identity source; missing / invalid token raises.
    from llm_router.profile import is_enterprise
    if is_enterprise():
        return _enterprise_identity()

    user_id = (os.environ.get(LLM_ROUTER_USER_ID_ENV) or "").strip()
    if not user_id:
        try:
            user_id = getpass.getuser()
        except Exception:
            user_id = _FALLBACK_USER_ID

    user_email = (os.environ.get(LLM_ROUTER_USER_EMAIL_ENV) or "").strip()
    if not user_email:
        user_email = f"{user_id}@local"

    org_id = (os.environ.get(LLM_ROUTER_ORG_ID_ENV) or "").strip()
    if not org_id:
        org_id = DEFAULT_ORG_ID

    # agent_id is optional. Blank / whitespace / unset all collapse to None
    # so downstream consumers (audit detail, log contextvars) can use a
    # simple ``if identity.agent_id:`` check.
    agent_id_raw = (os.environ.get(LLM_ROUTER_AGENT_ID_ENV) or "").strip()
    agent_id = agent_id_raw or None

    # T1-M1 (Q-P-2 Phase 3a): tenant_id is non-None in production.
    # Explicit ``LLM_ROUTER_TENANT_ID`` wins; otherwise fall back to
    # ``org_id`` so single-org-per-instance deployments carry the
    # tenant dimension for forward compat without forcing every
    # caller to populate it. Phase 3b (sidecar-per-tenant) sets the
    # env explicitly so llm_router processes within one org can serve
    # distinct tenants.
    tenant_id_raw = (os.environ.get(LLM_ROUTER_TENANT_ID_ENV) or "").strip()
    tenant_id = tenant_id_raw or org_id

    # P0-2: team is opt-in in developer mode (single-user installs have no
    # team). None → quota_routing enforces the user scope only.
    team_id = (os.environ.get(LLM_ROUTER_TEAM_ID_ENV) or "").strip() or None

    return TurnIdentity(
        user_id=user_id,
        user_email=user_email,
        org_id=org_id,
        agent_id=agent_id,
        tenant_id=tenant_id,
        team_id=team_id,
    )


__all__ = [
    "LLM_ROUTER_USER_ID_ENV",
    "LLM_ROUTER_USER_EMAIL_ENV",
    "LLM_ROUTER_ORG_ID_ENV",
    "LLM_ROUTER_AGENT_ID_ENV",
    "LLM_ROUTER_TENANT_ID_ENV",
    "LLM_ROUTER_TEAM_ID_ENV",
    "LLM_ROUTER_TOKEN_ENV",
    "LLM_ROUTER_OIDC_ISSUER_ENV",
    "LLM_ROUTER_OIDC_DEFAULT_ORG_ENV",
    "LLM_ROUTER_OIDC_DEFAULT_TEAM_ENV",
    "DEFAULT_ORG_ID",
    "EnterpriseIdentityRequired",
    "TurnIdentity",
    "current_identity",
]
