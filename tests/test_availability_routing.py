"""Tests for availability-aware routing (latency penalties + latency stats)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest


# ── get_model_latency_stats ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_latency_stats_empty_db(tmp_path, monkeypatch):
    """Returns empty dict when no routing decisions exist."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(db_path))
    # Clear config cache so it picks up the new env var
    import llm_router.config as cfg_mod
    cfg_mod._config = None

    from llm_router.cost import get_model_latency_stats
    result = await get_model_latency_stats()
    assert result == {}


@pytest.mark.asyncio
async def test_latency_stats_requires_minimum_5_calls(tmp_path, monkeypatch):
    """Models with fewer than 5 successful calls are excluded."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(db_path))
    import llm_router.config as cfg_mod
    cfg_mod._config = None

    from llm_router.cost import _get_db, get_model_latency_stats

    # Insert 4 rows for gemini/gemini-2.5-flash — not enough
    db = await _get_db()
    for ms in [500, 600, 700, 800]:
        await db.execute(
            "INSERT INTO routing_decisions "
            "(final_model, final_provider, task_type, profile, success, latency_ms, "
            " classifier_type, classifier_confidence, classifier_latency_ms, complexity, "
            " recommended_model, base_model, was_downshifted, budget_pct_used, "
            " quality_mode, input_tokens, output_tokens, cost_usd) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("gemini/gemini-2.5-flash", "gemini", "query", "budget", 1, ms,
             "heuristic", 0.9, 0.5, "simple", "gemini/gemini-2.5-flash",
             "gemini/gemini-2.5-flash", 0, 0.1, "best", 10, 20, 0.001),
        )
    await db.commit()
    await db.close()

    result = await get_model_latency_stats()
    assert "gemini/gemini-2.5-flash" not in result


@pytest.mark.asyncio
async def test_latency_stats_calculates_p50_p95(tmp_path, monkeypatch):
    """P50 and P95 are computed correctly from sorted latency samples."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(db_path))
    import llm_router.config as cfg_mod
    cfg_mod._config = None

    from llm_router.cost import _get_db, get_model_latency_stats

    # 10 samples: 1000, 2000, ..., 10000 ms
    db = await _get_db()
    for i in range(1, 11):
        await db.execute(
            "INSERT INTO routing_decisions "
            "(final_model, final_provider, task_type, profile, success, latency_ms, "
            " classifier_type, classifier_confidence, classifier_latency_ms, complexity, "
            " recommended_model, base_model, was_downshifted, budget_pct_used, "
            " quality_mode, input_tokens, output_tokens, cost_usd) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("openai/gpt-4o", "openai", "code", "balanced", 1, i * 1000.0,
             "heuristic", 0.9, 0.5, "moderate", "openai/gpt-4o",
             "openai/gpt-4o", 0, 0.4, "best", 100, 200, 0.006),
        )
    await db.commit()
    await db.close()

    result = await get_model_latency_stats()
    assert "openai/gpt-4o" in result
    stats = result["openai/gpt-4o"]
    assert stats["count"] == 10
    # P50 = index 5 of [1000, 2000, ..., 10000] = 6000 (int(10 * 0.5) = 5 → latencies[5] = 6000)
    assert stats["p50"] == 6000.0
    # P95 = index 9 = 10000 (int(10 * 0.95) = 9 → latencies[9] = 10000)
    assert stats["p95"] == 10000.0


@pytest.mark.asyncio
async def test_latency_stats_excludes_failed_calls(tmp_path, monkeypatch):
    """Failed calls (success=0) are not included in latency calculations."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(db_path))
    import llm_router.config as cfg_mod
    cfg_mod._config = None

    from llm_router.cost import _get_db, get_model_latency_stats

    db = await _get_db()
    # 5 successful calls at 1000ms, 10 failed calls at 200000ms
    for _ in range(5):
        await db.execute(
            "INSERT INTO routing_decisions "
            "(final_model, final_provider, task_type, profile, success, latency_ms, "
            " classifier_type, classifier_confidence, classifier_latency_ms, complexity, "
            " recommended_model, base_model, was_downshifted, budget_pct_used, "
            " quality_mode, input_tokens, output_tokens, cost_usd) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("codex/gpt-5.4", "codex", "code", "balanced", 1, 1000.0,
             "heuristic", 0.9, 0.5, "moderate", "codex/gpt-5.4",
             "codex/gpt-5.4", 0, 0.5, "best", 100, 200, 0.0),
        )
    for _ in range(10):
        await db.execute(
            "INSERT INTO routing_decisions "
            "(final_model, final_provider, task_type, profile, success, latency_ms, "
            " classifier_type, classifier_confidence, classifier_latency_ms, complexity, "
            " recommended_model, base_model, was_downshifted, budget_pct_used, "
            " quality_mode, input_tokens, output_tokens, cost_usd) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("codex/gpt-5.4", "codex", "code", "balanced", 0, 200000.0,
             "heuristic", 0.9, 0.5, "moderate", "codex/gpt-5.4",
             "codex/gpt-5.4", 0, 0.5, "best", 0, 0, 0.0),
        )
    await db.commit()
    await db.close()

    result = await get_model_latency_stats()
    # Only the 5 successful calls at 1000ms count → p95 ≈ 1000ms (fast)
    assert "codex/gpt-5.4" in result
    assert result["codex/gpt-5.4"]["p95"] < 5000.0
    assert result["codex/gpt-5.4"]["count"] == 5


# ── get_model_latency_penalty ─────────────────────────────────────────────────


def test_latency_penalty_fast_model():
    """Fast models (P95 < 5s) get zero penalty."""
    import llm_router.benchmarks as bm

    bm._latency_cache = {"gemini/gemini-2.5-flash": {"p50": 800.0, "p95": 1200.0, "count": 20}}
    bm._latency_cache_ts = float("inf")  # prevent refresh

    penalty = bm.get_model_latency_penalty("gemini/gemini-2.5-flash", "query")
    assert penalty == 0.0


def test_latency_penalty_normal_model():
    """Normal models (P95 5-15s) get a small 0.03 penalty."""
    import llm_router.benchmarks as bm

    bm._latency_cache = {"openai/gpt-4o": {"p50": 5000.0, "p95": 8000.0, "count": 15}}
    bm._latency_cache_ts = float("inf")

    penalty = bm.get_model_latency_penalty("openai/gpt-4o", "code")
    assert penalty == 0.03


def test_latency_penalty_slow_model():
    """Slow models (P95 15-60s) get a 0.10 penalty."""
    import llm_router.benchmarks as bm

    bm._latency_cache = {"openai/o3": {"p50": 20000.0, "p95": 45000.0, "count": 8}}
    bm._latency_cache_ts = float("inf")

    penalty = bm.get_model_latency_penalty("openai/o3", "code")
    assert penalty == 0.10


def test_latency_penalty_codex_cold_start_default():
    """Codex with no routing history uses _COLD_START_LATENCY_MS (60s → 0.30 penalty)."""
    import llm_router.benchmarks as bm

    bm._latency_cache = {}  # no history for codex
    bm._latency_cache_ts = float("inf")

    # codex/gpt-5.4 cold start = 60_000ms → 60s < 180s → 0.30 penalty
    penalty = bm.get_model_latency_penalty("codex/gpt-5.4", "code")
    assert penalty == 0.30


def test_latency_penalty_codex_o3_cold_start():
    """codex/o3 cold start = 90_000ms → ≥ 60s < 180s → 0.30 penalty."""
    import llm_router.benchmarks as bm

    bm._latency_cache = {}
    bm._latency_cache_ts = float("inf")

    penalty = bm.get_model_latency_penalty("codex/o3", "code")
    assert penalty == 0.30


def test_latency_penalty_very_slow_model():
    """Models with P95 ≥ 180s get maximum 0.50 penalty."""
    import llm_router.benchmarks as bm

    bm._latency_cache = {"codex/gpt-5.4": {"p50": 90000.0, "p95": 200000.0, "count": 6}}
    bm._latency_cache_ts = float("inf")

    penalty = bm.get_model_latency_penalty("codex/gpt-5.4", "code")
    assert penalty == 0.50


def test_latency_penalty_unknown_model():
    """Models not in history or cold-start table get zero penalty."""
    import llm_router.benchmarks as bm

    bm._latency_cache = {}
    bm._latency_cache_ts = float("inf")

    penalty = bm.get_model_latency_penalty("some/unknown-model", "query")
    assert penalty == 0.0


# ── integration: adjusted_score includes latency penalty ─────────────────────


def test_adjusted_score_codex_penalized_below_gemini():
    """Codex (30% latency penalty) drops below Gemini Pro in BALANCED/CODE ordering."""
    import llm_router.benchmarks as bm

    # Set cold-start latency so Codex gets 0.30 penalty (60s)
    bm._latency_cache = {}
    bm._latency_cache_ts = float("inf")

    from llm_router.types import RoutingProfile, TaskType

    chain = [
        "codex/gpt-5.4",
        "gemini/gemini-2.5-pro",
        "openai/gpt-4o",
    ]
    reordered = bm.apply_benchmark_ordering(chain, TaskType.CODE, RoutingProfile.BALANCED)

    # codex should NOT be first — latency penalty pushes it down
    assert reordered[0] != "codex/gpt-5.4", (
        f"Expected codex penalised away from position 0, got: {reordered}"
    )
