"""Loop-5 #1 — rename slice-3 ``LLM_ROUTER_PROFILE`` to
``LLM_ROUTER_DEPLOYMENT_PROFILE`` to kill the env-name collision with
llm_router's pre-existing Pydantic routing config.

Pre-fix: setting ``LLM_ROUTER_PROFILE=enterprise`` for the deployment
axis crashed ``get_config()`` because that env was already bound
to the routing ``llm_router_profile`` field (Pydantic validates against
``balanced/premium/.../subscription_local``). Tests in
``test_sec004_status_resource_gate.py`` exposed the collision and
the SEC-004 handler took a fail-soft path to avoid crashing.

This rename makes the deployment axis explicit. Backward compat:

* ``LLM_ROUTER_DEPLOYMENT_PROFILE`` is the canonical env.
* ``LLM_ROUTER_PROFILE`` is read AFTER the new env, with a one-shot
  stderr deprecation warning.
* Neither set → ``Profile.DEVELOPER`` (unchanged baseline).
* Typos in either env fall back to ``DEVELOPER`` (the existing
  defensive-default rule from slice 3).

Tests cover the resolution order, the one-shot warning latch, and
the hook script's inlined detector (``_is_enterprise_profile``).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from llm_router.profile import (
    PROFILE_ENV,
    Profile,
    _reset_legacy_warning_latch,
    is_enterprise,
    resolve_profile,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch) -> None:
    monkeypatch.delenv("LLM_ROUTER_DEPLOYMENT_PROFILE", raising=False)
    monkeypatch.delenv("LLM_ROUTER_PROFILE", raising=False)
    _reset_legacy_warning_latch()


# ── 1. Canonical env name pinned ─────────────────────────────────────────


def test_canonical_env_name_is_deployment_profile() -> None:
    """The constant the rest of the code reads ought to point at the
    new name. Pinning so a future "let's go back to LLM_ROUTER_PROFILE"
    revert breaks loudly."""
    assert PROFILE_ENV == "LLM_ROUTER_DEPLOYMENT_PROFILE"


# ── 2. Resolution order: new env wins ────────────────────────────────────


def test_new_env_resolves_to_enterprise(monkeypatch) -> None:
    monkeypatch.setenv("LLM_ROUTER_DEPLOYMENT_PROFILE", "enterprise")
    assert resolve_profile() == Profile.ENTERPRISE
    assert is_enterprise() is True


def test_new_env_resolves_to_developer_when_explicit(monkeypatch) -> None:
    monkeypatch.setenv("LLM_ROUTER_DEPLOYMENT_PROFILE", "developer")
    assert resolve_profile() == Profile.DEVELOPER


@pytest.mark.parametrize("alias", ["prod", "production", "ENTERPRISE"])
def test_new_env_aliases_still_work(monkeypatch, alias: str) -> None:
    monkeypatch.setenv("LLM_ROUTER_DEPLOYMENT_PROFILE", alias)
    assert resolve_profile() == Profile.ENTERPRISE


# ── 3. Backward compat: legacy env still works ───────────────────────────


def test_legacy_env_still_resolves_to_enterprise(monkeypatch) -> None:
    monkeypatch.setenv("LLM_ROUTER_PROFILE", "enterprise")
    assert resolve_profile() == Profile.ENTERPRISE


def test_legacy_env_emits_one_shot_deprecation_warning(
    monkeypatch, capsys,
) -> None:
    monkeypatch.setenv("LLM_ROUTER_PROFILE", "enterprise")
    resolve_profile()
    err = capsys.readouterr().err
    assert "DEPRECATED" in err
    assert "LLM_ROUTER_DEPLOYMENT_PROFILE" in err


def test_legacy_warning_only_fires_once(monkeypatch, capsys) -> None:
    """Multiple ``resolve_profile`` calls must emit the warning ONCE.
    Pinning the latch so noise doesn't drown out other startup
    diagnostics."""
    monkeypatch.setenv("LLM_ROUTER_PROFILE", "enterprise")
    resolve_profile()
    resolve_profile()
    resolve_profile()
    err = capsys.readouterr().err
    assert err.count("DEPRECATED") == 1


def test_legacy_warning_latch_can_be_reset_for_tests(
    monkeypatch, capsys,
) -> None:
    """The test helper restores the latch so subsequent unit tests
    can observe the warning. Pinning the helper's existence so a
    future refactor doesn't accidentally remove it."""
    monkeypatch.setenv("LLM_ROUTER_PROFILE", "enterprise")
    resolve_profile()
    capsys.readouterr()  # drain
    _reset_legacy_warning_latch()
    resolve_profile()
    err = capsys.readouterr().err
    assert "DEPRECATED" in err


# ── 4. New env wins over legacy if both set ─────────────────────────────


def test_new_env_takes_precedence_over_legacy(
    monkeypatch, capsys,
) -> None:
    """When BOTH are set, the new env wins and the legacy is NOT
    consulted (so no deprecation warning fires). Pinning so an
    operator mid-migration who sets both isn't surprised by the
    warning continuing to fire."""
    monkeypatch.setenv("LLM_ROUTER_DEPLOYMENT_PROFILE", "enterprise")
    monkeypatch.setenv("LLM_ROUTER_PROFILE", "developer")
    assert resolve_profile() == Profile.ENTERPRISE
    err = capsys.readouterr().err
    assert "DEPRECATED" not in err


def test_new_env_developer_overrides_legacy_enterprise(
    monkeypatch, capsys,
) -> None:
    """Edge case mid-migration: new env explicitly set to developer
    overrides a legacy enterprise value. Pinning so a planned
    rollback (operator flips new env to ``developer`` mid-migration)
    behaves predictably."""
    monkeypatch.setenv("LLM_ROUTER_DEPLOYMENT_PROFILE", "developer")
    monkeypatch.setenv("LLM_ROUTER_PROFILE", "enterprise")
    assert resolve_profile() == Profile.DEVELOPER


# ── 5. Typo guards ──────────────────────────────────────────────────────


def test_typo_in_new_env_falls_back_to_developer(monkeypatch) -> None:
    monkeypatch.setenv("LLM_ROUTER_DEPLOYMENT_PROFILE", "entrprise")
    assert resolve_profile() == Profile.DEVELOPER


def test_typo_in_legacy_env_falls_back_to_developer(monkeypatch) -> None:
    monkeypatch.setenv("LLM_ROUTER_PROFILE", "entrprise")
    assert resolve_profile() == Profile.DEVELOPER


# ── 6. Hook script (auto-route.py) honours the new env too ──────────────


@pytest.fixture(scope="module")
def hook_module():
    repo_root = Path(__file__).resolve().parent.parent
    hook_path = repo_root / "src" / "llm_router" / "hooks" / "auto-route.py"
    spec = importlib.util.spec_from_file_location(
        "_llm_router_auto_route_loop5_test", hook_path
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_hook_detector_honours_new_env(hook_module, monkeypatch) -> None:
    monkeypatch.setenv("LLM_ROUTER_DEPLOYMENT_PROFILE", "enterprise")
    assert hook_module._is_enterprise_profile() is True


def test_hook_detector_falls_back_to_legacy_env(
    hook_module, monkeypatch,
) -> None:
    monkeypatch.setenv("LLM_ROUTER_PROFILE", "enterprise")
    assert hook_module._is_enterprise_profile() is True


def test_hook_detector_new_env_takes_precedence(
    hook_module, monkeypatch,
) -> None:
    """When both are set the new env wins — same semantics as
    ``resolve_profile``. Pinning the symmetry so the hook and the
    library never disagree."""
    monkeypatch.setenv("LLM_ROUTER_DEPLOYMENT_PROFILE", "developer")
    monkeypatch.setenv("LLM_ROUTER_PROFILE", "enterprise")
    assert hook_module._is_enterprise_profile() is False


def test_hook_detector_both_unset_is_not_enterprise(
    hook_module,
) -> None:
    assert hook_module._is_enterprise_profile() is False


# ── 7. Cross-check: every slice-3-era consumer still works ──────────────


def test_rbac_mode_honours_new_env(monkeypatch) -> None:
    """Mirror of ``test_enterprise_profile_flips_all_three_defaults``
    from the slice-3 test file, but using the new env name. Pin that
    the chain still works through the rename."""
    monkeypatch.setenv("LLM_ROUTER_DEPLOYMENT_PROFILE", "enterprise")
    from llm_router.rbac_routing import _resolve_mode

    assert _resolve_mode() == "strict"


def test_audit_disabled_honours_new_env(monkeypatch) -> None:
    monkeypatch.setenv("LLM_ROUTER_DEPLOYMENT_PROFILE", "enterprise")
    monkeypatch.setenv("LLM_ROUTER_AUDIT_DISABLED", "1")
    from llm_router.audit_routing import _audit_disabled

    # Under enterprise the env is refused — audit stays on.
    assert _audit_disabled() is False


def test_redaction_honours_new_env(monkeypatch) -> None:
    monkeypatch.setenv("LLM_ROUTER_DEPLOYMENT_PROFILE", "enterprise")
    from llm_router.redaction_routing import _redaction_enabled

    assert _redaction_enabled() is True
