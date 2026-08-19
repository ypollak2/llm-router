"""SUBSCRIPTION_LOCAL profile — cost-inverted capability routing.

Models the enterprise shape: one paid subscription seat + a free
bucket of local + org-hosted internal models.

Three ordering regimes covered:

1. **Healthy + simple/moderate** → free → subscription → other paid.
   The seat is the safety-net fallback so routine prompts never fail
   when Ollama is down, but paid usage stays bounded.
2. **Healthy + complex** → subscription → free → other paid. Complex
   work goes to the capable seat first; locals are the fallback.
3. **Strained subscription (≥80% 5h-quota pressure)** → free →
   other paid → subscription. Demote the strained seat regardless
   of complexity so the remaining quota is preserved for prompts
   that really need it.

Backward compat: when the profile is something other than
SUBSCRIPTION_LOCAL, OR when ``LLM_ROUTER_SUBSCRIPTION_PROVIDER`` is unset,
the reorder is a no-op.
"""
from __future__ import annotations

import asyncio

import pytest

from llm_router import subscription_local_routing as sl
from llm_router.subscription_local_routing import (
    get_free_bucket,
    get_internal_providers,
    get_pressure_threshold,
    get_subscription_provider,
    is_subscription_local_active,
    is_subscription_strained,
    reorder_for_subscription_local,
)
from llm_router.types import RoutingProfile


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch) -> None:
    for env in (
        "LLM_ROUTER_SUBSCRIPTION_PROVIDER",
        "LLM_ROUTER_INTERNAL_PROVIDERS",
        "LLM_ROUTER_SUBSCRIPTION_PRESSURE_THRESHOLD",
        "LLM_ROUTER_SUBSCRIPTION_REORDER_ALL_PROFILES",
    ):
        monkeypatch.delenv(env, raising=False)


# ── 1. Configuration accessors ──────────────────────────────────────────────


def test_subscription_provider_unset_returns_none() -> None:
    assert get_subscription_provider() is None


def test_subscription_provider_lowercased(monkeypatch) -> None:
    monkeypatch.setenv("LLM_ROUTER_SUBSCRIPTION_PROVIDER", "Anthropic")
    assert get_subscription_provider() == "anthropic"


def test_internal_providers_csv_parsed(monkeypatch) -> None:
    monkeypatch.setenv(
        "LLM_ROUTER_INTERNAL_PROVIDERS", "internal_llm, company_mistral ,  "
    )
    assert get_internal_providers() == {"internal_llm", "company_mistral"}


def test_free_bucket_combines_local_and_internal(monkeypatch) -> None:
    monkeypatch.setenv("LLM_ROUTER_INTERNAL_PROVIDERS", "internal_llm")
    bucket = get_free_bucket()
    assert "ollama" in bucket
    assert "internal_llm" in bucket


def test_pressure_threshold_defaults_to_eighty_percent() -> None:
    assert get_pressure_threshold() == 0.80


def test_pressure_threshold_env_override(monkeypatch) -> None:
    monkeypatch.setenv("LLM_ROUTER_SUBSCRIPTION_PRESSURE_THRESHOLD", "0.95")
    assert get_pressure_threshold() == 0.95


def test_pressure_threshold_clamped_to_range(monkeypatch) -> None:
    monkeypatch.setenv("LLM_ROUTER_SUBSCRIPTION_PRESSURE_THRESHOLD", "-0.5")
    assert get_pressure_threshold() == 0.0
    monkeypatch.setenv("LLM_ROUTER_SUBSCRIPTION_PRESSURE_THRESHOLD", "1.5")
    assert get_pressure_threshold() == 1.0


def test_pressure_threshold_invalid_value_falls_back(monkeypatch) -> None:
    monkeypatch.setenv("LLM_ROUTER_SUBSCRIPTION_PRESSURE_THRESHOLD", "high")
    assert get_pressure_threshold() == 0.80


# ── 2. is_subscription_local_active gate ────────────────────────────────────


def test_active_requires_profile_and_env(monkeypatch) -> None:
    """Original SUBSCRIPTION_LOCAL-only activation contract (pre-
    option-#2). Pinned with the cross-profile extension explicitly
    disabled to keep this test focused on the original gate; the
    option-#2 behaviour is covered separately further down."""
    monkeypatch.setenv(
        "LLM_ROUTER_SUBSCRIPTION_REORDER_ALL_PROFILES", "off"
    )
    # Wrong profile + env → inactive (extension disabled).
    monkeypatch.setenv("LLM_ROUTER_SUBSCRIPTION_PROVIDER", "anthropic")
    assert is_subscription_local_active(RoutingProfile.BALANCED) is False
    # Right profile, missing env → inactive.
    monkeypatch.delenv("LLM_ROUTER_SUBSCRIPTION_PROVIDER")
    assert is_subscription_local_active(
        RoutingProfile.SUBSCRIPTION_LOCAL
    ) is False
    # Both → active.
    monkeypatch.setenv("LLM_ROUTER_SUBSCRIPTION_PROVIDER", "anthropic")
    assert is_subscription_local_active(
        RoutingProfile.SUBSCRIPTION_LOCAL
    ) is True


# ── 3. Reorder no-ops ───────────────────────────────────────────────────────


def test_noop_when_profile_is_other_and_extension_disabled(monkeypatch) -> None:
    """Pre-option-#2 contract: BALANCED + sub configured = no-op.
    After option #2 this requires explicitly disabling the cross-
    profile extension. The new (default) behaviour — BALANCED + sub
    DOES fire the reorder — is covered by
    ``test_active_under_balanced_when_subscription_set`` below."""
    monkeypatch.setenv("LLM_ROUTER_SUBSCRIPTION_PROVIDER", "anthropic")
    monkeypatch.setenv(
        "LLM_ROUTER_SUBSCRIPTION_REORDER_ALL_PROFILES", "off"
    )
    chain = ["anthropic/sonnet", "ollama/llama3", "openai/gpt-4o"]
    assert reorder_for_subscription_local(
        chain, complexity="simple", profile=RoutingProfile.BALANCED,
    ) == chain


def test_noop_when_subscription_env_unset() -> None:
    chain = ["anthropic/sonnet", "ollama/llama3"]
    assert reorder_for_subscription_local(
        chain, complexity="simple",
        profile=RoutingProfile.SUBSCRIPTION_LOCAL,
    ) == chain


# ── 4. Healthy subscription — simple/moderate prefers free first ────────────


@pytest.mark.parametrize("complexity", ["simple", "moderate"])
def test_healthy_simple_moderate_puts_free_first(
    monkeypatch, complexity: str
) -> None:
    monkeypatch.setenv("LLM_ROUTER_SUBSCRIPTION_PROVIDER", "anthropic")
    chain = [
        "anthropic/claude-sonnet-4-6",   # subscription
        "openai/gpt-4o",                  # other paid
        "ollama/llama3",                  # free
        "gemini/gemini-2.5-pro",          # other paid
    ]
    result = reorder_for_subscription_local(
        chain, complexity=complexity,
        profile=RoutingProfile.SUBSCRIPTION_LOCAL,
        subscription_pressure=0.10,
    )
    # Free first, then subscription, then other paid (stable within tier).
    assert result == [
        "ollama/llama3",
        "anthropic/claude-sonnet-4-6",
        "openai/gpt-4o",
        "gemini/gemini-2.5-pro",
    ]


def test_internal_providers_count_as_free(monkeypatch) -> None:
    monkeypatch.setenv("LLM_ROUTER_SUBSCRIPTION_PROVIDER", "anthropic")
    monkeypatch.setenv("LLM_ROUTER_INTERNAL_PROVIDERS", "internal_llm")
    chain = [
        "anthropic/claude",
        "openai/gpt-4o",
        "internal_llm/mistral-22b",
        "ollama/llama3",
    ]
    result = reorder_for_subscription_local(
        chain, complexity="simple",
        profile=RoutingProfile.SUBSCRIPTION_LOCAL,
        subscription_pressure=0.10,
    )
    assert result[:2] == ["internal_llm/mistral-22b", "ollama/llama3"]
    assert result[2] == "anthropic/claude"


# ── 5. Healthy subscription — complex prefers subscription first ────────────


@pytest.mark.parametrize("complexity", ["complex", "research", "unknown"])
def test_healthy_complex_puts_subscription_first(
    monkeypatch, complexity: str
) -> None:
    monkeypatch.setenv("LLM_ROUTER_SUBSCRIPTION_PROVIDER", "anthropic")
    chain = [
        "ollama/llama3",
        "openai/gpt-4o",
        "anthropic/claude-opus",
    ]
    result = reorder_for_subscription_local(
        chain, complexity=complexity,
        profile=RoutingProfile.SUBSCRIPTION_LOCAL,
        subscription_pressure=0.10,
    )
    # Subscription wins the head; free bucket second; other paid last.
    assert result == [
        "anthropic/claude-opus",
        "ollama/llama3",
        "openai/gpt-4o",
    ]


# ── 6. Strained subscription — demote regardless of complexity ──────────────


def test_strained_subscription_demoted_even_for_complex(monkeypatch) -> None:
    """The 5h quota is >=80% spent. Even complex prompts go free
    first; subscription is the last-resort fallback."""
    monkeypatch.setenv("LLM_ROUTER_SUBSCRIPTION_PROVIDER", "anthropic")
    chain = [
        "anthropic/claude-opus",
        "openai/gpt-4o",
        "ollama/llama3",
    ]
    result = reorder_for_subscription_local(
        chain, complexity="complex",
        profile=RoutingProfile.SUBSCRIPTION_LOCAL,
        subscription_pressure=0.85,
    )
    # Free → other paid → subscription.
    assert result == [
        "ollama/llama3",
        "openai/gpt-4o",
        "anthropic/claude-opus",
    ]


def test_strained_subscription_demoted_for_simple_too(monkeypatch) -> None:
    monkeypatch.setenv("LLM_ROUTER_SUBSCRIPTION_PROVIDER", "anthropic")
    chain = [
        "anthropic/sonnet",
        "openai/gpt-4o",
        "ollama/llama3",
    ]
    result = reorder_for_subscription_local(
        chain, complexity="simple",
        profile=RoutingProfile.SUBSCRIPTION_LOCAL,
        subscription_pressure=0.99,
    )
    assert result == [
        "ollama/llama3",
        "openai/gpt-4o",
        "anthropic/sonnet",
    ]


def test_threshold_is_inclusive(monkeypatch) -> None:
    """Exactly at the threshold counts as strained."""
    monkeypatch.setenv("LLM_ROUTER_SUBSCRIPTION_PROVIDER", "anthropic")
    assert is_subscription_strained(0.80) is True
    assert is_subscription_strained(0.7999) is False


def test_threshold_env_override_changes_demotion(monkeypatch) -> None:
    """Operator raises the threshold to 0.95 → 0.85 pressure no
    longer triggers demotion."""
    monkeypatch.setenv("LLM_ROUTER_SUBSCRIPTION_PROVIDER", "anthropic")
    monkeypatch.setenv("LLM_ROUTER_SUBSCRIPTION_PRESSURE_THRESHOLD", "0.95")
    chain = ["anthropic/opus", "ollama/llama3"]
    result = reorder_for_subscription_local(
        chain, complexity="complex",
        profile=RoutingProfile.SUBSCRIPTION_LOCAL,
        subscription_pressure=0.85,
    )
    # 0.85 < 0.95 → not strained → complex puts subscription first.
    assert result[0] == "anthropic/opus"


# ── 7. Pressure source: graceful failure ────────────────────────────────────


def test_none_pressure_treated_as_healthy(monkeypatch) -> None:
    """If pressure can't be resolved (custom subscription provider,
    quota_balance unavailable, etc.) the reorder treats it as healthy
    and uses complexity-only logic."""
    monkeypatch.setenv("LLM_ROUTER_SUBSCRIPTION_PROVIDER", "anthropic")
    chain = ["anthropic/opus", "ollama/llama3"]
    result = reorder_for_subscription_local(
        chain, complexity="complex",
        profile=RoutingProfile.SUBSCRIPTION_LOCAL,
        subscription_pressure=None,
    )
    assert result[0] == "anthropic/opus"


def test_get_subscription_pressure_returns_none_when_unconfigured() -> None:
    """No subscription provider → no pressure source → None."""
    assert asyncio.run(sl.get_subscription_pressure()) is None


def test_get_subscription_pressure_returns_none_for_unknown_provider(
    monkeypatch,
) -> None:
    """A custom subscription provider with no mapping in
    `_PROVIDER_TO_PRESSURE_KEY` returns None — no demotion signal."""
    monkeypatch.setenv("LLM_ROUTER_SUBSCRIPTION_PROVIDER", "custom_seat")
    assert asyncio.run(sl.get_subscription_pressure()) is None


def test_get_subscription_pressure_fetches_from_quota_balance(
    monkeypatch,
) -> None:
    """Configured anthropic subscription → pressure key 'claude' → the
    quota_balance dict value flows through."""
    monkeypatch.setenv("LLM_ROUTER_SUBSCRIPTION_PROVIDER", "anthropic")

    async def fake_pressures():
        return {"claude": 0.92, "gemini_cli": 0.10, "codex": 0.0}

    monkeypatch.setattr(
        "llm_router.quota_balance.get_provider_pressures", fake_pressures
    )
    assert asyncio.run(sl.get_subscription_pressure()) == 0.92


def test_get_subscription_pressure_fails_open_on_exception(monkeypatch) -> None:
    """If quota_balance raises, we return None — never break routing."""
    monkeypatch.setenv("LLM_ROUTER_SUBSCRIPTION_PROVIDER", "anthropic")

    async def boom():
        raise RuntimeError("quota service down")

    monkeypatch.setattr(
        "llm_router.quota_balance.get_provider_pressures", boom
    )
    assert asyncio.run(sl.get_subscription_pressure()) is None


# ── 8. Option #2: cross-profile extension ────────────────────────────────────
#
# Stage-4 decision (see docs/audit/post-remediation/
# SUBSCRIPTION_DEMOTION_DECISION.md): the reorder applies under any
# profile when LLM_ROUTER_SUBSCRIPTION_PROVIDER is set, so a strained
# subscription is demoted under BALANCED / PREMIUM / QUOTA_BALANCED
# too. Operators can flip this off via
# LLM_ROUTER_SUBSCRIPTION_REORDER_ALL_PROFILES=off to restore the
# SUBSCRIPTION_LOCAL-only behaviour.


def test_cross_profile_extension_default_on() -> None:
    assert sl.is_cross_profile_extension_enabled() is True


@pytest.mark.parametrize("off_value", ["off", "0", "false", "no", "disabled"])
def test_cross_profile_extension_off_values(monkeypatch, off_value: str) -> None:
    monkeypatch.setenv(
        "LLM_ROUTER_SUBSCRIPTION_REORDER_ALL_PROFILES", off_value
    )
    assert sl.is_cross_profile_extension_enabled() is False


def test_cross_profile_extension_unknown_value_stays_on(monkeypatch) -> None:
    """Typos must NOT silently disable a safety feature. Any
    unrecognised value keeps the extension on; only explicit off
    values flip it."""
    monkeypatch.setenv("LLM_ROUTER_SUBSCRIPTION_REORDER_ALL_PROFILES", "yes")
    assert sl.is_cross_profile_extension_enabled() is True


def test_active_under_balanced_when_subscription_set(monkeypatch) -> None:
    """The Stage-4 escalation: BALANCED + subscription configured →
    reorder applies. Before option #2 this was False."""
    monkeypatch.setenv("LLM_ROUTER_SUBSCRIPTION_PROVIDER", "anthropic")
    assert is_subscription_local_active(RoutingProfile.BALANCED) is True


def test_active_under_premium_when_subscription_set(monkeypatch) -> None:
    monkeypatch.setenv("LLM_ROUTER_SUBSCRIPTION_PROVIDER", "anthropic")
    assert is_subscription_local_active(RoutingProfile.PREMIUM) is True


def test_active_under_quota_balanced_when_subscription_set(
    monkeypatch,
) -> None:
    monkeypatch.setenv("LLM_ROUTER_SUBSCRIPTION_PROVIDER", "anthropic")
    assert is_subscription_local_active(RoutingProfile.QUOTA_BALANCED) is True


def test_inactive_under_balanced_when_extension_disabled(
    monkeypatch,
) -> None:
    """Operator-controlled opt-out: setting REORDER_ALL_PROFILES=off
    restores SUBSCRIPTION_LOCAL-only gating. BALANCED no longer
    activates even with the env set."""
    monkeypatch.setenv("LLM_ROUTER_SUBSCRIPTION_PROVIDER", "anthropic")
    monkeypatch.setenv(
        "LLM_ROUTER_SUBSCRIPTION_REORDER_ALL_PROFILES", "off"
    )
    assert is_subscription_local_active(RoutingProfile.BALANCED) is False
    # But SUBSCRIPTION_LOCAL itself still activates — the opt-out
    # affects only the cross-profile extension.
    assert is_subscription_local_active(
        RoutingProfile.SUBSCRIPTION_LOCAL
    ) is True


def test_inactive_under_balanced_when_subscription_unset() -> None:
    """Without a subscription provider the reorder must stay a no-op
    under any profile — protects existing developer installs from a
    surprise routing change after upgrade."""
    assert is_subscription_local_active(RoutingProfile.BALANCED) is False
    assert is_subscription_local_active(RoutingProfile.PREMIUM) is False
    assert is_subscription_local_active(
        RoutingProfile.QUOTA_BALANCED
    ) is False


def test_balanced_strained_subscription_gets_demoted(monkeypatch) -> None:
    """The headline production behaviour. Under BALANCED with a
    configured subscription at ≥80% pressure, the strained seat is
    demoted regardless of complexity. This is the test that the
    Stage-3 characterization could NOT conclusively run because it
    only exercised primitives, not the production path."""
    monkeypatch.setenv("LLM_ROUTER_SUBSCRIPTION_PROVIDER", "anthropic")
    chain = [
        "anthropic/claude-opus",     # strained subscription
        "openai/gpt-4o",              # other paid
        "ollama/llama3",              # free
    ]
    result = reorder_for_subscription_local(
        chain, complexity="complex",
        profile=RoutingProfile.BALANCED,
        subscription_pressure=0.95,
    )
    assert result == [
        "ollama/llama3",
        "openai/gpt-4o",
        "anthropic/claude-opus",
    ]


def test_balanced_healthy_complex_keeps_subscription_first(
    monkeypatch,
) -> None:
    """Under BALANCED with healthy pressure (<80%) and complex task,
    the subscription stays at the head — the cross-profile extension
    does not silently subvert BALANCED's preference for capability
    when nothing is strained."""
    monkeypatch.setenv("LLM_ROUTER_SUBSCRIPTION_PROVIDER", "anthropic")
    chain = [
        "anthropic/claude-opus",
        "openai/gpt-4o",
        "ollama/llama3",
    ]
    result = reorder_for_subscription_local(
        chain, complexity="complex",
        profile=RoutingProfile.BALANCED,
        subscription_pressure=0.10,
    )
    assert result[0] == "anthropic/claude-opus"


def test_premium_strained_subscription_gets_demoted(monkeypatch) -> None:
    """Mirror of the BALANCED test for PREMIUM — the same demotion
    holds. PREMIUM existing users get strain-protection too."""
    monkeypatch.setenv("LLM_ROUTER_SUBSCRIPTION_PROVIDER", "openai")
    chain = [
        "codex/gpt-5",                # other subscription
        "openai/gpt-5",               # this is our strained subscription
        "anthropic/claude",           # other paid
        "ollama/llama3",              # free
    ]
    # Provider key for openai → maps to codex in pressure dict but the
    # reorder uses the provider name in LLM_ROUTER_SUBSCRIPTION_PROVIDER —
    # which means models whose provider segment equals 'openai' are
    # the demoted ones. (codex/ models are a different provider tag.)
    result = reorder_for_subscription_local(
        chain, complexity="complex",
        profile=RoutingProfile.PREMIUM,
        subscription_pressure=0.90,
    )
    # ollama first (free), then other paid (codex, anthropic), then
    # the strained openai last.
    assert result[0] == "ollama/llama3"
    assert result[-1] == "openai/gpt-5"


def test_quota_balanced_keeps_extension_too(monkeypatch) -> None:
    """QUOTA_BALANCED already has its own pressure-aware demotion in
    router.py. The cross-profile extension shouldn't fight it — both
    should be applied (the SUBSCRIPTION_LOCAL reorder runs first
    inside build_chain; the router-level reorder runs later). Test
    that the SUBSCRIPTION_LOCAL reorder fires under QUOTA_BALANCED
    with a configured subscription."""
    monkeypatch.setenv("LLM_ROUTER_SUBSCRIPTION_PROVIDER", "anthropic")
    chain = [
        "anthropic/sonnet",
        "ollama/llama3",
    ]
    result = reorder_for_subscription_local(
        chain, complexity="simple",
        profile=RoutingProfile.QUOTA_BALANCED,
        subscription_pressure=0.95,
    )
    assert result == ["ollama/llama3", "anthropic/sonnet"]


def test_extension_disabled_balanced_is_noop(monkeypatch) -> None:
    """When the cross-profile extension is explicitly off, BALANCED
    + configured subscription must still be a no-op for backward
    compatibility with the SUBSCRIPTION_LOCAL-only contract."""
    monkeypatch.setenv("LLM_ROUTER_SUBSCRIPTION_PROVIDER", "anthropic")
    monkeypatch.setenv(
        "LLM_ROUTER_SUBSCRIPTION_REORDER_ALL_PROFILES", "off"
    )
    chain = ["anthropic/sonnet", "ollama/llama3", "openai/gpt-4o"]
    result = reorder_for_subscription_local(
        chain, complexity="simple",
        profile=RoutingProfile.BALANCED,
        subscription_pressure=0.95,
    )
    assert result == chain  # unchanged
