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
# Scales are fixed per field, not guessed:
#   highest_pressure          0-1   (session-start, session-end, usage-refresh, auto-route)
#   session/weekly/sonnet_pct 0-100
# Files written by the OLD hooks/auto-route.py hold highest_pressure on 0-100;
# those are recognised against the *_pct fields. Invariant: a real 1% must
# never read as 100% (that would skip Claude as "budget exhausted").


def _write_usage_json(tmp_path, monkeypatch, payload: dict) -> None:
    from llm_router import paths

    home = tmp_path / "home"
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home))
    usage_path = paths.state_path("usage.json")
    usage_path.parent.mkdir(parents=True, exist_ok=True)
    usage_path.write_text(json.dumps(payload))
    return usage_path


async def _pressure(tmp_path, monkeypatch, payload: dict) -> float:
    from llm_router import budget

    _write_usage_json(tmp_path, monkeypatch, {**payload, "updated_at": time.time()})
    budget.invalidate_cache("anthropic")
    return (await budget._claude_subscription_state()).pressure


@pytest.mark.asyncio
async def test_highest_pressure_is_read_as_a_0_1_fraction(tmp_path, monkeypatch):
    """Canonical writers store highest_pressure already divided by 100; the
    reader must not divide it again (92% used to read 0.0092)."""
    assert await _pressure(tmp_path, monkeypatch, {"highest_pressure": 0.92}) == pytest.approx(0.92)


@pytest.mark.asyncio
async def test_current_writer_file_round_trips(tmp_path, monkeypatch):
    """A file as written today: *_pct on 0-100, highest_pressure on 0-1."""
    p = await _pressure(
        tmp_path, monkeypatch,
        {"session_pct": 40.0, "weekly_pct": 92.0, "sonnet_pct": 10.0, "highest_pressure": 0.92},
    )
    assert p == pytest.approx(0.92)


@pytest.mark.asyncio
async def test_real_one_percent_session_right_after_reset_is_not_exhausted(tmp_path, monkeypatch):
    """A real 1% session just after a quota reset must read 0.01: no provider
    skip (>= 1.0) and no premium cap (>= 0.85)."""
    p = await _pressure(
        tmp_path, monkeypatch,
        {"session_pct": 1.0, "weekly_pct": 0.0, "sonnet_pct": 0.0, "highest_pressure": 0.01},
    )
    assert p == pytest.approx(0.01)
    assert p < 0.85 < 1.0


@pytest.mark.asyncio
async def test_pct_fields_alone_are_read_as_0_100(tmp_path, monkeypatch):
    """No highest_pressure key: session/weekly/sonnet_pct are 0-100, so a
    1.0 is 1%, not 100%."""
    assert await _pressure(tmp_path, monkeypatch, {"session_pct": 1.0}) == pytest.approx(0.01)
    assert await _pressure(
        tmp_path, monkeypatch, {"session_pct": 40.0, "weekly_pct": 90.0, "sonnet_pct": 10.0},
    ) == pytest.approx(0.90)


@pytest.mark.asyncio
async def test_legacy_0_100_highest_pressure_file_is_read_correctly(tmp_path, monkeypatch):
    """Files written before the fix by hooks/auto-route.py carry
    highest_pressure on 0-100 (unrounded). Consistent with the *_pct fields,
    92 means 92%."""
    p = await _pressure(
        tmp_path, monkeypatch,
        {"session_pct": 92.0, "weekly_pct": 30.0, "sonnet_pct": 0.0, "highest_pressure": 92.0},
    )
    assert p == pytest.approx(0.92)


@pytest.mark.asyncio
async def test_legacy_file_with_real_one_percent_never_reads_as_100(tmp_path, monkeypatch):
    """The dangerous legacy case: old auto-route wrote highest_pressure=1.0
    for a real 1%. 1.0 is a valid 0-1 value (100%), but it contradicts
    session_pct=1.0, so pressure is recomputed from the *_pct fields."""
    p = await _pressure(
        tmp_path, monkeypatch,
        {"session_pct": 1.0, "weekly_pct": 0.3, "sonnet_pct": 0.0, "highest_pressure": 1.0},
    )
    assert p == pytest.approx(0.01)


@pytest.mark.asyncio
async def test_legacy_0_100_highest_pressure_without_pct_fields_fails_open(tmp_path, monkeypatch):
    """highest_pressure > 1 with nothing to check it against is unknown, not a
    guess."""
    from llm_router import budget

    before = budget._failopen_count
    assert await _pressure(tmp_path, monkeypatch, {"highest_pressure": 92}) == 0.0
    assert budget._failopen_count == before + 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"session_pct": 250.0},
        {"weekly_pct": -5.0},
        {"session_pct": "lots"},
        {"session_pct": 10.0, "sonnet_pct": float("nan")},
        {"highest_pressure": 150},
        {"highest_pressure": -0.2},
        {"highest_pressure": float("nan")},
        {"highest_pressure": "0.9"},
    ],
)
async def test_out_of_range_value_fails_open_and_is_recorded(tmp_path, monkeypatch, payload):
    """An out-of-range or non-numeric value is unknown: no skip, no cap
    (pressure 0.0), and the fail-open is counted."""
    from llm_router import budget

    before = budget._failopen_count
    assert await _pressure(tmp_path, monkeypatch, payload) == 0.0
    assert budget._failopen_count == before + 1


def _load_auto_route_hook():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "src" / "llm_router" / "hooks" / "auto-route.py"
    spec = importlib.util.spec_from_file_location("auto_route_pressure_scale", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_auto_route_writes_highest_pressure_on_0_1_scale(tmp_path, monkeypatch):
    """The source of the second scale: hooks/auto-route.py's inline refresh
    must write highest_pressure as a 0-1 fraction like every other writer."""
    import io
    import sys

    hook = _load_auto_route_hook()
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        hook.subprocess, "run",
        lambda *a, **k: SimpleNamespace(
            returncode=0, stdout=json.dumps({"claudeAiOauth": {"accessToken": "t"}}),
        ),
    )
    payload = {
        "five_hour": {"utilization": 1.0},
        "seven_day": {"utilization": 0.3},
        "seven_day_sonnet": {"utilization": 0.0},
    }

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(
        hook.urllib.request, "urlopen", lambda *a, **k: _Resp(json.dumps(payload).encode()),
    )

    result = hook._fetch_usage_inline()

    assert result is not None
    assert result["session_pct"] == 1.0
    assert result["highest_pressure"] == pytest.approx(0.01)
    on_disk = json.loads((tmp_path / "home" / "usage.json").read_text())
    assert on_disk["highest_pressure"] == pytest.approx(0.01)


def test_hooks_mirror_of_auto_route_is_byte_identical():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    assert (root / "hooks" / "auto-route.py").read_bytes() == (
        root / "src" / "llm_router" / "hooks" / "auto-route.py"
    ).read_bytes()


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
