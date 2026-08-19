"""#26 — opt-in leaderboard ordering for the DYNAMIC routing table.

The static path already reorders non-BUDGET chains by live-leaderboard quality
(cheapest-capable-first); the dynamic table did not, so discovery-active BALANCED/
PREMIUM chains fell back to raw static preference order. This adds the SAME ordering,
gated on LLM_ROUTER_DYNAMIC_LEADERBOARD_ORDERING and DEFAULT OFF so the audited/QUALIFIED
default is preserved byte-for-byte. BUDGET is never reordered (cheap-first is the
cost-saving behaviour).
"""
from __future__ import annotations

import llm_router.benchmarks as bm
from llm_router.dynamic_routing import build_dynamic_routing_table
from llm_router.profiles import ROUTING_TABLE, RoutingProfile

_PROVIDERS = {"openai", "anthropic", "google", "deepseek", "codex", "ollama", "gemini_cli"}


def _a_nonbudget_multi_model_key():
    """Find a (BALANCED/PREMIUM, task) chain with >=2 models available under _PROVIDERS."""
    from llm_router.profiles import provider_from_model
    for (profile, task), chain in ROUTING_TABLE.items():
        if profile == RoutingProfile.BUDGET:
            continue
        avail = [m for m in chain if provider_from_model(m) in _PROVIDERS]
        if len(avail) >= 2:
            return profile, task, avail
    return None


def test_default_off_is_a_noop(monkeypatch):
    """With the flag unset, the dynamic table must NOT apply benchmark ordering —
    it equals the filter+quota build (audited behaviour). We prove it by making
    apply_benchmark_ordering a tripwire that must never be called."""
    monkeypatch.delenv("LLM_ROUTER_DYNAMIC_LEADERBOARD_ORDERING", raising=False)

    def _tripwire(*a, **k):  # pragma: no cover - must not run
        raise AssertionError("apply_benchmark_ordering called with flag OFF")

    monkeypatch.setattr(bm, "apply_benchmark_ordering", _tripwire)
    table = build_dynamic_routing_table(available_providers=set(_PROVIDERS))
    assert table  # built fine, tripwire never fired


def test_flag_on_reorders_nonbudget_not_budget(monkeypatch):
    """With the flag on, non-BUDGET chains pass through apply_benchmark_ordering
    (here stubbed to reverse), BUDGET chains do not."""
    monkeypatch.setenv("LLM_ROUTER_DYNAMIC_LEADERBOARD_ORDERING", "on")
    monkeypatch.setattr(bm, "apply_benchmark_ordering",
                        lambda chain, task_type, profile: list(reversed(chain)))

    found = _a_nonbudget_multi_model_key()
    assert found, "expected a non-BUDGET multi-model chain in ROUTING_TABLE"
    profile, task, avail_static = found

    table = build_dynamic_routing_table(available_providers=set(_PROVIDERS))
    # non-BUDGET chain was reversed by the stub (modulo quota pressure, absent here)
    assert table[(profile, task)] == list(reversed(avail_static))

    # a BUDGET chain for the same task is NOT reversed
    from llm_router.profiles import provider_from_model
    budget_key = (RoutingProfile.BUDGET, task)
    if budget_key in ROUTING_TABLE:
        budget_static = [m for m in ROUTING_TABLE[budget_key]
                         if provider_from_model(m) in _PROVIDERS]
        assert table[budget_key] == budget_static, "BUDGET must keep cheap-first order"


def test_flag_on_fails_open_without_benchmark_data(monkeypatch):
    """Real apply_benchmark_ordering returns the chain unchanged when no benchmark
    data is present — so enabling the flag on a machine without benchmarks.json is
    safe (no crash, no reorder)."""
    monkeypatch.setenv("LLM_ROUTER_DYNAMIC_LEADERBOARD_ORDERING", "on")
    monkeypatch.setattr(bm, "get_benchmark_data", lambda: None)
    table = build_dynamic_routing_table(available_providers=set(_PROVIDERS))
    assert table  # built without error
