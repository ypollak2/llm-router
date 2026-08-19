"""Tests for llm_router.subscription_local_routing + its chain_builder wiring."""

import pytest

from llm_router.types import RoutingProfile
from llm_router import subscription_local_routing as sl
from llm_router.chain_builder import _apply_subscription_local

# provider/model chain: ollama (free), anthropic (the seat), openai (other paid)
CHAIN = ["ollama/hermes3:8b", "anthropic/claude-sonnet", "openai/gpt-4o", "ollama/qwen"]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("LLM_ROUTER_SUBSCRIPTION_PROVIDER", "LLM_ROUTER_INTERNAL_PROVIDERS",
              "LLM_ROUTER_SUBSCRIPTION_PRESSURE_THRESHOLD",
              "LLM_ROUTER_SUBSCRIPTION_REORDER_ALL_PROFILES"):
        monkeypatch.delenv(k, raising=False)
    sl.set_pressure_provider(None)


def _providers(chain):
    return [sl._provider_of(m) for m in chain]


def test_noop_when_unconfigured():
    """No subscription provider set ⇒ chain unchanged (safe default)."""
    out = sl.reorder_for_subscription_local(
        CHAIN, complexity="complex", profile=RoutingProfile.BALANCED)
    assert out == CHAIN


def test_healthy_simple_is_free_first(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_SUBSCRIPTION_PROVIDER", "anthropic")
    out = sl.reorder_for_subscription_local(
        CHAIN, complexity="simple", profile=RoutingProfile.SUBSCRIPTION_LOCAL)
    # free (ollama) → subscription (anthropic) → other (openai)
    assert _providers(out) == ["ollama", "ollama", "anthropic", "openai"]


def test_healthy_complex_is_subscription_first(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_SUBSCRIPTION_PROVIDER", "anthropic")
    out = sl.reorder_for_subscription_local(
        CHAIN, complexity="complex", profile=RoutingProfile.SUBSCRIPTION_LOCAL)
    assert _providers(out) == ["anthropic", "ollama", "ollama", "openai"]


def test_strained_seat_demoted_last(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_SUBSCRIPTION_PROVIDER", "anthropic")
    out = sl.reorder_for_subscription_local(
        CHAIN, complexity="complex", profile=RoutingProfile.SUBSCRIPTION_LOCAL,
        subscription_pressure=0.95)  # >= 0.80 default
    # free → other paid → subscription (LAST)
    assert _providers(out) == ["ollama", "ollama", "openai", "anthropic"]


def test_stable_within_tier(monkeypatch):
    """Order within a tier is preserved (stable sort)."""
    monkeypatch.setenv("LLM_ROUTER_SUBSCRIPTION_PROVIDER", "anthropic")
    out = sl.reorder_for_subscription_local(
        CHAIN, complexity="simple", profile=RoutingProfile.SUBSCRIPTION_LOCAL)
    assert out[0] == "ollama/hermes3:8b" and out[1] == "ollama/qwen"  # incoming order kept


def test_free_bucket_includes_internal(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_INTERNAL_PROVIDERS", "company_llm, internal_mistral")
    fb = sl.get_free_bucket()
    assert "ollama" in fb and "company_llm" in fb and "internal_mistral" in fb


def test_cross_profile_gating(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_SUBSCRIPTION_PROVIDER", "anthropic")
    # default: active under any profile
    assert sl.is_subscription_local_active(RoutingProfile.BALANCED) is True
    # off: only the explicit SUBSCRIPTION_LOCAL profile
    monkeypatch.setenv("LLM_ROUTER_SUBSCRIPTION_REORDER_ALL_PROFILES", "off")
    assert sl.is_subscription_local_active(RoutingProfile.BALANCED) is False
    assert sl.is_subscription_local_active(RoutingProfile.SUBSCRIPTION_LOCAL) is True


def test_inactive_without_provider():
    assert sl.is_subscription_local_active(RoutingProfile.SUBSCRIPTION_LOCAL) is False


def test_pressure_threshold_clamped(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_SUBSCRIPTION_PRESSURE_THRESHOLD", "1.5")
    assert sl.get_pressure_threshold() == 1.0
    monkeypatch.setenv("LLM_ROUTER_SUBSCRIPTION_PRESSURE_THRESHOLD", "bogus")
    assert sl.get_pressure_threshold() == 0.80


async def test_pluggable_pressure_provider(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_SUBSCRIPTION_PROVIDER", "anthropic")
    assert await sl.get_subscription_pressure() is None  # no provider registered

    async def fake(): return {"anthropic": 0.9}
    sl.set_pressure_provider(fake)
    assert await sl.get_subscription_pressure() == 0.9

    async def boom(): raise RuntimeError("quota down")
    sl.set_pressure_provider(boom)
    assert await sl.get_subscription_pressure() is None  # never crashes routing


async def test_chain_builder_wiring_noop_by_default():
    """_apply_subscription_local is a no-op unless configured."""
    out = await _apply_subscription_local(CHAIN, "complex", RoutingProfile.BALANCED)
    assert out == CHAIN


async def test_chain_builder_wiring_reorders_when_configured(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_SUBSCRIPTION_PROVIDER", "anthropic")
    out = await _apply_subscription_local(CHAIN, "simple", RoutingProfile.SUBSCRIPTION_LOCAL)
    assert _providers(out)[0] == "ollama"  # free-first for simple


def test_explicit_profile_has_nonempty_base_chain():
    """Regression: SUBSCRIPTION_LOCAL must resolve to BALANCED's base chain (not empty).
    Caught in the v11 audit — an empty chain means nothing to route to."""
    from llm_router.chain_builder import _static_chain
    from llm_router.profiles import get_model_chain
    from llm_router.types import TaskType
    assert _static_chain(TaskType.CODE, RoutingProfile.SUBSCRIPTION_LOCAL), "static chain empty"
    assert get_model_chain(RoutingProfile.SUBSCRIPTION_LOCAL, TaskType.CODE), "get_model_chain empty"
    # equals BALANCED's base (reorder happens on top, no-op when unconfigured)
    assert _static_chain(TaskType.CODE, RoutingProfile.SUBSCRIPTION_LOCAL) == \
           _static_chain(TaskType.CODE, RoutingProfile.BALANCED)


def test_overlap_seat_role_wins(monkeypatch):
    """Audit (Chuzom review): if the subscription provider is also a local/free
    provider, the seat ROLE wins so tiering is unambiguous."""
    monkeypatch.setenv("LLM_ROUTER_SUBSCRIPTION_PROVIDER", "ollama")  # seat == a local provider
    chain = ["ollama/hermes3:8b", "vllm/mixtral", "openai/gpt-4o"]
    # complex: seat first → ollama(seat) before vllm(free) before openai(other)
    out = sl.reorder_for_subscription_local(
        chain, complexity="complex", profile=RoutingProfile.SUBSCRIPTION_LOCAL)
    assert _providers(out) == ["ollama", "vllm", "openai"]
