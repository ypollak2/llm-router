"""Tests for core routing logic."""

from unittest.mock import AsyncMock, patch

import pytest

from llm_router.router import route_and_call
from llm_router.types import LLMResponse, RoutingProfile, TaskType


@pytest.mark.asyncio
async def test_routes_to_first_available_model(mock_env, mock_acompletion):
    resp = await route_and_call(TaskType.QUERY, "Hello", profile=RoutingProfile.BUDGET)
    assert isinstance(resp, LLMResponse)
    assert resp.content == "Mock response"
    # Should have called acompletion with the first model in budget/query chain
    call_kwargs = mock_acompletion.call_args
    assert "gemini/gemini-2.0-flash" in call_kwargs.kwargs["model"]


@pytest.mark.asyncio
async def test_model_override_bypasses_routing(mock_env, mock_acompletion):
    resp = await route_and_call(
        TaskType.QUERY, "Hello",
        model_override="openai/gpt-4o",
    )
    call_kwargs = mock_acompletion.call_args
    assert call_kwargs.kwargs["model"] == "openai/gpt-4o"


@pytest.mark.asyncio
async def test_system_prompt_included(mock_env, mock_acompletion):
    await route_and_call(
        TaskType.GENERATE, "Write a poem",
        system_prompt="You are a poet",
    )
    call_kwargs = mock_acompletion.call_args
    messages = call_kwargs.kwargs["messages"]
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == "You are a poet"


@pytest.mark.asyncio
async def test_falls_back_on_failure(mock_env, mock_litellm_response):
    call_count = 0

    async def side_effect(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise Exception("Provider down")
        return mock_litellm_response()

    with patch("litellm.acompletion", side_effect=side_effect):
        with patch("litellm.completion_cost", return_value=0.001):
            resp = await route_and_call(
                TaskType.QUERY, "Hello",
                profile=RoutingProfile.BUDGET,
            )
    assert resp.content == "Mock response"
    assert call_count == 2  # first failed, second succeeded


@pytest.mark.asyncio
async def test_raises_when_all_fail(mock_env):
    with patch("litellm.acompletion", side_effect=Exception("All down")):
        with pytest.raises(RuntimeError, match="All models failed"):
            await route_and_call(TaskType.QUERY, "Hello")


@pytest.mark.asyncio
async def test_no_providers_configured(monkeypatch):
    # Explicitly clear all API keys and prevent .env file loading
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
    monkeypatch.chdir("/tmp")  # no .env file here
    with pytest.raises(ValueError, match="No available models"):
        await route_and_call(TaskType.QUERY, "Hello")


@pytest.mark.asyncio
async def test_research_adds_search_params(mock_env, mock_acompletion):
    await route_and_call(TaskType.RESEARCH, "What happened today?")
    call_kwargs = mock_acompletion.call_args.kwargs
    assert "extra_body" in call_kwargs
    assert call_kwargs["extra_body"]["search_recency_filter"] == "week"
