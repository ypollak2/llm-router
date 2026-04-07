"""Tests for core routing logic."""

from unittest.mock import patch

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
    assert "gemini/gemini-2.5-flash" in call_kwargs.kwargs["model"]


@pytest.mark.asyncio
async def test_model_override_bypasses_routing(mock_env, mock_acompletion):
    await route_and_call(
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
    # Explicitly clear all API keys — use setenv("", "") pattern to also
    # override values that may be present in the shell environment.
    for key in ["GEMINI_API_KEY", "OPENAI_API_KEY", "PERPLEXITY_API_KEY",
                "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY", "GROQ_API_KEY",
                "MISTRAL_API_KEY", "TOGETHER_API_KEY", "XAI_API_KEY",
                "COHERE_API_KEY", "OLLAMA_BASE_URL"]:
        monkeypatch.setenv(key, "")
    monkeypatch.chdir("/tmp")  # no .env file here
    monkeypatch.setattr("llm_router.router.is_codex_available", lambda: False)
    with pytest.raises(ValueError, match="No available models"):
        await route_and_call(TaskType.QUERY, "Hello")


@pytest.mark.asyncio
async def test_research_no_search_params_for_non_perplexity(mock_env, mock_acompletion):
    # Non-Perplexity models explicitly overridden must NOT receive search_recency_filter.
    await route_and_call(TaskType.RESEARCH, "What happened today?", model_override="openai/gpt-4o")
    call_kwargs = mock_acompletion.call_args.kwargs
    extra_body = call_kwargs.get("extra_body", {})
    assert "search_recency_filter" not in extra_body


@pytest.mark.asyncio
async def test_research_adds_search_params_for_perplexity(mock_env, mock_acompletion, monkeypatch):
    # Perplexity sonar models should receive the recency filter.
    monkeypatch.setenv("LLM_ROUTER_PROFILE", "balanced")
    await route_and_call(TaskType.RESEARCH, "What happened today?", model_override="perplexity/sonar")
    call_kwargs = mock_acompletion.call_args.kwargs
    assert call_kwargs.get("extra_body", {}).get("search_recency_filter") == "week"


@pytest.mark.asyncio
async def test_content_filter_error_is_silent_fallback(mock_env, mock_litellm_response):
    """Content filter errors should silently skip to next model without warning."""
    call_count = 0

    async def side_effect(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise Exception("litellm.BadRequestError: Output blocked by content filtering policy")
        return mock_litellm_response()

    with patch("litellm.acompletion", side_effect=side_effect):
        with patch("litellm.completion_cost", return_value=0.001):
            resp = await route_and_call(
                TaskType.QUERY, "Hello",
                profile=RoutingProfile.BUDGET,
            )
    assert resp.content == "Mock response"
    assert call_count == 2  # first content-filtered, second succeeded


@pytest.mark.asyncio
async def test_subscription_mode_blocks_anthropic_override(mock_env, mock_acompletion, monkeypatch):
    """In subscription mode, explicit anthropic/ model_override should be redirected."""
    monkeypatch.setenv("LLM_ROUTER_CLAUDE_SUBSCRIPTION", "true")
    import llm_router.router as _router
    import llm_router.config as _config
    _config._config = None  # force config reload
    _router._config = None if hasattr(_router, "_config") else None
    resp = await route_and_call(
        TaskType.QUERY, "Hello",
        model_override="anthropic/claude-haiku-4-5-20251001",
    )
    # Should have used a non-Anthropic model
    assert not resp.model.startswith("anthropic/")
    _config._config = None  # reset for other tests


@pytest.mark.asyncio
async def test_claw_code_mode_injects_ollama_for_balanced_profile(
    mock_env, mock_acompletion, monkeypatch
):
    """In claw-code mode, Ollama should be injected for BALANCED profile (not just BUDGET)."""
    monkeypatch.setenv("LLM_ROUTER_CLAW_CODE", "true")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
    monkeypatch.setenv("OLLAMA_BUDGET_MODELS", "llama3.2")
    import llm_router.config as _config
    _config._config = None

    await route_and_call(TaskType.QUERY, "Hello", profile=RoutingProfile.BALANCED)

    call_kwargs = mock_acompletion.call_args.kwargs
    assert "ollama" in call_kwargs["model"], (
        f"Expected Ollama to be first in BALANCED chain in claw-code mode, got {call_kwargs['model']}"
    )
    _config._config = None


@pytest.mark.asyncio
async def test_claw_code_mode_injects_ollama_for_premium_profile(
    mock_env, mock_acompletion, monkeypatch
):
    """In claw-code mode, Ollama should also be injected for PREMIUM profile."""
    monkeypatch.setenv("LLM_ROUTER_CLAW_CODE", "true")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
    monkeypatch.setenv("OLLAMA_BUDGET_MODELS", "llama3.2")
    import llm_router.config as _config
    _config._config = None

    await route_and_call(TaskType.QUERY, "Hello", profile=RoutingProfile.PREMIUM)

    call_kwargs = mock_acompletion.call_args.kwargs
    assert "ollama" in call_kwargs["model"], (
        f"Expected Ollama to be first in PREMIUM chain in claw-code mode, got {call_kwargs['model']}"
    )
    _config._config = None


@pytest.mark.asyncio
async def test_no_claw_code_mode_ollama_skipped_for_balanced(
    mock_env, mock_acompletion, monkeypatch
):
    """Without claw-code mode, Ollama should NOT inject for BALANCED profile at zero pressure."""
    monkeypatch.setenv("LLM_ROUTER_CLAW_CODE", "false")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
    monkeypatch.setenv("OLLAMA_BUDGET_MODELS", "llama3.2")
    import llm_router.config as _config
    import llm_router.claude_usage as _usage
    _config._config = None
    _usage.set_claude_pressure(0.0)  # no subscription pressure

    await route_and_call(TaskType.QUERY, "Hello", profile=RoutingProfile.BALANCED)

    call_kwargs = mock_acompletion.call_args.kwargs
    assert "ollama" not in call_kwargs["model"], (
        f"Ollama should not inject for BALANCED without claw-code or pressure, got {call_kwargs['model']}"
    )
    _config._config = None
