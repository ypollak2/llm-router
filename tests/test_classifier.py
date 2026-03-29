"""Tests for the complexity classifier."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from llm_router.classifier import _parse_classification, classify_complexity
from llm_router.types import Complexity, LLMResponse, TaskType


# ── JSON parsing tests ───────────────────────────────────────────────────────


class TestParseClassification:
    def test_clean_json(self):
        raw = '{"complexity": "simple", "task_type": "query", "confidence": 0.95, "reasoning": "factual"}'
        result = _parse_classification(raw)
        assert result["complexity"] == "simple"
        assert result["confidence"] == 0.95

    def test_json_in_code_fence(self):
        raw = '```json\n{"complexity": "complex", "task_type": "code", "confidence": 0.8, "reasoning": "architecture"}\n```'
        result = _parse_classification(raw)
        assert result["complexity"] == "complex"

    def test_json_in_plain_fence(self):
        raw = '```\n{"complexity": "moderate", "task_type": "analyze", "confidence": 0.7, "reasoning": "multi-step"}\n```'
        result = _parse_classification(raw)
        assert result["complexity"] == "moderate"

    def test_json_with_surrounding_text(self):
        raw = 'Here is my analysis:\n{"complexity": "simple", "task_type": "query", "confidence": 0.9, "reasoning": "basic"}\nDone.'
        result = _parse_classification(raw)
        assert result["complexity"] == "simple"

    def test_invalid_json_raises(self):
        with pytest.raises(ValueError, match="Could not parse JSON"):
            _parse_classification("This is not JSON at all")

    def test_empty_string_raises(self):
        with pytest.raises(ValueError):
            _parse_classification("")


# ── classify_complexity tests ────────────────────────────────────────────────


def _make_classifier_response(complexity: str, task_type: str, confidence: float = 0.9) -> LLMResponse:
    content = json.dumps({
        "complexity": complexity,
        "task_type": task_type,
        "confidence": confidence,
        "reasoning": "test reasoning",
    })
    return LLMResponse(
        content=content, model="gemini/gemini-2.5-flash",
        input_tokens=50, output_tokens=30, cost_usd=0.00001,
        latency_ms=200.0, provider="gemini",
    )


@pytest.mark.asyncio
async def test_classify_simple(mock_env, mock_acompletion):
    resp = _make_classifier_response("simple", "query")
    mock_acompletion.return_value = None  # override fixture
    with patch("llm_router.providers.call_llm", new_callable=AsyncMock, return_value=resp):
        result = await classify_complexity("What is 2+2?")
    assert result.complexity == Complexity.SIMPLE
    assert result.inferred_task_type == TaskType.QUERY
    assert result.confidence == 0.9


@pytest.mark.asyncio
async def test_classify_complex(mock_env, mock_acompletion):
    resp = _make_classifier_response("complex", "code", 0.85)
    with patch("llm_router.providers.call_llm", new_callable=AsyncMock, return_value=resp):
        result = await classify_complexity("Design a distributed CQRS event sourcing architecture")
    assert result.complexity == Complexity.COMPLEX
    assert result.inferred_task_type == TaskType.CODE


@pytest.mark.asyncio
async def test_classify_fallback_on_invalid_complexity(mock_env, mock_acompletion):
    content = json.dumps({"complexity": "ultra_hard", "task_type": "query", "confidence": 0.5, "reasoning": "weird"})
    resp = LLMResponse(
        content=content, model="gemini/gemini-2.5-flash",
        input_tokens=50, output_tokens=30, cost_usd=0.00001,
        latency_ms=200.0, provider="gemini",
    )
    with patch("llm_router.providers.call_llm", new_callable=AsyncMock, return_value=resp):
        result = await classify_complexity("test prompt")
    assert result.complexity == Complexity.MODERATE  # fallback


@pytest.mark.asyncio
async def test_classify_fallback_on_parse_error(mock_env, mock_acompletion):
    resp = LLMResponse(
        content="I cannot classify this", model="gemini/gemini-2.5-flash",
        input_tokens=50, output_tokens=30, cost_usd=0.00001,
        latency_ms=200.0, provider="gemini",
    )
    with patch("llm_router.providers.call_llm", new_callable=AsyncMock, return_value=resp):
        result = await classify_complexity("test prompt")
    # Should try next model, then fallback to moderate
    assert result.complexity == Complexity.MODERATE
    assert result.confidence == 0.0


@pytest.mark.asyncio
async def test_classify_no_providers():
    """No API keys configured — returns moderate fallback."""
    with patch("llm_router.classifier.get_config") as mock_config:
        mock_config.return_value.available_providers = set()
        result = await classify_complexity("test prompt")
    assert result.complexity == Complexity.MODERATE
    assert result.confidence == 0.0
    assert "no classifier models" in result.reasoning


@pytest.mark.asyncio
async def test_classify_clamps_confidence(mock_env, mock_acompletion):
    content = json.dumps({"complexity": "simple", "task_type": "query", "confidence": 5.0, "reasoning": "over"})
    resp = LLMResponse(
        content=content, model="gemini/gemini-2.5-flash",
        input_tokens=50, output_tokens=30, cost_usd=0.00001,
        latency_ms=200.0, provider="gemini",
    )
    with patch("llm_router.providers.call_llm", new_callable=AsyncMock, return_value=resp):
        result = await classify_complexity("test")
    assert result.confidence == 1.0


@pytest.mark.asyncio
async def test_classify_invalid_task_type_returns_none(mock_env, mock_acompletion):
    content = json.dumps({"complexity": "moderate", "task_type": "dance", "confidence": 0.5, "reasoning": "bad type"})
    resp = LLMResponse(
        content=content, model="gemini/gemini-2.5-flash",
        input_tokens=50, output_tokens=30, cost_usd=0.00001,
        latency_ms=200.0, provider="gemini",
    )
    with patch("llm_router.providers.call_llm", new_callable=AsyncMock, return_value=resp):
        result = await classify_complexity("test")
    assert result.inferred_task_type is None
