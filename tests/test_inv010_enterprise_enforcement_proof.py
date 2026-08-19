"""INV-010 closure proof — the enterprise *profile* alone activates the
already-wired routing-path enforcement, end to end.

The audit's anchor finding F-INV-010 was that ``enterprise/rbac`` +
``enterprise/audit`` shipped but had **zero callers from the routing
path**. That has since been remediated piecemeal (T1-M2 route-prompt
gate, T1-M3 per-candidate provider/model filter, audit_routing_turn
call sites in ``router.route_and_call``). The final activation piece
was the deployment-profile default-flip: under ``enterprise`` the RBAC
mode resolves to ``strict`` and audit becomes mandatory *without* the
operator setting ``LLM_ROUTER_RBAC_MODE`` / ``LLM_ROUTER_AUDIT_DISABLED`` by
hand.

Existing e2e tests pin the enforcement by setting ``LLM_ROUTER_RBAC_MODE=
strict`` explicitly. This file pins the integration the audit actually
cares about: **set only ``LLM_ROUTER_DEPLOYMENT_PROFILE=enterprise`` and the
wired gates enforce on a real ``route_and_call``.** It also pins the
developer-profile no-op so we can prove zero regression for the OSS
edition.

Note: this exercises enforcement given an identity that already carries
allow-lists (``_IdentityWithLists``). Populating those lists from an
SSO/SCIM token is a separate identity-resolution concern (Phase 3b);
here we prove the *enforcement* half composes with the *profile* flip.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


class _IdentityWithLists:
    """Stand-in for the Tier-3 identity that SSO/SCIM will mint —
    carries ``Permission.ROUTE_PROMPT`` plus optional provider/model
    allow-lists. Mirrors the helper in
    ``test_t1_m3_per_provider_model_permissions``."""

    def __init__(
        self,
        *,
        allowed_providers: set[str] | None = None,
        allowed_models: set[str] | None = None,
    ) -> None:
        from llm_router.enterprise.rbac import Permission

        self.user_id = "alice"
        self.user_email = "alice@corp.io"
        self.org_id = "acme"
        self.tenant_id = "acme"
        self.agent_id = None
        self.permissions = frozenset({Permission.ROUTE_PROMPT})
        if allowed_providers is not None:
            self.allowed_providers = allowed_providers
        if allowed_models is not None:
            self.allowed_models = allowed_models


@pytest.fixture
def enterprise_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Enterprise profile with NOTHING else set — the whole point is to
    prove the profile alone activates enforcement. Isolated audit +
    idempotency DBs so the assertions are deterministic."""
    from llm_router.audit_routing import reset_audit_log_for_tests
    from llm_router.idempotency import reset_store_for_tests

    # The deployment profile is the ONLY enforcement knob we set.
    monkeypatch.setenv("LLM_ROUTER_DEPLOYMENT_PROFILE", "enterprise")
    monkeypatch.delenv("LLM_ROUTER_PROFILE", raising=False)
    monkeypatch.delenv("LLM_ROUTER_RBAC_MODE", raising=False)
    monkeypatch.delenv("LLM_ROUTER_AUDIT_DISABLED", raising=False)
    monkeypatch.setenv("LLM_ROUTER_AUDIT_PATH", str(tmp_path / "audit.db"))
    monkeypatch.setenv("LLM_ROUTER_IDEMPOTENCY_PATH", str(tmp_path / "idem.db"))
    reset_audit_log_for_tests()
    reset_store_for_tests()


@pytest.mark.asyncio
async def test_enterprise_profile_alone_denies_disallowed_chain(
    enterprise_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The headline INV-010 proof. With ONLY the enterprise profile set
    (no explicit ``LLM_ROUTER_RBAC_MODE``), an identity whose allow-list
    permits no production provider gets the whole chain filtered →
    ``PermissionDenied`` from ``route_and_call``. This composes the #70
    default-flip with the already-wired per-candidate RBAC gate."""
    from llm_router.enterprise.rbac import PermissionDenied
    from llm_router.router import route_and_call
    from llm_router.types import TaskType

    ident = _IdentityWithLists(allowed_providers={"non-existent-provider"})
    with pytest.raises(PermissionDenied):
        await route_and_call(
            task_type=TaskType.QUERY, prompt="hi", identity=ident
        )


@pytest.mark.asyncio
async def test_enterprise_profile_allows_permitted_identity(
    enterprise_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The allow path: under the enterprise profile an identity whose
    allow-list permits the dispatched provider routes successfully — the
    strict gate doesn't over-block a permitted candidate."""
    from llm_router import router as router_mod
    from llm_router.router import route_and_call
    from llm_router.types import LLMResponse, TaskType

    async def _fake_dispatch(**kwargs: Any) -> LLMResponse:
        models = kwargs.get("models_to_try", ["gemini/gemini-2.5-flash"])
        return LLMResponse(
            content="ok",
            model=models[0],
            provider="gemini",
            input_tokens=1,
            output_tokens=1,
            cost_usd=0.001,
            latency_ms=10.0,
        )

    monkeypatch.setattr(router_mod, "_dispatch_model_loop", _fake_dispatch)
    ident = _IdentityWithLists(allowed_providers={"gemini"})
    resp = await route_and_call(
        task_type=TaskType.QUERY, prompt="hi", identity=ident
    )
    assert resp.content == "ok"


@pytest.mark.asyncio
async def test_developer_profile_does_not_enforce(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zero-regression proof for the OSS edition: the SAME disallowed
    identity under the developer profile (no enforcement) routes
    normally — strict enforcement is enterprise-only."""
    from llm_router import router as router_mod
    from llm_router.audit_routing import reset_audit_log_for_tests
    from llm_router.idempotency import reset_store_for_tests
    from llm_router.router import route_and_call
    from llm_router.types import LLMResponse, TaskType

    monkeypatch.delenv("LLM_ROUTER_DEPLOYMENT_PROFILE", raising=False)
    monkeypatch.delenv("LLM_ROUTER_PROFILE", raising=False)
    monkeypatch.delenv("LLM_ROUTER_RBAC_MODE", raising=False)
    monkeypatch.setenv("LLM_ROUTER_AUDIT_PATH", str(tmp_path / "audit.db"))
    monkeypatch.setenv("LLM_ROUTER_IDEMPOTENCY_PATH", str(tmp_path / "idem.db"))
    reset_audit_log_for_tests()
    reset_store_for_tests()

    async def _fake_dispatch(**kwargs: Any) -> LLMResponse:
        models = kwargs.get("models_to_try", ["gemini/gemini-2.5-flash"])
        return LLMResponse(
            content="ok",
            model=models[0],
            provider="gemini",
            input_tokens=1,
            output_tokens=1,
            cost_usd=0.001,
            latency_ms=10.0,
        )

    monkeypatch.setattr(router_mod, "_dispatch_model_loop", _fake_dispatch)
    # Disallowed under enterprise, but developer profile = no enforcement.
    ident = _IdentityWithLists(allowed_providers={"non-existent-provider"})
    resp = await route_and_call(
        task_type=TaskType.QUERY, prompt="hi", identity=ident
    )
    assert resp.content == "ok"
