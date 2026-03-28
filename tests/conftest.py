"""Shared fixtures for LLM Router tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from llm_router.types import LLMResponse


@pytest.fixture(autouse=True)
def _reset_singletons():
    """Reset module-level singletons between tests."""
    import llm_router.config as config_mod
    import llm_router.health as health_mod

    config_mod._config = None
    health_mod._tracker = None
    yield
    config_mod._config = None
    health_mod._tracker = None


@pytest.fixture
def mock_env(monkeypatch):
    """Set up test environment variables."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("PERPLEXITY_API_KEY", "test-perplexity-key")
    monkeypatch.setenv("LLM_ROUTER_PROFILE", "balanced")


@pytest.fixture
def sample_response() -> LLMResponse:
    return LLMResponse(
        content="Test response content",
        model="openai/gpt-4o",
        input_tokens=100,
        output_tokens=50,
        cost_usd=0.00075,
        latency_ms=450.0,
        provider="openai",
    )


@pytest.fixture
def mock_litellm_response():
    """Create a mock LiteLLM completion response."""
    def _make(content="Mock response", input_tokens=100, output_tokens=50):
        response = MagicMock()
        response.choices = [MagicMock()]
        response.choices[0].message.content = content
        response.usage = MagicMock()
        response.usage.prompt_tokens = input_tokens
        response.usage.completion_tokens = output_tokens
        response.citations = None
        return response
    return _make


@pytest.fixture
def mock_acompletion(mock_litellm_response):
    """Patch litellm.acompletion with a mock."""
    response = mock_litellm_response()
    with patch("litellm.acompletion", new_callable=AsyncMock, return_value=response) as mock:
        with patch("litellm.completion_cost", return_value=0.00075):
            yield mock
