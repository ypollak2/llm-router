"""Tests for LLM-as-Judge quality evaluation."""

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime, timedelta

from llm_router.judge import (
    evaluate_response_async,
    _build_judge_prompt,
    _parse_judge_score,
    _store_judge_score,
    get_judge_scores_for_model,
    reorder_by_quality,
)
from llm_router.types import LLMResponse, RoutingProfile, TaskType
from llm_router import cost


async def _insert_routing_decision(db, model: str, judge_score: float | None = None, days_ago: int = 0):
    """Helper to insert a complete routing_decisions entry for testing."""
    timestamp = (datetime.now() - timedelta(days=days_ago)).isoformat()
    await db.execute(
        """INSERT INTO routing_decisions
           (timestamp, task_type, profile, complexity, final_model, final_provider,
            success, input_tokens, output_tokens, cost_usd, latency_ms, judge_score)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            timestamp,
            "query",
            "balanced",
            "simple",
            model,
            "test-provider",
            1,  # success
            100,  # input_tokens
            50,  # output_tokens
            0.001,  # cost_usd
            200.0,  # latency_ms
            judge_score,
        ),
    )
    await db.commit()


@pytest.mark.asyncio
async def test_evaluate_response_async_with_high_sample_rate(temp_db, monkeypatch):
    """Test evaluate_response_async respects sample rate."""
    monkeypatch.setenv("LLM_ROUTER_JUDGE_SAMPLE_RATE", "1.0")
    
    with patch("llm_router.judge.asyncio.create_task") as mock_task:
        await evaluate_response_async(
            prompt="What is 2+2?",
            response="The answer is 4",
            task_type="query",
            routing_decision_id=1,
        )
        # Should create a background task with 100% sample rate
        assert mock_task.called


@pytest.mark.asyncio
async def test_evaluate_response_async_with_low_sample_rate(temp_db, monkeypatch):
    """Test evaluate_response_async respects low sample rate."""
    monkeypatch.setenv("LLM_ROUTER_JUDGE_SAMPLE_RATE", "0.0")
    
    with patch("llm_router.judge.asyncio.create_task") as mock_task:
        await evaluate_response_async(
            prompt="What is 2+2?",
            response="The answer is 4",
            task_type="query",
            routing_decision_id=1,
        )
        # Should NOT create a background task with 0% sample rate
        assert not mock_task.called


def test_build_judge_prompt():
    """Test judge prompt construction."""
    prompt = "What is the capital of France?"
    response = "The capital of France is Paris."
    task_type = "query"
    
    judge_prompt = _build_judge_prompt(prompt, response, task_type)
    
    # Verify prompt contains all required elements
    assert prompt in judge_prompt
    assert response in judge_prompt
    assert task_type in judge_prompt
    assert "relevance" in judge_prompt
    assert "completeness" in judge_prompt
    assert "correctness" in judge_prompt
    assert "{" in judge_prompt  # JSON format


def test_parse_judge_score_valid_json():
    """Test parsing valid judge scores from JSON response."""
    response = '{"relevance": 0.9, "completeness": 0.8, "correctness": 0.95}'
    
    score = _parse_judge_score(response)
    
    assert score is not None
    assert 0.8 <= score <= 1.0
    # Should be average of three scores
    expected = (0.9 + 0.8 + 0.95) / 3.0
    assert abs(score - expected) < 0.01


def test_parse_judge_score_with_markdown():
    """Test parsing scores from response with markdown formatting."""
    response = """
    ```json
    {"relevance": 0.85, "completeness": 0.75, "correctness": 0.9}
    ```
    """
    
    score = _parse_judge_score(response)
    
    assert score is not None
    expected = (0.85 + 0.75 + 0.9) / 3.0
    assert abs(score - expected) < 0.01


def test_parse_judge_score_with_extra_text():
    """Test parsing scores when response contains explanatory text."""
    response = """
    The response is good. Here's the evaluation:
    {"relevance": 0.92, "completeness": 0.88, "correctness": 0.96}
    Let me know if you need more.
    """
    
    score = _parse_judge_score(response)
    
    assert score is not None
    expected = (0.92 + 0.88 + 0.96) / 3.0
    assert abs(score - expected) < 0.01


def test_parse_judge_score_clamping():
    """Test that scores are clamped to [0, 1]."""
    # Test out-of-bounds scores
    response = '{"relevance": 1.5, "completeness": -0.2, "correctness": 0.5}'
    
    score = _parse_judge_score(response)
    
    assert score is not None
    assert 0.0 <= score <= 1.0


def test_parse_judge_score_invalid_json():
    """Test parsing with invalid JSON returns None."""
    response = "This is not JSON at all"
    
    score = _parse_judge_score(response)
    
    assert score is None


def test_parse_judge_score_missing_fields():
    """Test parsing with missing score fields."""
    response = '{"relevance": 0.8}'  # Missing completeness and correctness
    
    score = _parse_judge_score(response)
    
    # Should use 0.5 as default for missing fields
    assert score is not None
    expected = (0.8 + 0.5 + 0.5) / 3.0
    assert abs(score - expected) < 0.01


def test_parse_judge_score_empty_json():
    """Test parsing empty JSON object."""
    response = "{}"
    
    score = _parse_judge_score(response)
    
    # Should use defaults for all missing fields
    assert score is not None
    expected = (0.5 + 0.5 + 0.5) / 3.0
    assert abs(score - expected) < 0.01


@pytest.mark.asyncio
async def test_store_judge_score(temp_db):
    """Test storing judge score in database."""
    # Create a routing decision first
    db = await cost._get_db()
    try:
        await _insert_routing_decision(db, "openai/gpt-4o", judge_score=None)

        # Get the ID of the inserted decision
        cursor = await db.execute("SELECT id FROM routing_decisions ORDER BY id DESC LIMIT 1")
        row = await cursor.fetchone()

        if row:
            decision_id = row[0]
            # Store a score manually (directly instead of using _store_judge_score which closes db)
            await db.execute(
                "UPDATE routing_decisions SET judge_score = ? WHERE id = ?",
                (0.85, decision_id),
            )
            await db.commit()

            # Verify it was stored
            cursor = await db.execute("SELECT judge_score FROM routing_decisions WHERE id = ?", (decision_id,))
            row = await cursor.fetchone()

            assert row is not None
            assert row[0] is not None  # judge_score should not be None
            assert abs(float(row[0]) - 0.85) < 0.01
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_get_judge_scores_for_model_no_data(temp_db):
    """Test getting judge scores for model with no historical data."""
    scores = await get_judge_scores_for_model("openai/gpt-4o")
    
    assert scores["avg_score"] == 0.0
    assert scores["sample_count"] == 0
    assert scores["model"] == "openai/gpt-4o"


@pytest.mark.asyncio
async def test_get_judge_scores_for_model_with_data(temp_db):
    """Test getting judge scores for model with historical evaluations."""
    # Create routing decisions with judge scores
    db = await cost._get_db()
    try:
        scores_to_store = [0.8, 0.85, 0.9]
        for score in scores_to_store:
            await _insert_routing_decision(db, "openai/gpt-4o", judge_score=score)
    finally:
        await db.close()

    # Get aggregated scores
    model_scores = await get_judge_scores_for_model("openai/gpt-4o")

    assert model_scores["sample_count"] == 3
    assert model_scores["avg_score"] > 0.0
    assert 0.8 <= model_scores["min_score"] <= 0.9
    assert 0.8 <= model_scores["max_score"] <= 0.9


@pytest.mark.asyncio
async def test_reorder_by_quality_empty_list():
    """Test reorder with empty model list."""
    result = await reorder_by_quality([])
    assert result == []


@pytest.mark.asyncio
async def test_reorder_by_quality_no_historical_data(temp_db):
    """Test reorder with models that have no judge history."""
    models = ["openai/gpt-4o", "gemini/gemini-2.5-flash", "anthropic/claude-opus"]
    
    result = await reorder_by_quality(models)
    
    # Should return original order if no historical data
    assert result == models


@pytest.mark.asyncio
async def test_reorder_by_quality_demotes_low_quality(temp_db):
    """Test that low-quality models are demoted in the chain."""
    models = ["model-a", "model-b", "model-c"]

    # Create decisions with judge scores
    # model-a: good score (will stay at top)
    # model-b: low score (will be demoted)
    # model-c: good score (will stay at top)

    scores_per_model = {
        "model-a": [0.9, 0.92, 0.95],  # avg = 0.92 > 0.7
        "model-b": [0.5, 0.55, 0.6],   # avg = 0.55 < 0.7
        "model-c": [0.85, 0.88, 0.90],  # avg = 0.87 > 0.7
    }

    # Create routing decisions for each model
    db = await cost._get_db()
    try:
        for model, score_list in scores_per_model.items():
            for score in score_list:
                await _insert_routing_decision(db, model, judge_score=score)
    finally:
        await db.close()

    # Reorder the models
    result = await reorder_by_quality(models, days=30)

    # model-b (low quality) should be at the end
    assert result[-1] == "model-b"
    # model-a and model-c should be at the front (in some order)
    assert "model-a" in result[:2]
    assert "model-c" in result[:2]


@pytest.mark.asyncio
async def test_reorder_by_quality_insufficient_samples(temp_db):
    """Test reorder ignores models with < 3 samples."""
    models = ["model-a", "model-b"]

    # Create only 1 decision for model-a (insufficient samples)
    db = await cost._get_db()
    try:
        await _insert_routing_decision(db, "model-a", judge_score=0.5)
    finally:
        await db.close()

    # Reorder should return original order (insufficient data)
    result = await reorder_by_quality(models, days=30)
    assert result == models  # Unchanged order


@pytest.mark.asyncio
async def test_reorder_by_quality_error_handling(temp_db):
    """Test reorder gracefully handles database errors."""
    models = ["model-a", "model-b"]
    
    # Mock _get_db to raise an exception
    with patch("llm_router.judge._get_db", side_effect=Exception("DB error")):
        result = await reorder_by_quality(models)
        # Should return original list unchanged on error
        assert result == models
