"""RETROSPECTIVE B-7 / M-2 — honest "real dollars avoided" beside the baseline.

For free-provider calls under a flat-rate Claude Code subscription, llm_router
booked the FULL Opus baseline as "saved" — but the marginal cost of the host
Opus call on a subscription is ~$0, so no real dollars were avoided. The fix
adds a second, clearly-labelled figure:

  - baseline_avoided_usd : Opus-baseline vs actual (a quota/token-smoothing #).
  - real_dollars_avoided_usd : dollars the user would ACTUALLY have paid.
        ~$0 on a subscription; the full baseline only in metered API mode.

These tests pin both the SessionSpend properties and the get_savings_by_period
aggregate under both subscription and metered modes.
"""
from __future__ import annotations

import llm_router.config as config_module
import pytest
from llm_router import cost
from llm_router.session_spend import SessionSpend
from llm_router.types import LLMResponse, RoutingProfile, TaskType


def _free_local_session() -> SessionSpend:
    s = SessionSpend()
    s.opus_equivalent_usd = 74.15  # a large Opus-baseline figure
    s.total_usd = 0.0              # free-local: $0 actually spent
    return s


def test_session_subscription_default_real_is_zero(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_CLAUDE_SUBSCRIPTION", raising=False)
    s = _free_local_session()
    # Baseline-avoided is large, but real cash avoided is $0 on a subscription.
    assert s.baseline_avoided_usd == 74.15
    assert s.real_dollars_avoided_usd == 0.0


def test_session_explicit_subscription_true_real_is_zero(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_CLAUDE_SUBSCRIPTION", "true")
    assert _free_local_session().real_dollars_avoided_usd == 0.0


def test_session_metered_mode_real_equals_baseline(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_CLAUDE_SUBSCRIPTION", "false")
    s = _free_local_session()
    assert cost._host_is_metered() is True
    assert s.real_dollars_avoided_usd == s.baseline_avoided_usd == 74.15


def test_summary_exposes_both_figures(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_CLAUDE_SUBSCRIPTION", raising=False)
    d = _free_local_session().get_summary()
    assert d["baseline_avoided_usd"] == 74.15
    assert d["real_dollars_avoided_usd"] == 0.0


@pytest.mark.asyncio
async def test_period_aggregate_real_zero_on_subscription(temp_db, monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_CLAUDE_SUBSCRIPTION", raising=False)
    config_module._config = None
    free = LLMResponse(content="x", model="ollama/qwen3", input_tokens=1000,
                       output_tokens=2000, cost_usd=0.0, latency_ms=10, provider="ollama")
    await cost.log_usage(free, TaskType.QUERY, RoutingProfile.BUDGET)

    data = await cost.get_savings_by_period()
    wk = data["week"]
    # baseline_avoided > 0 (we routed away from an Opus call) but real $ = 0.
    assert wk["baseline_avoided_usd"] > 0.0
    assert wk["baseline_avoided_usd"] == wk["saved_usd"]  # back-compat alias
    assert wk["real_dollars_avoided_usd"] == 0.0


@pytest.mark.asyncio
async def test_period_aggregate_real_positive_when_metered(temp_db, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_CLAUDE_SUBSCRIPTION", "false")
    config_module._config = None
    free = LLMResponse(content="x", model="ollama/qwen3", input_tokens=1000,
                       output_tokens=2000, cost_usd=0.0, latency_ms=10, provider="ollama")
    await cost.log_usage(free, TaskType.QUERY, RoutingProfile.BUDGET)

    wk = (await cost.get_savings_by_period())["week"]
    # In metered API mode the avoided Opus call really would have been billed.
    assert wk["real_dollars_avoided_usd"] == wk["baseline_avoided_usd"] > 0.0


# ── AC-2 / INV-COST-006: get_team_savings must carry the same host-mode split ──
@pytest.mark.asyncio
async def test_team_savings_real_zero_on_subscription(temp_db, monkeypatch):
    """Regression for audit P0-2 / AC-2: the team report (which team.py broadcasts to
    Slack/Discord/Telegram) must NOT present baseline-avoided as cash on a subscription."""
    monkeypatch.delenv("LLM_ROUTER_CLAUDE_SUBSCRIPTION", raising=False)
    config_module._config = None
    free = LLMResponse(content="x", model="ollama/qwen3", input_tokens=1000,
                       output_tokens=2000, cost_usd=0.0, latency_ms=10, provider="ollama")
    await cost.log_usage(free, TaskType.QUERY, RoutingProfile.BUDGET)

    data = await cost.get_team_savings(period="week")
    assert data["baseline_equivalent_avoided_usd"] > 0.0        # counterfactual is real
    assert data["real_dollars_avoided_usd"] == 0.0             # but no cash on subscription
    assert data["saved_usd"] == data["baseline_equivalent_avoided_usd"]  # back-compat alias


@pytest.mark.asyncio
async def test_team_savings_real_positive_when_metered(temp_db, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_CLAUDE_SUBSCRIPTION", "false")
    config_module._config = None
    free = LLMResponse(content="x", model="ollama/qwen3", input_tokens=1000,
                       output_tokens=2000, cost_usd=0.0, latency_ms=10, provider="ollama")
    await cost.log_usage(free, TaskType.QUERY, RoutingProfile.BUDGET)

    data = await cost.get_team_savings(period="week")
    assert data["real_dollars_avoided_usd"] == data["baseline_equivalent_avoided_usd"] > 0.0
