"""Shared fixtures for LLM Router tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from llm_router.types import LLMResponse


@pytest.fixture(autouse=True)
def _reset_singletons():
    """Reset module-level singletons between tests."""
    import llm_router.config as config_mod
    import llm_router.context as context_mod
    import llm_router.health as health_mod
    import llm_router.discover as discover_mod

    config_mod._config = None
    health_mod._tracker = None
    context_mod._session_buffer = None
    discover_mod._discovery_cache = None
    yield
    config_mod._config = None
    health_mod._tracker = None
    context_mod._session_buffer = None
    discover_mod._discovery_cache = None


@pytest.fixture
def mock_env(monkeypatch):
    """Set up test environment variables."""
    from llm_router.types import BudgetState

    # Set all provider API keys to enable routing
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("PERPLEXITY_API_KEY", "test-perplexity-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")
    monkeypatch.setenv("MISTRAL_API_KEY", "test-mistral-key")
    monkeypatch.setenv("LLM_ROUTER_PROFILE", "balanced")
    # Pydantic requires boolean env vars to be "0", "false", "False", "FALSE", "no" for False
    # Setting to "0" ensures it's parsed as False, not truthy like the string "false"
    monkeypatch.setenv("LLM_ROUTER_CLAUDE_SUBSCRIPTION", "0")
    # Disable Codex in unit tests — the binary may be installed locally but
    # Codex CLI requires a trusted git directory and an active session, which
    # unit tests don't provide. Tests that specifically exercise Codex routing
    # should patch this themselves.
    monkeypatch.setattr("llm_router.router.is_codex_available", lambda: False)
    monkeypatch.setattr("llm_router.profiles.is_codex_available", lambda: False, raising=False)
    # Disable Ollama in unit tests — ollama_base_url may be set in the local .env
    # but Ollama routing requires a running server. Unit tests that specifically
    # exercise Ollama injection should set OLLAMA_BASE_URL themselves.
    monkeypatch.setenv("OLLAMA_BASE_URL", "")
    # Mock budget pressure to 0.0 for all providers so tests aren't affected by
    # real system budget state. Tests that specifically want to test budget
    # pressure behavior should patch this themselves.
    async def mock_get_budget_state(provider: str) -> BudgetState:
        return BudgetState(provider=provider, pressure=0.0)
    monkeypatch.setattr("llm_router.router.get_budget_state", mock_get_budget_state)


@pytest.fixture
def minimal_env(monkeypatch):
    """Minimal test environment with few configured providers.

    Used for setup status tests that need to verify "Recommended to Add" section.
    This fixture intentionally leaves some recommended providers (groq, deepseek)
    unconfigured so the status output includes recommendations.
    """
    from llm_router.types import BudgetState

    # Only configure openai and gemini, leaving groq/deepseek unconfigured
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("LLM_ROUTER_PROFILE", "balanced")
    monkeypatch.setenv("LLM_ROUTER_CLAUDE_SUBSCRIPTION", "0")
    # Disable Codex and Ollama
    monkeypatch.setattr("llm_router.router.is_codex_available", lambda: False)
    monkeypatch.setattr("llm_router.profiles.is_codex_available", lambda: False, raising=False)
    monkeypatch.setenv("OLLAMA_BASE_URL", "")
    # Mock budget pressure
    async def mock_get_budget_state(provider: str) -> BudgetState:
        return BudgetState(provider=provider, pressure=0.0)
    monkeypatch.setattr("llm_router.router.get_budget_state", mock_get_budget_state)


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
