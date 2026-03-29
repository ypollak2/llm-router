"""Tests for the llm_route smart routing tool."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from llm_router.types import (
    ClassificationResult, Complexity, LLMResponse, RoutingProfile, TaskType,
)


def _make_classification(
    complexity: str = "moderate",
    task_type: str = "query",
    confidence: float = 0.85,
) -> ClassificationResult:
    return ClassificationResult(
        complexity=Complexity(complexity),
        confidence=confidence,
        reasoning="test",
        inferred_task_type=TaskType(task_type),
        classifier_model="gemini/gemini-2.5-flash",
        classifier_cost_usd=0.00001,
        classifier_latency_ms=200.0,
    )


def _make_response(model: str = "openai/gpt-4o") -> LLMResponse:
    return LLMResponse(
        content="The answer is 42.",
        model=model,
        input_tokens=100,
        output_tokens=50,
        cost_usd=0.003,
        latency_ms=800.0,
        provider=model.split("/")[0],
    )


@pytest.fixture
def mock_ctx():
    ctx = MagicMock()
    ctx.info = AsyncMock()
    ctx.warning = AsyncMock()
    return ctx


@pytest.mark.asyncio
async def test_route_simple_goes_to_budget(mock_env, mock_ctx):
    classification = _make_classification("simple", "query")
    response = _make_response("gemini/gemini-2.5-flash")

    with patch("llm_router.server.classify_complexity", new_callable=AsyncMock, return_value=classification), \
         patch("llm_router.server.route_and_call", new_callable=AsyncMock, return_value=response) as mock_route:

        from llm_router.server import llm_route
        result = await llm_route("What is 2+2?", mock_ctx)

        # Verify route_and_call was called with budget profile
        call_kwargs = mock_route.call_args
        assert call_kwargs[1]["profile"] == RoutingProfile.BUDGET
        assert "simple" in result
        assert "gemini" in result


@pytest.mark.asyncio
async def test_route_complex_goes_to_premium(mock_env, mock_ctx):
    classification = _make_classification("complex", "code")
    response = _make_response("openai/o3")

    with patch("llm_router.server.classify_complexity", new_callable=AsyncMock, return_value=classification), \
         patch("llm_router.server.route_and_call", new_callable=AsyncMock, return_value=response) as mock_route:

        from llm_router.server import llm_route
        result = await llm_route("Design a distributed CQRS architecture", mock_ctx)

        call_kwargs = mock_route.call_args
        assert call_kwargs[1]["profile"] == RoutingProfile.PREMIUM
        assert "complex" in result


@pytest.mark.asyncio
async def test_route_with_complexity_override(mock_env, mock_ctx):
    response = _make_response("gemini/gemini-2.5-flash")

    with patch("llm_router.server.classify_complexity") as mock_classify, \
         patch("llm_router.server.route_and_call", new_callable=AsyncMock, return_value=response) as mock_route:

        from llm_router.server import llm_route
        await llm_route("Some prompt", mock_ctx, complexity_override="simple")

        # Classifier should NOT be called
        mock_classify.assert_not_called()
        call_kwargs = mock_route.call_args
        assert call_kwargs[1]["profile"] == RoutingProfile.BUDGET


@pytest.mark.asyncio
async def test_route_invalid_complexity_override(mock_env, mock_ctx):
    from llm_router.server import llm_route
    result = await llm_route("test", mock_ctx, complexity_override="impossible")
    assert "Invalid complexity" in result


@pytest.mark.asyncio
async def test_route_uses_inferred_task_type(mock_env, mock_ctx):
    classification = _make_classification("moderate", "code")
    response = _make_response("openai/gpt-4o")

    with patch("llm_router.server.classify_complexity", new_callable=AsyncMock, return_value=classification), \
         patch("llm_router.server.route_and_call", new_callable=AsyncMock, return_value=response) as mock_route:

        from llm_router.server import llm_route
        await llm_route("Write a Python function to sort a list", mock_ctx)

        # Should use the inferred task_type from classifier
        call_args = mock_route.call_args
        assert call_args[0][0] == TaskType.CODE


@pytest.mark.asyncio
async def test_route_explicit_task_type_overrides_inferred(mock_env, mock_ctx):
    classification = _make_classification("moderate", "code")
    response = _make_response("openai/gpt-4o")

    with patch("llm_router.server.classify_complexity", new_callable=AsyncMock, return_value=classification), \
         patch("llm_router.server.route_and_call", new_callable=AsyncMock, return_value=response) as mock_route:

        from llm_router.server import llm_route
        await llm_route("Write a poem about code", mock_ctx, task_type="generate")

        call_args = mock_route.call_args
        assert call_args[0][0] == TaskType.GENERATE


@pytest.mark.asyncio
async def test_route_shows_total_cost(mock_env, mock_ctx):
    classification = _make_classification("moderate", "query")
    response = _make_response("openai/gpt-4o")

    with patch("llm_router.server.classify_complexity", new_callable=AsyncMock, return_value=classification), \
         patch("llm_router.server.route_and_call", new_callable=AsyncMock, return_value=response):

        from llm_router.server import llm_route
        result = await llm_route("Explain quantum computing", mock_ctx)

        assert "Total cost" in result
        # 0.00001 (classifier) + 0.003 (response) = 0.00301
        assert "$0.003010" in result


@pytest.mark.asyncio
async def test_route_classifier_failure_falls_back(mock_env, mock_ctx):
    response = _make_response("openai/gpt-4o")

    with patch("llm_router.server.classify_complexity", new_callable=AsyncMock, side_effect=RuntimeError("boom")), \
         patch("llm_router.server.route_and_call", new_callable=AsyncMock, return_value=response) as mock_route:

        from llm_router.server import llm_route
        result = await llm_route("test prompt", mock_ctx)

        # Should fall back to balanced (moderate)
        call_kwargs = mock_route.call_args
        assert call_kwargs[1]["profile"] == RoutingProfile.BALANCED
        assert "The answer is 42." in result
