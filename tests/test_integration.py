"""CI-safe integration tests for router + provider wiring.

These tests exercise ``route_and_call()`` end to end while mocking the network
boundary at LiteLLM. That keeps them deterministic in CI while still covering
model overrides, research-specific provider params, and profile-based routing.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from llm_router.router import route_and_call
from llm_router.types import RoutingProfile, TaskType

@pytest.mark.asyncio
async def test_openai_query_model_override_end_to_end(mock_env, mock_litellm_response):
    captured: dict = {}

    async def side_effect(**kwargs):
        captured.update(kwargs)
        return mock_litellm_response(content="4", input_tokens=12, output_tokens=1)

    with patch("litellm.acompletion", side_effect=side_effect):
        with patch("litellm.completion_cost", return_value=0.0001):
            resp = await route_and_call(
                TaskType.QUERY,
                "What is 2+2? Reply with just the number.",
                model_override="openai/gpt-4o-mini",
                max_tokens=10,
            )

    assert "4" in resp.content
    assert captured["model"] == "openai/gpt-4o-mini"
    assert resp.input_tokens > 0
    assert resp.cost_usd >= 0


@pytest.mark.asyncio
async def test_gemini_query_model_override_end_to_end(mock_env, mock_litellm_response):
    captured: dict = {}

    async def side_effect(**kwargs):
        captured.update(kwargs)
        return mock_litellm_response(content="4", input_tokens=10, output_tokens=1)

    with patch("litellm.acompletion", side_effect=side_effect):
        with patch("litellm.completion_cost", return_value=0.00005):
            resp = await route_and_call(
                TaskType.QUERY,
                "What is 2+2? Reply with just the number.",
                model_override="gemini/gemini-2.5-flash",
                max_tokens=100,
            )

    assert "4" in resp.content
    assert captured["model"] == "gemini/gemini-2.5-flash"
    assert resp.input_tokens > 0


@pytest.mark.asyncio
async def test_perplexity_research_uses_recency_filter_end_to_end(mock_env, mock_litellm_response):
    captured: dict = {}

    async def side_effect(**kwargs):
        captured.update(kwargs)
        return mock_litellm_response(content="Python 3.14", input_tokens=20, output_tokens=4)

    with patch("litellm.acompletion", side_effect=side_effect):
        with patch("litellm.completion_cost", return_value=0.0002):
            resp = await route_and_call(
                TaskType.RESEARCH,
                "What is the current version of Python?",
                model_override="perplexity/sonar",
                max_tokens=100,
            )

    assert len(resp.content) > 10
    assert captured["model"] == "perplexity/sonar"
    assert captured.get("extra_body", {}).get("search_recency_filter") == "week"


@pytest.mark.asyncio
async def test_budget_profile_routes_code_end_to_end(mock_env, mock_litellm_response):
    captured: dict = {}

    async def side_effect(**kwargs):
        captured.update(kwargs)
        return mock_litellm_response(
            content="def always_true():\n    return True",
            input_tokens=30,
            output_tokens=8,
        )

    with patch("litellm.acompletion", side_effect=side_effect):
        with patch("litellm.completion_cost", return_value=0.0001):
            resp = await route_and_call(
                TaskType.CODE,
                "Write a Python function that returns True. Just the function, nothing else.",
                profile=RoutingProfile.BUDGET,
                max_tokens=150,
            )

    assert "def" in resp.content.lower() or "true" in resp.content.lower()
    assert captured["model"] == resp.model
