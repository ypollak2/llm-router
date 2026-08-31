"""GH#69 — ``RouterConfig.llm_router_profile`` (config.py) is a THIRD,
independent reader of ``LLM_ROUTER_PROFILE``, distinct from the two GH#65
fixed (``repo_config.RepoConfig.effective_profile()``) and the deployment-
identity axis (``llm_router.profile`` / ``identity.py``).

Unlike ``effective_profile()`` (display-only — its only caller is
``llm_router config``), this pydantic-settings field is the LIVE routing
profile consumed throughout ``router.py``, ``orchestrator.py``, ``state.py``,
``tools/routing.py`` and the dashboard. Before this fix, pydantic-settings
bound it directly (by naming convention) to ``LLM_ROUTER_PROFILE`` and
validated it strictly against the six routing tiers — so setting
``LLM_ROUTER_PROFILE=enterprise`` (the one value historically documented to
select the enterprise identity profile) raised
``pydantic_core.ValidationError`` at ``RouterConfig()`` construction, which
``server.py:146`` triggers at IMPORT time. The whole MCP server could not
boot.

#76 also removed the entire enterprise surface (``llm_router.enterprise``,
``rbac_routing.py``, ``audit_routing.py``, ``scim_api.py``,
``commands/verify_enterprise.py``) — it is not shipped in this package.
``LLM_ROUTER_PROFILE=enterprise``/``LLM_ROUTER_DEPLOYMENT_PROFILE=enterprise``
is still MEANINGFUL to ``llm_router.profile.is_enterprise()`` (it flips a few
"strict" defaults and, since #76, ``server._startup_verify_or_die()``
deliberately refuses to boot with a clear message rather than a crash) — but
it is no longer a value this field should ever have tried to validate
against routing tiers in the first place.

The fix (this file tests it): apply the same value-domain filter GH#65
established for ``repo_config.py``, extended to actually matter here since
this field is the live reader. ``llm_router_profile`` now reads
``LLM_ROUTER_COST_PROFILE`` first (GH#65's de-collided name), then falls back
to the legacy ``LLM_ROUTER_PROFILE`` — but ONLY when that value is a real
routing tier. Anything else (``enterprise``, ``developer``, a plain typo)
never reaches pydantic's enum validator: a ``mode="before"`` validator
intercepts it, emits a one-shot stderr warning, and substitutes the default
profile instead of raising. The fix deliberately does NOT special-case the
string ``"enterprise"`` — any unrecognized value is treated the same way.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from llm_router import config as config_module
from llm_router.config import RouterConfig
from llm_router.types import RoutingProfile

REPO_ROOT = Path(__file__).resolve().parents[1]
_ENTRY_POINT = Path(sys.executable).with_name("llm-router")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_PROFILE", raising=False)
    monkeypatch.delenv("LLM_ROUTER_COST_PROFILE", raising=False)
    config_module._reset_llm_router_profile_fallback_warning_latch()


# ── 1. The exact crash from the issue must be gone ──────────────────────


def test_stale_enterprise_value_does_not_raise(monkeypatch):
    """The literal repro from GH#69: LLM_ROUTER_PROFILE=enterprise must not
    raise pydantic_core.ValidationError when RouterConfig() is constructed."""
    monkeypatch.setenv("LLM_ROUTER_PROFILE", "enterprise")
    cfg = RouterConfig()  # pre-fix: raised here
    assert cfg.llm_router_profile == RoutingProfile.BALANCED


# ── 2. Must not special-case "enterprise" ───────────────────────────────


def test_arbitrary_garbage_value_does_not_raise(monkeypatch):
    """A value with no meaning on ANY axis (not identity, not routing) must
    be handled identically to 'enterprise' — the fix is a value-domain
    filter, not a hardcoded string check."""
    monkeypatch.setenv("LLM_ROUTER_PROFILE", "xyzzy-not-a-real-profile-42")
    cfg = RouterConfig()
    assert cfg.llm_router_profile == RoutingProfile.BALANCED


def test_other_identity_axis_value_also_falls_back(monkeypatch):
    """'developer' is meaningful on the OTHER (identity) axis but is still
    not a routing tier — must fall back exactly like 'enterprise', not be
    coerced into some special developer-routing behavior."""
    monkeypatch.setenv("LLM_ROUTER_PROFILE", "developer")
    cfg = RouterConfig()
    assert cfg.llm_router_profile == RoutingProfile.BALANCED


# ── 3. Every genuinely valid routing tier still resolves ────────────────


@pytest.mark.parametrize(
    "tier",
    ["budget", "balanced", "premium", "reasoning", "quota_balanced", "subscription_local"],
)
def test_every_real_tier_still_resolves_via_legacy_env(monkeypatch, tier):
    monkeypatch.setenv("LLM_ROUTER_PROFILE", tier)
    cfg = RouterConfig()
    assert cfg.llm_router_profile == RoutingProfile(tier)


@pytest.mark.parametrize(
    "tier",
    ["budget", "balanced", "premium", "reasoning", "quota_balanced", "subscription_local"],
)
def test_every_real_tier_resolves_via_cost_profile_env(monkeypatch, tier):
    monkeypatch.setenv("LLM_ROUTER_COST_PROFILE", tier)
    cfg = RouterConfig()
    assert cfg.llm_router_profile == RoutingProfile(tier)


# ── 4. LLM_ROUTER_COST_PROFILE precedence (GH#65 pattern) ───────────────


def test_cost_profile_takes_precedence_over_legacy_profile(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_COST_PROFILE", "budget")
    monkeypatch.setenv("LLM_ROUTER_PROFILE", "premium")
    cfg = RouterConfig()
    assert cfg.llm_router_profile == RoutingProfile.BUDGET


def test_garbage_cost_profile_falls_back_to_valid_legacy_value(monkeypatch):
    """Mirrors GH#65's test_garbage_new_env_falls_back_to_legacy: an invalid
    LLM_ROUTER_COST_PROFILE must not win just because it was checked first —
    it must fall through to a genuinely valid legacy value."""
    monkeypatch.setenv("LLM_ROUTER_COST_PROFILE", "not-a-tier")
    monkeypatch.setenv("LLM_ROUTER_PROFILE", "premium")
    cfg = RouterConfig()
    assert cfg.llm_router_profile == RoutingProfile.PREMIUM


def test_garbage_everywhere_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_COST_PROFILE", "not-a-tier")
    monkeypatch.setenv("LLM_ROUTER_PROFILE", "enterprise")
    cfg = RouterConfig()
    assert cfg.llm_router_profile == RoutingProfile.BALANCED


def test_default_profile_when_neither_env_set():
    cfg = RouterConfig()
    assert cfg.llm_router_profile == RoutingProfile.BALANCED


# ── 5. Warning behavior: actionable, one-shot ────────────────────────────


def test_fallback_emits_actionable_one_shot_warning(monkeypatch, capsys):
    monkeypatch.setenv("LLM_ROUTER_PROFILE", "enterprise")
    RouterConfig()
    RouterConfig()
    RouterConfig()
    err = capsys.readouterr().err
    assert err.count("WARNING") == 1, "warning must fire once per process, not per construction"
    assert "enterprise" in err
    assert "LLM_ROUTER_COST_PROFILE" in err
    assert "LLM_ROUTER_DEPLOYMENT_PROFILE" in err


def test_valid_tier_emits_no_warning(monkeypatch, capsys):
    monkeypatch.setenv("LLM_ROUTER_PROFILE", "budget")
    RouterConfig()
    err = capsys.readouterr().err
    assert "WARNING" not in err


# ── 6. Regression guard: validation_alias must not break kwarg construction


def test_direct_kwarg_construction_by_field_name_still_works():
    """Every OTHER field in RouterConfig is constructible by its plain
    field name (the whole test suite relies on this, e.g.
    test_config_routing_value.py). Adding validation_alias for the
    LLM_ROUTER_COST_PROFILE precedence must not silently break
    `RouterConfig(llm_router_profile=...)` and fall back to the default —
    populate_by_name is what keeps this working."""
    cfg = RouterConfig(llm_router_profile=RoutingProfile.PREMIUM)
    assert cfg.llm_router_profile == RoutingProfile.PREMIUM

    cfg2 = RouterConfig(llm_router_profile="budget")
    assert cfg2.llm_router_profile == RoutingProfile.BUDGET


# ── 7. Real entry point: the actual reported crash site ─────────────────
#
# Driven as a real console script (never `python -c`) so the exact import
# chain from the issue (cli.py -> server.py:146 -> get_config() ->
# RouterConfig()) is exercised, not a synthetic stand-in.


def _run_entry_point(extra_env: dict) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(extra_env)
    return subprocess.run(
        [str(_ENTRY_POINT)],
        input=b"",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=REPO_ROOT,
        timeout=60,
    )


@pytest.mark.skipif(not _ENTRY_POINT.exists(), reason="llm-router console script not installed")
def test_real_entry_point_boots_cleanly_with_garbage_profile():
    """The reported crash's actual failure mode: a stale/garbage
    LLM_ROUTER_PROFILE must not prevent the server from starting at all."""
    result = _run_entry_point({"LLM_ROUTER_PROFILE": "totally-bogus-legacy-value"})
    stderr = result.stderr.decode("utf-8", "replace")
    assert "ValidationError" not in stderr
    assert "Traceback" not in stderr
    assert result.returncode == 0, (
        f"server failed to boot (exit {result.returncode}):\n{stderr}"
    )
    assert "WARNING" in stderr


@pytest.mark.skipif(not _ENTRY_POINT.exists(), reason="llm-router console script not installed")
def test_real_entry_point_refuses_enterprise_cleanly_not_via_crash():
    """LLM_ROUTER_PROFILE=enterprise is still refused post-#76 (the RBAC/
    audit/SCIM surface it would need isn't shipped) — but via
    server._startup_verify_or_die()'s deliberate, documented message, never
    via the pydantic crash this issue is about. Distinguishing these two
    failure modes is the whole point of the fix: an operator gets told
    *why*, instead of a traceback."""
    result = _run_entry_point({"LLM_ROUTER_PROFILE": "enterprise"})
    stderr = result.stderr.decode("utf-8", "replace")
    assert "ValidationError" not in stderr
    assert "Traceback" not in stderr
    assert "not supported in this distribution" in stderr
