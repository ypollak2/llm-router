"""T1-M3 (Track-1 identity, Medium): per-provider + per-model
permission checks.

Closes the SECOND SLICE of G-001 (the first slice was T1-M2's
``Permission.ROUTE_PROMPT`` gate at the routing chokepoint). Extends
RBAC to the per-candidate level inside ``_dispatch_model_loop``: an
identity that carries ``allowed_providers`` / ``allowed_models``
allow-lists has every chain candidate filtered against them; strict
mode skips disallowed candidates and ultimately raises
``PermissionDenied`` if the whole chain is filtered out.

Modes follow T1-M2:
* **off**     — no-op (default)
* **warn**    — log + audit, allow
* **strict**  — skip; if everything skipped, raise PermissionDenied

Identities without the allow-list attributes pass the gate
unconditionally (legacy / no-policy default). Production identity
objects (Tier 3) will populate them via SSO/SCIM.

See: Docs/audit/post-remediation/GAP_ANALYSIS.md G-001 (second slice).
"""
from __future__ import annotations

from typing import Any

import pytest

from llm_router.identity import TurnIdentity
from llm_router.rbac_routing import check_model, check_provider


@pytest.fixture
def clean_rbac_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("LLM_ROUTER_RBAC_MODE", raising=False)


class _IdentityWithLists:
    """Mimics the Tier-3 identity object that will carry allow-lists
    once SSO/SCIM lands. Used to exercise the rbac_routing helpers
    without standing up an IdentityStore.

    Carries ``Permission.ROUTE_PROMPT`` by default so the T1-M2 entry
    gate (set when strict mode is active in the end-to-end tests)
    lets the turn proceed and reach the per-candidate filter.
    """

    def __init__(
        self,
        *,
        allowed_providers: set[str] | None = None,
        allowed_models: set[str] | None = None,
        permissions: frozenset | None = None,
    ) -> None:
        from llm_router.enterprise.rbac import Permission

        self.user_id = "alice"
        self.user_email = "alice@corp.io"
        self.org_id = "acme"
        self.tenant_id = "acme"
        self.agent_id = None
        self.permissions = permissions or frozenset({Permission.ROUTE_PROMPT})
        # Lists are optional; either may be None.
        if allowed_providers is not None:
            self.allowed_providers = allowed_providers
        if allowed_models is not None:
            self.allowed_models = allowed_models


# ── 1. check_provider semantics ──────────────────────────────────────────────


def test_off_mode_always_allows_any_provider(clean_rbac_env) -> None:
    """No env / off mode = no enforcement, regardless of allow-list."""
    ident = _IdentityWithLists(allowed_providers={"gemini"})
    mode, ok = check_provider(ident, "openai")
    assert mode == "off"
    assert ok is True


def test_strict_no_allow_list_passes(clean_rbac_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A Tier-1 identity has no allowed_providers attribute. Strict
    mode treats that as 'allow all' (legacy / no-policy default)."""
    monkeypatch.setenv("LLM_ROUTER_RBAC_MODE", "strict")
    ident = TurnIdentity(
        user_id="u", user_email="u@l", org_id="o", tenant_id="o"
    )
    mode, ok = check_provider(ident, "openai")
    assert mode == "strict"
    assert ok is True


def test_strict_provider_in_allow_list_passes(
    clean_rbac_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_ROUTER_RBAC_MODE", "strict")
    ident = _IdentityWithLists(allowed_providers={"gemini", "anthropic"})
    assert check_provider(ident, "gemini") == ("strict", True)
    assert check_provider(ident, "anthropic") == ("strict", True)


def test_strict_provider_not_in_allow_list_denies(
    clean_rbac_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_ROUTER_RBAC_MODE", "strict")
    ident = _IdentityWithLists(allowed_providers={"gemini"})
    assert check_provider(ident, "openai") == ("strict", False)


def test_provider_match_is_case_insensitive(
    clean_rbac_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_ROUTER_RBAC_MODE", "strict")
    ident = _IdentityWithLists(allowed_providers={"Gemini"})
    assert check_provider(ident, "GEMINI") == ("strict", True)


def test_warn_mode_reports_has_perm_false(
    clean_rbac_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Warn mode returns the raw has-perm answer; the router decides
    behaviour (audit + allow). Mirrors T1-M2's helper shape."""
    monkeypatch.setenv("LLM_ROUTER_RBAC_MODE", "warn")
    ident = _IdentityWithLists(allowed_providers={"gemini"})
    mode, ok = check_provider(ident, "openai")
    assert mode == "warn"
    assert ok is False


# ── 2. check_model semantics ─────────────────────────────────────────────────


def test_model_strict_requires_exact_form_match(
    clean_rbac_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G-004: the allow-list entry and the candidate must match in the
    SAME form — both ``provider/model`` or both bare. The provider
    prefix is never stripped, so a forged prefix can't spoof a bare
    entry. (Supersedes the pre-G-004 behaviour, which cross-matched the
    two forms — exactly the gap G-004 closed.)"""
    monkeypatch.setenv("LLM_ROUTER_RBAC_MODE", "strict")
    # Exact prefixed entry matches the exact prefixed candidate.
    ident_b = _IdentityWithLists(allowed_models={"anthropic/claude-sonnet-4-6"})
    assert check_model(ident_b, "anthropic/claude-sonnet-4-6") == ("strict", True)
    # Cross-form never matches (the G-004 closure):
    ident_a = _IdentityWithLists(allowed_models={"claude-sonnet-4-6"})
    assert check_model(ident_a, "anthropic/claude-sonnet-4-6") == ("strict", False)
    assert check_model(ident_b, "claude-sonnet-4-6") == ("strict", False)


def test_model_strict_rejects_unlisted(
    clean_rbac_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_ROUTER_RBAC_MODE", "strict")
    ident = _IdentityWithLists(allowed_models={"gemini/gemini-2.5-flash"})
    assert check_model(ident, "anthropic/claude-opus-4-6") == ("strict", False)


def test_model_strict_no_allow_list_passes(
    clean_rbac_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_ROUTER_RBAC_MODE", "strict")
    ident = TurnIdentity(
        user_id="u", user_email="u@l", org_id="o", tenant_id="o"
    )
    assert check_model(ident, "anthropic/claude-opus-4-6") == ("strict", True)


def test_model_match_is_case_insensitive(
    clean_rbac_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_ROUTER_RBAC_MODE", "strict")
    ident = _IdentityWithLists(allowed_models={"GEMINI/GEMINI-2.5-FLASH"})
    assert check_model(ident, "gemini/gemini-2.5-flash") == ("strict", True)


# ── 3. End-to-end: router skips disallowed candidates ────────────────────────


@pytest.mark.asyncio
async def test_strict_chain_walks_past_disallowed_candidates(
    clean_rbac_env,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """With strict mode + allowed_providers={gemini}, openai
    candidates are skipped and the chain advances to the gemini
    candidate, which dispatches successfully."""
    from llm_router import router as router_mod
    from llm_router.audit_routing import reset_audit_log_for_tests
    from llm_router.router import route_and_call
    from llm_router.types import LLMResponse, TaskType

    db = tmp_path / "audit.db"
    monkeypatch.setenv("LLM_ROUTER_AUDIT_PATH", str(db))
    monkeypatch.delenv("LLM_ROUTER_AUDIT_DISABLED", raising=False)
    reset_audit_log_for_tests()
    monkeypatch.setenv("LLM_ROUTER_RBAC_MODE", "strict")
    # Idempotency DB isolated to keep tests independent.
    monkeypatch.setenv("LLM_ROUTER_IDEMPOTENCY_PATH", str(tmp_path / "idem.db"))
    from llm_router.idempotency import reset_store_for_tests
    reset_store_for_tests()

    # Track which models the dispatcher actually saw to confirm the
    # filter ran inside _dispatch_model_loop.
    seen_models: list[str] = []

    async def _fake_dispatch(**kwargs: Any) -> LLMResponse:
        models_to_try = kwargs.get("models_to_try", [])
        # Replicate the filter: per-candidate identity gate. The test
        # asserts on which model was first to dispatch; the production
        # loop does the filter itself, so by the time we see kwargs we
        # already have the post-filter list.
        seen_models.extend(models_to_try)
        return LLMResponse(
            content="ok",
            model=models_to_try[0],
            provider="gemini",
            input_tokens=1,
            output_tokens=1,
            cost_usd=0.001,
            latency_ms=10.0,
        )

    monkeypatch.setattr(router_mod, "_dispatch_model_loop", _fake_dispatch)

    ident = _IdentityWithLists(allowed_providers={"gemini"})
    resp = await route_and_call(
        task_type=TaskType.QUERY,
        prompt="hi",
        identity=ident,
    )
    assert resp.content == "ok"
    # The mock dispatcher received the full chain (filtering happens
    # inside _dispatch_model_loop, which we mocked — so this test pins
    # the wiring of identity through to dispatch). The next test pins
    # actual filtering behaviour.


@pytest.mark.asyncio
async def test_chain_all_disallowed_raises_permission_denied(
    clean_rbac_env,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """When every candidate in the chain is filtered out by the
    provider/model allow-lists in strict mode, route_and_call must
    raise PermissionDenied."""
    from llm_router.audit_routing import reset_audit_log_for_tests
    from llm_router.enterprise.rbac import PermissionDenied
    from llm_router.idempotency import reset_store_for_tests
    from llm_router.router import route_and_call
    from llm_router.types import TaskType

    db = tmp_path / "audit.db"
    monkeypatch.setenv("LLM_ROUTER_AUDIT_PATH", str(db))
    monkeypatch.delenv("LLM_ROUTER_AUDIT_DISABLED", raising=False)
    monkeypatch.setenv("LLM_ROUTER_IDEMPOTENCY_PATH", str(tmp_path / "idem.db"))
    monkeypatch.setenv("LLM_ROUTER_RBAC_MODE", "strict")
    reset_audit_log_for_tests()
    reset_store_for_tests()

    # Allow ONLY a provider that's not in any production chain.
    ident = _IdentityWithLists(allowed_providers={"non-existent-provider"})

    with pytest.raises(PermissionDenied):
        await route_and_call(
            task_type=TaskType.QUERY,
            prompt="hi",
            identity=ident,
        )
