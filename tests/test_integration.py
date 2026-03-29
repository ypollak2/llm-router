"""Integration tests — hit real APIs. Run with: pytest tests/test_integration.py -v

Requires actual API keys in environment. Skipped if keys are missing.
"""

from __future__ import annotations

import os

import pytest

from llm_router.router import route_and_call
from llm_router.types import RoutingProfile, TaskType

has_openai = bool(os.environ.get("OPENAI_API_KEY"))
has_gemini = bool(os.environ.get("GEMINI_API_KEY"))
has_perplexity = bool(os.environ.get("PERPLEXITY_API_KEY"))


@pytest.mark.skipif(not has_openai, reason="OPENAI_API_KEY not set")
@pytest.mark.asyncio
async def test_real_openai_query():
    resp = await route_and_call(
        TaskType.QUERY,
        "What is 2+2? Reply with just the number.",
        model_override="openai/gpt-4o-mini",
        max_tokens=10,
    )
    assert "4" in resp.content
    assert resp.input_tokens > 0
    assert resp.cost_usd >= 0
    print(f"\nOpenAI: {resp.summary()}")


@pytest.mark.skipif(not has_gemini, reason="GEMINI_API_KEY not set")
@pytest.mark.asyncio
async def test_real_gemini_query():
    resp = await route_and_call(
        TaskType.QUERY,
        "What is 2+2? Reply with just the number.",
        model_override="gemini/gemini-2.5-flash",
        max_tokens=100,
    )
    assert "4" in resp.content
    assert resp.input_tokens > 0
    print(f"\nGemini: {resp.summary()}")


@pytest.mark.skipif(not has_perplexity, reason="PERPLEXITY_API_KEY not set")
@pytest.mark.asyncio
async def test_real_perplexity_research():
    resp = await route_and_call(
        TaskType.RESEARCH,
        "What is the current version of Python?",
        model_override="perplexity/sonar",
        max_tokens=100,
    )
    assert len(resp.content) > 10
    print(f"\nPerplexity: {resp.summary()}")


@pytest.mark.skipif(not has_gemini, reason="GEMINI_API_KEY not set")
@pytest.mark.asyncio
async def test_real_routing_budget():
    """Test that budget profile auto-routes correctly."""
    resp = await route_and_call(
        TaskType.CODE,
        "Write a Python function that returns True. Just the function, nothing else.",
        profile=RoutingProfile.BUDGET,
        max_tokens=150,
    )
    assert "def" in resp.content.lower() or "true" in resp.content.lower()
    print(f"\nBudget/Code routed to: {resp.model} — {resp.summary()}")
