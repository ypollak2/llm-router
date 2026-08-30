"""GH#65 — ``LLM_ROUTER_PROFILE`` meant two unrelated things depending on
which module read it.

``repo_config.py::RepoConfig.effective_profile()`` read ``LLM_ROUTER_PROFILE``
for the *routing cost tier* (budget/balanced/premium/...). Separately,
``llm_router.profile`` / ``identity.py`` / ``server.py`` read the SAME env
name for the unrelated *enterprise identity* axis (developer/enterprise).

The identity side had already renamed to ``LLM_ROUTER_DEPLOYMENT_PROFILE``
(see ``llm_router.profile.PROFILE_ENV``) — the reporter followed that
deprecation guidance in their own ``.zshrc`` and it silently broke routing,
because ``effective_profile()`` never knew the new name existed and fell
through to ``None``.

The fix (this PR): the ROUTING side takes a new name,
``LLM_ROUTER_COST_PROFILE``. The legacy ``LLM_ROUTER_PROFILE`` name is
still honored as a fallback, but ONLY when its value is a valid routing
tier — a value like ``developer``/``enterprise`` is identity-axis data and
must be ignored by the routing reader, not misinterpreted. That
value-domain filter is what makes the two readers mutually exclusive even
during the deprecation window, rather than only in some future 14.0.
"""
from __future__ import annotations

import pytest

from llm_router import repo_config
from llm_router.profile import (
    Profile,
    _reset_legacy_warning_latch,
    resolve_profile,
)
from llm_router.types import RoutingProfile


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch) -> None:
    monkeypatch.delenv("LLM_ROUTER_COST_PROFILE", raising=False)
    monkeypatch.delenv("LLM_ROUTER_PROFILE", raising=False)
    monkeypatch.delenv("LLM_ROUTER_DEPLOYMENT_PROFILE", raising=False)
    repo_config._reset_legacy_cost_profile_warning_latch()
    _reset_legacy_warning_latch()


def _cfg() -> repo_config.RepoConfig:
    return repo_config.RepoConfig()


# ── 1. The exact trap the reporter fell into ────────────────────────────


def test_deployment_profile_alone_does_not_resolve_routing_profile() -> None:
    """Following the identity-side rename guidance and setting ONLY
    LLM_ROUTER_DEPLOYMENT_PROFILE must not crash the routing reader, and
    must not accidentally resolve a routing profile either — routing simply
    has no opinion, and identity stays developer since DEPLOYMENT_PROFILE
    wasn't set to 'enterprise'."""
    import os

    os.environ["LLM_ROUTER_DEPLOYMENT_PROFILE"] = "balanced"
    try:
        assert _cfg().effective_profile() is None
        assert resolve_profile() == Profile.DEVELOPER
    finally:
        del os.environ["LLM_ROUTER_DEPLOYMENT_PROFILE"]


# ── 2. New name is read directly ────────────────────────────────────────


def test_new_cost_profile_env_is_read(monkeypatch) -> None:
    monkeypatch.setenv("LLM_ROUTER_COST_PROFILE", "premium")
    assert _cfg().effective_profile() == "premium"


# ── 3. The collision itself, both halves in one test ────────────────────


def test_legacy_enterprise_value_ignored_by_routing_but_honored_by_identity(
    monkeypatch,
) -> None:
    monkeypatch.setenv("LLM_ROUTER_PROFILE", "enterprise")
    # Routing reader: "enterprise" is not a routing tier -- must be ignored,
    # not crash, not silently coerced into some default tier's value.
    assert _cfg().effective_profile() is None
    # Identity reader: same raw env, still honored as the legacy identity
    # value.
    assert resolve_profile() == Profile.ENTERPRISE


# ── 4. Legacy fallback still works for genuinely valid tiers, with a warning


def test_legacy_valid_tier_still_resolves_and_warns_once(
    monkeypatch, capsys,
) -> None:
    monkeypatch.setenv("LLM_ROUTER_PROFILE", "balanced")
    cfg = _cfg()
    assert cfg.effective_profile() == "balanced"
    assert cfg.effective_profile() == "balanced"
    assert cfg.effective_profile() == "balanced"
    err = capsys.readouterr().err
    assert err.count("DEPRECATED") == 1
    assert "LLM_ROUTER_COST_PROFILE" in err


def test_legacy_env_not_consulted_when_new_env_set(monkeypatch, capsys) -> None:
    """New env wins; legacy must not even be inspected, so no deprecation
    warning fires when the migration is already complete."""
    monkeypatch.setenv("LLM_ROUTER_COST_PROFILE", "budget")
    monkeypatch.setenv("LLM_ROUTER_PROFILE", "premium")
    assert _cfg().effective_profile() == "budget"
    assert "DEPRECATED" not in capsys.readouterr().err


# ── 5. Repo-config fallback (neither env set) is untouched ──────────────


def test_repo_config_value_used_when_no_env_set() -> None:
    cfg = repo_config.RepoConfig(profile="premium")
    assert cfg.effective_profile() == "premium"


# ── 6. The value-domain set is wired to the real enum, not a hand list ──


def test_valid_profiles_matches_real_routing_profile_enum() -> None:
    """GH#65's plan text guessed budget/balanced/premium/quota_balanced/
    subscription_local -- the real ``RoutingProfile`` enum also has
    'reasoning'. Pin against the enum itself so the set can't drift again
    the way the old hand-maintained {'budget','balanced','premium'} did."""
    assert repo_config.VALID_PROFILES == {p.value for p in RoutingProfile}
    assert "reasoning" in repo_config.VALID_PROFILES
    assert "quota_balanced" in repo_config.VALID_PROFILES
    assert "subscription_local" in repo_config.VALID_PROFILES


@pytest.mark.parametrize(
    "tier", ["budget", "balanced", "premium", "reasoning",
             "quota_balanced", "subscription_local"],
)
def test_every_real_tier_is_honored_via_new_env(monkeypatch, tier) -> None:
    monkeypatch.setenv("LLM_ROUTER_COST_PROFILE", tier)
    assert _cfg().effective_profile() == tier


# ── 7. Typo / garbage guard on both env names ────────────────────────────


def test_garbage_new_env_falls_back_to_legacy(monkeypatch) -> None:
    monkeypatch.setenv("LLM_ROUTER_COST_PROFILE", "not-a-tier")
    monkeypatch.setenv("LLM_ROUTER_PROFILE", "balanced")
    assert _cfg().effective_profile() == "balanced"


def test_garbage_everywhere_falls_back_to_repo_config() -> None:
    import os

    os.environ["LLM_ROUTER_COST_PROFILE"] = "not-a-tier"
    os.environ["LLM_ROUTER_PROFILE"] = "developer"
    try:
        cfg = repo_config.RepoConfig(profile="balanced")
        assert cfg.effective_profile() == "balanced"
    finally:
        del os.environ["LLM_ROUTER_COST_PROFILE"]
        del os.environ["LLM_ROUTER_PROFILE"]
