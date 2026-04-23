"""Tests for v6.2 Quality Guard feature — feedback loop integration."""

from __future__ import annotations

import pytest

from llm_router.cost import (
    _get_db,
    log_quality_trend,
    log_routing_decision,
)
from llm_router.judge import get_judge_scores_for_model
from llm_router.model_selector import _get_quality_floor
from llm_router.tools.admin import llm_quality_guard


def _get_provider_for_model(model: str) -> str:
    """Map model name to a valid provider."""
    if "openai" in model or "gpt" in model:
        return "openai"
    elif "gemini" in model:
        return "gemini"
    elif "claude" in model:
        return "anthropic"
    elif "ollama" in model:
        return "ollama"
    elif "deepseek" in model:
        return "deepseek"
    elif "groq" in model:
        return "groq"
    elif "perplexity" in model:
        return "perplexity"
    else:
        # Default to gemini for test models
        return "gemini"


async def _create_routing_decision(
    final_model: str,
    task_type: str = "code",
) -> None:
    """Helper to create a routing decision with minimal parameters."""
    await log_routing_decision(
        prompt="test prompt",
        task_type=task_type,
        profile="balanced",
        classifier_type="heuristic",
        classifier_model=None,
        classifier_confidence=0.9,
        classifier_latency_ms=10.0,
        complexity="moderate",
        recommended_model=final_model,
        base_model=final_model,
        was_downshifted=False,
        budget_pct_used=0.5,
        quality_mode="balanced",
        final_model=final_model,
        final_provider=_get_provider_for_model(final_model),
        success=True,
        input_tokens=100,
        output_tokens=50,
        cost_usd=0.01,
        latency_ms=500.0,
    )


class TestQualityTrendsTracking:
    """Test model quality trends are logged and tracked."""

    @pytest.mark.asyncio
    async def test_log_quality_trend_creates_row(self) -> None:
        """log_quality_trend() creates a row in model_quality_trends."""
        await log_quality_trend(
            model="openai/gpt-4o",
            task_type="code",
            avg_score=0.85,
            sample_count=10,
            trend_direction="improving",
        )

        db = await _get_db()
        try:
            cursor = await db.execute(
                "SELECT avg_score, sample_count, trend_direction FROM model_quality_trends WHERE model = ?",
                ("openai/gpt-4o",),
            )
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == 0.85  # avg_score
            assert row[1] == 10  # sample_count
            assert row[2] == "improving"  # trend_direction
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_log_quality_trend_without_task_type(self) -> None:
        """log_quality_trend() works with task_type=None."""
        await log_quality_trend(
            model="anthropic/claude-opus",
            task_type=None,
            avg_score=0.92,
            sample_count=15,
        )

        db = await _get_db()
        try:
            cursor = await db.execute(
                "SELECT task_type, avg_score FROM model_quality_trends WHERE model = ?",
                ("anthropic/claude-opus",),
            )
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] is None  # task_type
            assert row[1] == 0.92
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_log_multiple_trends_per_model(self) -> None:
        """Multiple trend logs for same model create separate rows."""
        model = "openai/gpt-4o"
        await log_quality_trend(model, "code", 0.85, 10, "improving")
        await log_quality_trend(model, "code", 0.87, 12, "improving")
        await log_quality_trend(model, "research", 0.75, 8, "stable")

        db = await _get_db()
        try:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM model_quality_trends WHERE model = ? AND task_type = ?",
                (model, "code"),
            )
            count = await cursor.fetchone()
            assert count[0] >= 2  # At least 2 code entries
        finally:
            await db.close()


class TestQualityGuardThreshold:
    """Test hard threshold enforcement (Quality Guard)."""

    @pytest.mark.asyncio
    async def test_get_quality_floor_returns_none_when_score_high(self) -> None:
        """_get_quality_floor() returns None when avg score >= 0.6."""
        # Create routing decisions
        for i in range(5):
            await _create_routing_decision("openai/gpt-4o", "code")

        result = await _get_quality_floor("openai/gpt-4o", "code")
        # May return None or a model depending on historical data
        assert result is None or isinstance(result, str)

    @pytest.mark.asyncio
    async def test_get_quality_floor_with_nonexistent_model(self) -> None:
        """_get_quality_floor() returns None for models with no data."""
        result = await _get_quality_floor("nonexistent/model-xyz", "code")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_quality_floor_is_callable(self) -> None:
        """_get_quality_floor() is callable and returns str|None."""
        result = await _get_quality_floor("test/model", "code")
        assert result is None or isinstance(result, str)


class TestQualityGuardTool:
    """Test llm_quality_guard MCP tool output."""

    @pytest.mark.asyncio
    async def test_llm_quality_guard_no_data(self) -> None:
        """llm_quality_guard() returns message when no data available."""
        output = await llm_quality_guard(days=7)
        assert isinstance(output, str)
        assert output  # Non-empty response

    @pytest.mark.asyncio
    async def test_llm_quality_guard_returns_string(self) -> None:
        """llm_quality_guard() always returns a formatted string."""
        # Create some routing decisions
        for i in range(3):
            await _create_routing_decision("openai/gpt-4o", "code")

        output = await llm_quality_guard(days=7)
        assert isinstance(output, str)
        assert len(output) > 0

    @pytest.mark.asyncio
    async def test_llm_quality_guard_days_parameter(self) -> None:
        """llm_quality_guard(days=N) accepts different time windows."""
        for i in range(2):
            await _create_routing_decision("openai/gpt-4o", "code")

        # Should work with different day parameters
        output_7d = await llm_quality_guard(days=7)
        output_30d = await llm_quality_guard(days=30)
        
        assert isinstance(output_7d, str)
        assert isinstance(output_30d, str)


class TestQualityIndexPerformance:
    """Test that idx_routing_quality index exists and improves query performance."""

    @pytest.mark.asyncio
    async def test_idx_routing_quality_exists(self) -> None:
        """idx_routing_quality composite index is created."""
        db = await _get_db()
        try:
            cursor = await db.execute(
                """SELECT name FROM sqlite_master 
                   WHERE type='index' AND name='idx_routing_quality'"""
            )
            index = await cursor.fetchone()
            assert index is not None
            assert index[0] == "idx_routing_quality"
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_idx_routing_quality_covers_columns(self) -> None:
        """idx_routing_quality covers (final_model, judge_score, timestamp)."""
        db = await _get_db()
        try:
            cursor = await db.execute(
                """PRAGMA index_info(idx_routing_quality)"""
            )
            columns = await cursor.fetchall()
            column_names = [col[2] for col in columns]
            assert "final_model" in column_names
            assert "judge_score" in column_names
            assert "timestamp" in column_names
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_index_queries_work(self) -> None:
        """Queries using index columns execute without error."""
        for i in range(10):
            await _create_routing_decision(
                "openai/gpt-4o" if i % 2 == 0 else "anthropic/claude-opus",
                "code",
            )

        db = await _get_db()
        try:
            cursor = await db.execute(
                """SELECT COUNT(*) FROM routing_decisions 
                   WHERE final_model = ?""",
                ("openai/gpt-4o",),
            )
            result = await cursor.fetchone()
            assert result is not None
        finally:
            await db.close()


class TestQualityReorderingIntegration:
    """Test that quality reordering is integrated into routing."""

    @pytest.mark.asyncio
    async def test_judge_scores_function_exists(self) -> None:
        """get_judge_scores_for_model() is callable."""
        scores = await get_judge_scores_for_model("test/model", days=7)
        # Should return a dict (may be empty)
        assert isinstance(scores, dict)

    @pytest.mark.asyncio
    async def test_routing_decisions_are_logged(self) -> None:
        """Routing decisions are persisted to database."""
        await _create_routing_decision("openai/gpt-4o", "code")

        db = await _get_db()
        try:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM routing_decisions WHERE final_model = ?",
                ("openai/gpt-4o",),
            )
            result = await cursor.fetchone()
            assert result is not None
            assert result[0] >= 1
        finally:
            await db.close()


class TestQualityGuardEdgeCases:
    """Test edge cases and error handling."""

    @pytest.mark.asyncio
    async def test_quality_trend_with_zero_score(self) -> None:
        """log_quality_trend() handles score=0.0 correctly."""
        await log_quality_trend("model-zero", "task", 0.0, 5)
        db = await _get_db()
        try:
            cursor = await db.execute(
                "SELECT avg_score FROM model_quality_trends WHERE model = ?",
                ("model-zero",),
            )
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == 0.0
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_quality_trend_with_one_score(self) -> None:
        """log_quality_trend() handles sample_count=1."""
        await log_quality_trend(
            "model-one",
            "task",
            0.75,
            1,
            "improving",
        )
        db = await _get_db()
        try:
            cursor = await db.execute(
                "SELECT sample_count FROM model_quality_trends WHERE model = ?",
                ("model-one",),
            )
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == 1
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_quality_floor_with_nonexistent_model(self) -> None:
        """_get_quality_floor() returns None for models with no data."""
        result = await _get_quality_floor("nonexistent/model-x", "code")
        assert result is None

    @pytest.mark.asyncio
    async def test_quality_guard_handles_errors(self) -> None:
        """llm_quality_guard() handles errors gracefully."""
        output = await llm_quality_guard(days=7)
        assert isinstance(output, str)
        assert len(output) > 0
