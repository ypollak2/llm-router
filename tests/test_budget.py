"""Tests for budget aggregation behavior."""

from __future__ import annotations

import json
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_spend_aggregation_max_mode():
    from llm_router.budget import _api_provider_state

    cfg = SimpleNamespace(llm_router_spend_aggregation="max")
    with (
        patch("llm_router.budget.get_config", return_value=cfg),
        patch("llm_router.budget._get_cap", return_value=10.0),
        patch("llm_router.budget._get_provider_monthly_spend", new_callable=AsyncMock, return_value=1.0),
        patch("llm_router.integrations.helicone.get_helicone_spend",
              new_callable=AsyncMock, return_value={"openai": 2.0}),
        patch("llm_router.integrations.litellm_budget.is_litellm_budget_enabled", return_value=True),
        patch("llm_router.integrations.litellm_budget.get_litellm_spend",
              new_callable=AsyncMock, return_value={"openai": 3.0}),
    ):
        state = await _api_provider_state("openai")

    assert state.spend_usd == 3.0
    assert state.pressure == pytest.approx(0.3)


@pytest.mark.asyncio
async def test_spend_aggregation_sum_mode():
    from llm_router.budget import _api_provider_state

    cfg = SimpleNamespace(llm_router_spend_aggregation="sum")
    with (
        patch("llm_router.budget.get_config", return_value=cfg),
        patch("llm_router.budget._get_cap", return_value=10.0),
        patch("llm_router.budget._get_provider_monthly_spend", new_callable=AsyncMock, return_value=1.0),
        patch("llm_router.integrations.helicone.get_helicone_spend",
              new_callable=AsyncMock, return_value={"openai": 2.0}),
        patch("llm_router.integrations.litellm_budget.is_litellm_budget_enabled", return_value=True),
        patch("llm_router.integrations.litellm_budget.get_litellm_spend",
              new_callable=AsyncMock, return_value={"openai": 3.0}),
    ):
        state = await _api_provider_state("openai")

    assert state.spend_usd == 6.0
    assert state.pressure == pytest.approx(0.6)


# ── _claude_subscription_state: scale bug (highest_pressure, session/weekly/sonnet_pct) ──
#
# usage.json is written by several independent hooks that disagree on scale:
#   - hooks/session-start.py, hooks/session-end.py, hooks/usage-refresh.py all
#     divide by 100 before writing highest_pressure -> 0.0-1.0 fraction.
#   - hooks/auto-route.py's inline OAuth refresh writes highest_pressure as a
#     raw 0-100 percentage (no division).
#   - src/llm_router/quota_tracker.py reads session_pct/weekly_pct assuming
#     they are already 0.0-1.0, but the canonical hooks write them as 0-100.
# Both scales exist in the same file in production. The reader must detect
# magnitude rather than assume one convention.


def _write_usage_json(tmp_path, monkeypatch, payload: dict) -> None:
    from llm_router import paths

    home = tmp_path / "home"
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home))
    usage_path = paths.state_path("usage.json")
    usage_path.parent.mkdir(parents=True, exist_ok=True)
    usage_path.write_text(json.dumps(payload))
    return usage_path


@pytest.mark.asyncio
async def test_highest_pressure_fraction_scale_read_as_is(tmp_path, monkeypatch):
    """Canonical writers (session-start/session-end/usage-refresh) store
    highest_pressure already divided by 100 -> a 0.0-1.0 fraction. The reader
    must not divide it again."""
    from llm_router import budget

    _write_usage_json(tmp_path, monkeypatch, {"highest_pressure": 0.92, "updated_at": time.time()})
    budget.invalidate_cache("anthropic")

    state = await budget._claude_subscription_state()

    assert state.pressure == pytest.approx(0.92)


@pytest.mark.asyncio
async def test_highest_pressure_percent_scale_normalised(tmp_path, monkeypatch):
    """hooks/auto-route.py's inline refresh writes highest_pressure as a raw
    0-100 percentage (no /100 division) into the SAME usage.json. The reader
    must detect this second scale and normalise it, not just stop dividing."""
    from llm_router import budget

    _write_usage_json(tmp_path, monkeypatch, {"highest_pressure": 92, "updated_at": time.time()})
    budget.invalidate_cache("anthropic")

    state = await budget._claude_subscription_state()

    assert state.pressure == pytest.approx(0.92)


@pytest.mark.asyncio
async def test_fallback_percent_scale_session_weekly_sonnet(tmp_path, monkeypatch):
    """Fallback branch (no highest_pressure key): session_pct/weekly_pct/
    sonnet_pct as written by the canonical hooks, raw 0-100 percentages."""
    from llm_router import budget

    _write_usage_json(
        tmp_path, monkeypatch,
        {"session_pct": 40.0, "weekly_pct": 90.0, "sonnet_pct": 10.0, "updated_at": time.time()},
    )
    budget.invalidate_cache("anthropic")

    state = await budget._claude_subscription_state()

    assert state.pressure == pytest.approx(0.90)


@pytest.mark.asyncio
async def test_fallback_fraction_scale_session_weekly_sonnet(tmp_path, monkeypatch):
    """Regression guard: src/llm_router/quota_tracker.py's own writer puts
    session_pct/weekly_pct back as an already-fractional 0.0-1.0 value. The
    reader must not divide an already-fractional value a second time."""
    from llm_router import budget

    _write_usage_json(
        tmp_path, monkeypatch,
        {"session_pct": 0.40, "weekly_pct": 0.90, "sonnet_pct": 0.10, "updated_at": time.time()},
    )
    budget.invalidate_cache("anthropic")

    state = await budget._claude_subscription_state()

    assert state.pressure == pytest.approx(0.90)


# ── _claude_subscription_state: staleness bug (monotonic vs wall-clock) ──────


@pytest.mark.asyncio
async def test_stale_usage_json_returns_floor_pressure(tmp_path, monkeypatch):
    """usage.json older than the 24h staleness window must fall back to the
    stale-pressure floor (default 0.5), not the fresh value in the file.

    time.monotonic() is an arbitrary clock unrelated to the wall-clock mtime
    time.time() produced: diffing them does not measure an age. Before the
    fix, age_sec came out deeply negative and never exceeded the threshold,
    so a 48h-old file was treated as fresh.
    """
    from llm_router import budget

    usage_path = _write_usage_json(
        tmp_path, monkeypatch, {"highest_pressure": 0.01, "updated_at": time.time()},
    )
    old_mtime = time.time() - 48 * 3600
    os.utime(usage_path, (old_mtime, old_mtime))
    budget.invalidate_cache("anthropic")

    state = await budget._claude_subscription_state()

    assert state.pressure == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_fresh_usage_json_is_not_treated_as_stale(tmp_path, monkeypatch):
    """Regression guard: a just-written usage.json must NOT trip the
    staleness floor and must return the real pressure from the file."""
    from llm_router import budget

    _write_usage_json(tmp_path, monkeypatch, {"highest_pressure": 0.42, "updated_at": time.time()})
    budget.invalidate_cache("anthropic")

    state = await budget._claude_subscription_state()

    assert state.pressure == pytest.approx(0.42)


# ── Integration: at weekly=90%, reorder_for_pressure puts Claude last ────────


@pytest.mark.asyncio
async def test_weekly_90pct_pressure_puts_claude_last_in_balanced_chain(tmp_path, monkeypatch):
    """At 90% weekly quota (>= the documented 85% 'Claude last' threshold),
    the real get_budget_state('anthropic') pressure fed into the real
    profiles.reorder_for_pressure() for the BALANCED profile must push the
    Claude model to the back of the chain, not leave it first.

    This is the live-behaviour change the fix delivers: at >=85% Claude
    subscription quota, llm() BALANCED chains no longer try Claude first.
    """
    from llm_router import budget
    from llm_router.profiles import reorder_for_pressure
    from llm_router.types import RoutingProfile

    _write_usage_json(tmp_path, monkeypatch, {"weekly_pct": 90.0, "session_pct": 10.0, "updated_at": time.time()})
    budget.invalidate_cache("anthropic")

    state = await budget.get_budget_state("anthropic")
    assert state.pressure == pytest.approx(0.90)

    chain = ["anthropic/claude-sonnet-4-6", "openai/gpt-4o-mini", "gemini/gemini-2.5-flash"]
    reordered = reorder_for_pressure(chain, state.pressure, RoutingProfile.BALANCED)

    assert reordered[-1] == "anthropic/claude-sonnet-4-6"
    assert reordered.index("anthropic/claude-sonnet-4-6") > reordered.index("openai/gpt-4o-mini")
