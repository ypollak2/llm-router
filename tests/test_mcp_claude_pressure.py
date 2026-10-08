"""P0.2 (plan v16): the MCP process sees real Claude quota pressure.

Bug: ``claude_usage.get_claude_pressure()`` returned the in-process cache, and
only ``set_claude_pressure`` (called by the ``llm_update_usage`` tool) ever filled
it. An MCP server that never saw that tool call ran with pressure 0.0 for its
whole life, so routing never demoted Claude even with usage.json at 96%.

Fix: ``get_claude_pressure_reading()`` falls back to usage.json (through
``budget._pressure_from_usage``) when ``set_claude_pressure`` has not run in the
last 300 s, and reports ``stale`` / ``unknown`` instead of a number it cannot
trust. Stale or unknown pressure reads as 0.0, the pre-fix default, so the chain
is not reordered by pressure.
"""

from __future__ import annotations

import json
import time

import pytest

from llm_router import claude_usage
from llm_router.types import Complexity, RoutingProfile, TaskType


@pytest.fixture(autouse=True)
def _fresh_pressure_state(monkeypatch):
    """No cached reading and no recent set_claude_pressure from another test."""
    monkeypatch.setattr(claude_usage, "_cached_pressure", 0.0)
    monkeypatch.setattr(claude_usage, "_cached_pressure_set_at", None, raising=False)
    reset = getattr(claude_usage, "_reset_pressure_reading_cache", None)
    if reset is not None:
        reset()
    yield
    if reset is not None:
        reset()


def _write_usage(*, session_pct: float, weekly_pct: float = 10.0, age_s: float = 0.0) -> None:
    from llm_router import paths

    path = paths.state_path("usage.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    hp = max(session_pct, weekly_pct) / 100.0
    path.write_text(json.dumps({
        "session_pct": session_pct,
        "weekly_pct": weekly_pct,
        "sonnet_pct": 0.0,
        "highest_pressure": hp,
        "updated_at": time.time() - age_s,
    }))


# ── P0.2-a: the reading ───────────────────────────────────────────────────────


def test_fresh_usage_json_is_read_when_nothing_was_pushed():
    _write_usage(session_pct=96.0)
    value, state, as_of = claude_usage.get_claude_pressure_reading()
    assert state == "ok"
    assert value == pytest.approx(0.96, abs=0.01)
    assert as_of is not None


def test_get_claude_pressure_is_no_longer_zero_for_life():
    """The bug itself: usage.json at 96%, no llm_update_usage call → was 0.0."""
    _write_usage(session_pct=96.0)
    assert claude_usage.get_claude_pressure() == pytest.approx(0.96, abs=0.01)


def test_fresh_push_wins_over_the_file():
    _write_usage(session_pct=96.0)
    claude_usage.set_claude_pressure(0.3)
    value, state, _ = claude_usage.get_claude_pressure_reading()
    assert (value, state) == (pytest.approx(0.3), "ok")
    assert claude_usage.get_claude_pressure() == pytest.approx(0.3)


def test_push_older_than_300s_falls_back_to_the_file(monkeypatch):
    _write_usage(session_pct=96.0)
    claude_usage.set_claude_pressure(0.3)
    monkeypatch.setattr(claude_usage, "_cached_pressure_set_at", time.time() - 301)
    value, state, _ = claude_usage.get_claude_pressure_reading()
    assert state == "ok"
    assert value == pytest.approx(0.96, abs=0.01)


def test_stale_file_is_unknown_and_reads_as_zero():
    _write_usage(session_pct=96.0, age_s=31 * 60)
    value, state, as_of = claude_usage.get_claude_pressure_reading()
    assert value is None
    assert state == "stale"
    assert as_of is not None
    assert claude_usage.get_claude_pressure() == 0.0


def test_missing_file_is_unknown():
    value, state, as_of = claude_usage.get_claude_pressure_reading()
    assert (value, state, as_of) == (None, "unknown", None)
    assert claude_usage.get_claude_pressure() == 0.0


def test_unreadable_file_is_unknown():
    from llm_router import paths

    path = paths.state_path("usage.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    value, state, _ = claude_usage.get_claude_pressure_reading()
    assert (value, state) == (None, "unknown")


def test_file_reading_is_cached_for_60s():
    _write_usage(session_pct=96.0)
    first = claude_usage.get_claude_pressure_reading()
    _write_usage(session_pct=20.0)
    assert claude_usage.get_claude_pressure_reading() == first


# ── Chain order through the real reading (P0.2-a stale, P0.2-c) ─────────────


def _cfg():
    from llm_router.config import get_config
    return get_config()


async def _code_chain(monkeypatch) -> list[str]:
    import llm_router.config as config_module
    from llm_router.router import _build_and_filter_chain

    # Claude via API key, so the chain holds Claude and Codex to compare.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    config_module._config = None
    monkeypatch.setattr("llm_router.router.is_codex_available", lambda: True)
    monkeypatch.setattr("llm_router.router.is_gemini_cli_available", lambda: False)
    monkeypatch.setattr("llm_router.router._disabled_subprocess_backends", lambda: frozenset())
    return await _build_and_filter_chain(
        TaskType.CODE, RoutingProfile.BALANCED, None, "moderate", Complexity.MODERATE, _cfg()
    )


@pytest.mark.asyncio
async def test_high_pressure_puts_codex_before_claude(mock_env, monkeypatch):
    """P0.2-c: usage.json at 96% → an eligible non-Claude provider leads."""
    _write_usage(session_pct=96.0)
    chain = await _code_chain(monkeypatch)
    print(f"chain head at 0.96: {chain[:4]}")
    assert chain, "empty chain proves nothing"
    assert any(m.startswith("anthropic/") for m in chain), "need Claude in the chain to compare"
    first_claude = next(i for i, m in enumerate(chain) if m.startswith("anthropic/"))
    first_codex = next(i for i, m in enumerate(chain) if m.startswith("codex/"))
    assert not chain[0].startswith("anthropic/")
    assert first_codex < first_claude


@pytest.mark.asyncio
async def test_stale_pressure_leaves_the_order_unchanged(mock_env, monkeypatch):
    """Stale 96% must not reorder: same chain as no reading at all."""
    baseline = await _code_chain(monkeypatch)
    claude_usage._reset_pressure_reading_cache()
    _write_usage(session_pct=96.0, age_s=31 * 60)
    stale = await _code_chain(monkeypatch)
    assert baseline
    assert stale == baseline
    assert baseline[0].startswith("anthropic/"), f"low-pressure default leads with Claude: {baseline[:3]}"
