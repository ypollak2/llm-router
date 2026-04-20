"""Tests for v6.2 Quality Guard — all 5 gaps.

Verifies:
1. Quality reordering in router.py
2. model_quality_trends table creation and logging
3. Quality floor guard in model_selector.py
4. llm_quality_guard MCP tool output
5. Database indices for performance
"""

import pytest

from llm_router.cost import log_quality_trend, _get_db, get_compression_stats
from llm_router.judge import reorder_by_quality, get_judge_scores_for_model
from llm_router.model_selector import select_model, _get_quality_floor
from llm_router.types import ClassificationResult, Complexity, TaskType


@pytest.mark.asyncio
async def test_model_quality_trends_table_exists():
    """Gap 2: Verify model_quality_trends table is created."""
    db = await _get_db()
    try:
        # Try to query the table
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='model_quality_trends'"
        )
        result = await cursor.fetchone()
        assert result is not None, "model_quality_trends table should exist"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_log_quality_trend():
    """Gap 2: Verify log_quality_trend stores data correctly."""
    model = "test/gpt-4o"
    task_type = "code"
    avg_score = 0.75
    sample_count = 10

    # Log a trend
    await log_quality_trend(model, task_type, avg_score, sample_count, "stable")

    # Verify it was stored
    db = await _get_db()
    try:
        cursor = await db.execute(
            "SELECT model, task_type, avg_score, sample_count FROM model_quality_trends WHERE model = ? ORDER BY timestamp DESC LIMIT 1",
            (model,),
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == model
        assert row[1] == task_type
        assert abs(row[2] - avg_score) < 0.01  # Allow small floating point difference
        assert row[3] == sample_count
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_quality_indices_exist():
    """Gap 5: Verify quality indices are created for performance."""
    db = await _get_db()
    try:
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE '%quality%'"
        )
        indices = [row[0] for row in await cursor.fetchall()]

        # Should have at least the routing quality index and model quality trends index
        assert any("quality" in idx.lower() for idx in indices), \
            f"Should have quality indices, got: {indices}"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_reorder_by_quality_exists():
    """Gap 1: Verify reorder_by_quality function exists and is callable."""
    models = ["openai/gpt-4o", "anthropic/claude-sonnet", "gemini/gemini-pro"]

    # Should not raise an exception
    reordered = await reorder_by_quality(models, days=7)

    # Should return a list
    assert isinstance(reordered, list)
    # All models should still be present (no removal, just reordering)
    assert set(reordered) == set(models)


@pytest.mark.asyncio
async def test_quality_floor_guard_check():
    """Gap 3: Verify quality floor guard can be checked."""
    # This tests that _get_quality_floor is callable and handles missing data gracefully
    model = "anthropic/claude-sonnet"
    task_type = "code"

    result = await _get_quality_floor(model, task_type)

    # Should return None (no data) or a model string (if quality is degraded)
    assert result is None or isinstance(result, str)


@pytest.mark.asyncio
async def test_select_model_is_async():
    """Gap 3: Verify select_model is now async and supports quality floor."""
    classification = ClassificationResult(
        complexity=Complexity.MODERATE,
        confidence=0.9,
        reasoning="test",
        inferred_task_type=TaskType.CODE,
        classifier_model="test",
        classifier_cost_usd=0.0,
        classifier_latency_ms=0.0,
    )

    # Should be awaitable
    rec = await select_model(classification, budget_pct_used=0.0)

    # Should return a recommendation
    assert rec is not None
    assert hasattr(rec, "recommended_model")


@pytest.mark.asyncio
async def test_llm_quality_guard_tool():
    """Gap 4: Verify llm_quality_guard tool can be executed."""
    from llm_router.tools.admin import llm_quality_guard

    result = await llm_quality_guard(days=7)

    # Should return a string with quality information
    assert isinstance(result, str)
    # Should contain table header or "no quality data" message
    assert ("Model" in result or "quality data" in result.lower())


@pytest.mark.asyncio
async def test_judge_scores_retrieval():
    """Verify judge scores can be retrieved for models (foundation for Gap 3)."""
    model = "openai/gpt-4o"

    scores = await get_judge_scores_for_model(model, days=7)

    # Should return dict with expected keys
    assert isinstance(scores, dict)
    assert "avg_score" in scores
    assert "sample_count" in scores
    assert "model" in scores


@pytest.mark.asyncio
async def test_compression_stats_still_work():
    """Ensure our database changes don't break compression stats."""
    stats = await get_compression_stats(days=7)

    # Should return dict with compression data
    assert isinstance(stats, dict)
    # Should have the expected structure
    if stats:  # If there's any data
        assert "layers" in stats or "rtk" in stats or "total_operations" in stats


if __name__ == "__main__":
    # Run tests
    pytest.main([__file__, "-v"])
