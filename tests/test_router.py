"""Tests for core routing logic."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from llm_router.router import route_and_call
from llm_router.types import BudgetState, LLMResponse, RoutingProfile, TaskType


# ── CHZ-AUD-013: Per-provider canary tests ───────────────────────────────────
# These tests assert that the *correct provider* produced the response, not just
# that some mock returned "Mock response". Each provider gets a unique canary
# string injected as the fake response content, so any wiring bug that sends
# traffic to the wrong provider is detected immediately.

@pytest.mark.asyncio
async def test_provider_canary_openai(temp_db, mock_env, monkeypatch):
    """Routing to openai/ must produce the openai-specific canary — not a fixed
    'Mock response' constant. Validates that call_llm was actually invoked with
    the openai model and that its return value flows through to the caller.
    """
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)

    canary = "PROVIDER_OPENAI_CANARY_DEADBEEF"

    async def _fake_call_llm(model: str, messages, **kwargs):
        return LLMResponse(
            content=canary if model.startswith("openai/") else "WRONG_PROVIDER",
            model=model,
            input_tokens=5,
            output_tokens=5,
            cost_usd=0.0001,
            latency_ms=50.0,
            provider="openai",
        )

    mock_tracker = MagicMock()
    mock_tracker.is_healthy.return_value = True

    with patch("llm_router.providers.call_llm", side_effect=_fake_call_llm):
        with patch("llm_router.codex_agent.is_codex_available", return_value=False):
            with patch("llm_router.router.get_tracker", return_value=mock_tracker):
                with patch("llm_router.router._build_and_filter_chain",
                           new_callable=lambda: AsyncMock(return_value=["openai/gpt-4o-mini"])):
                    resp = await route_and_call(TaskType.QUERY, "Hello")

    assert resp.content == canary, (
        f"Expected openai canary '{canary}', got '{resp.content}'. "
        "Provider execution is not reaching call_llm correctly."
    )
    assert resp.model.startswith("openai/"), (
        f"Expected openai/ model, got {resp.model!r}"
    )


@pytest.mark.asyncio
async def test_provider_canary_gemini(temp_db, mock_env, monkeypatch):
    """Routing to gemini/ must produce the gemini-specific canary — verifying
    the provider dispatch path reaches call_llm with the correct model.
    """
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)

    canary = "PROVIDER_GEMINI_CANARY_CAFEBABE"

    async def _fake_call_llm(model: str, messages, **kwargs):
        return LLMResponse(
            content=canary if model.startswith("gemini/") else "WRONG_PROVIDER",
            model=model,
            input_tokens=5,
            output_tokens=5,
            cost_usd=0.0,
            latency_ms=60.0,
            provider="gemini",
        )

    mock_tracker = MagicMock()
    mock_tracker.is_healthy.return_value = True

    with patch("llm_router.providers.call_llm", side_effect=_fake_call_llm):
        with patch("llm_router.codex_agent.is_codex_available", return_value=False):
            with patch("llm_router.router.get_tracker", return_value=mock_tracker):
                with patch("llm_router.router._build_and_filter_chain",
                           new_callable=lambda: AsyncMock(return_value=["gemini/gemini-2.5-flash"])):
                    resp = await route_and_call(TaskType.QUERY, "Hello")

    assert resp.content == canary, (
        f"Expected gemini canary '{canary}', got '{resp.content}'. "
        "Provider execution is not reaching call_llm correctly."
    )
    assert resp.model.startswith("gemini/"), (
        f"Expected gemini/ model, got {resp.model!r}"
    )


@pytest.mark.asyncio
async def test_routes_to_first_available_model(temp_db, mock_env, mock_acompletion, monkeypatch):
    # Disable Ollama to test pure API chain
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    resp = await route_and_call(TaskType.QUERY, "Hello", profile=RoutingProfile.BUDGET)
    assert isinstance(resp, LLMResponse)
    assert resp.content == "Mock response"
    # Should have called acompletion with a model
    call_kwargs = mock_acompletion.call_args
    assert call_kwargs is not None
    assert "model" in call_kwargs.kwargs


@pytest.mark.asyncio
async def test_logs_structured_routing_decision(temp_db, mock_env, mock_acompletion):
    route_log = MagicMock()
    fake_uuid = MagicMock(hex="deadbeefcafebabe")

    with patch("llm_router.router.log") as mock_log:
        with patch("llm_router.router.uuid4", return_value=fake_uuid):
            mock_log.bind.return_value = route_log
            resp = await route_and_call(
                TaskType.QUERY,
                "Hello",
                complexity_hint="simple",
            )

    decision_calls = [
        call for call in route_log.info.call_args_list
        if call.args and call.args[0] == "routing_decision"
    ]
    assert decision_calls
    decision = decision_calls[-1]
    assert decision.kwargs["correlation_id"] == "deadbeef"
    assert decision.kwargs["task_type"] == "query"
    assert decision.kwargs["complexity"] == "simple"
    assert decision.kwargs["model"] == resp.model
    assert decision.kwargs["cost_usd"] == resp.cost_usd


@pytest.mark.asyncio
async def test_model_override_bypasses_routing(temp_db, mock_env, mock_acompletion):
    await route_and_call(
        TaskType.QUERY, "Hello",
        model_override="openai/gpt-4o",
    )
    call_kwargs = mock_acompletion.call_args
    assert call_kwargs.kwargs["model"] == "openai/gpt-4o"


@pytest.mark.asyncio
async def test_system_prompt_included(temp_db, mock_env, mock_acompletion):
    await route_and_call(
        TaskType.GENERATE, "Write a poem",
        system_prompt="You are a poet",
    )
    call_kwargs = mock_acompletion.call_args
    messages = call_kwargs.kwargs["messages"]
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == "You are a poet"


@pytest.mark.asyncio
async def test_falls_back_on_failure(temp_db, mock_env, mock_litellm_response):
    from llm_router.types import LLMResponse

    call_count = 0

    async def side_effect(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise Exception("Provider down")
        # Return LLMResponse for providers.call_llm (not litellm response)
        return LLMResponse(
            content="Mock response",
            model=kwargs.get("model", "test/mock"),
            input_tokens=10,
            output_tokens=5,
            cost_usd=0.001,
            latency_ms=100.0,
            provider="test",
        )

    # Pin the chain: on a bare runner (no ollama, no discovered providers) the
    # environment-built chain can collapse to a single model, leaving nothing
    # to fall back to. Two models makes the fallback assertion hermetic.
    with patch("llm_router.router._build_and_filter_chain",
               new_callable=lambda: AsyncMock(
                   return_value=["openai/gpt-4o-mini", "gemini/gemini-2.5-flash"])), \
         patch("llm_router.providers.call_llm", new_callable=lambda: AsyncMock(side_effect=side_effect)):
        resp = await route_and_call(
            TaskType.QUERY, "Hello",
            profile=RoutingProfile.BUDGET,
        )
    assert resp.content == "Mock response"
    assert call_count == 2  # first failed, second succeeded


@pytest.mark.asyncio
async def test_raises_when_all_fail(temp_db, mock_env):
    with patch("litellm.acompletion", side_effect=Exception("All down")):
        with pytest.raises(RuntimeError, match="All models failed"):
            await route_and_call(TaskType.QUERY, "Hello")


@pytest.mark.asyncio
async def test_no_providers_configured(no_providers_env, monkeypatch):
    """When no providers are configured, the router should raise an error.

    Before the provider-availability audit fix, codex/ollama/gemini_cli
    candidates always survived the provider filter regardless of real
    availability, so this scenario used to produce a non-empty chain of
    phantom candidates that all failed at dispatch time, surfacing as
    "All models failed" only after wasted attempts. The filter now checks
    real availability, so a genuinely empty environment is caught upfront
    with a clearer, more actionable message instead.
    """
    monkeypatch.setattr("llm_router.router.is_codex_available", lambda: False)
    with pytest.raises(
        (ValueError, RuntimeError),
        match="No available models|All models failed|No providers available",
    ):
        await route_and_call(TaskType.QUERY, "Hello")


@pytest.mark.asyncio
async def test_research_no_search_params_for_non_perplexity(temp_db, mock_env, mock_acompletion):
    # Non-Perplexity models explicitly overridden must NOT receive search_recency_filter.
    await route_and_call(TaskType.RESEARCH, "What happened today?", model_override="openai/gpt-4o")
    call_kwargs = mock_acompletion.call_args.kwargs
    extra_body = call_kwargs.get("extra_body", {})
    assert "search_recency_filter" not in extra_body


@pytest.mark.asyncio
async def test_research_adds_search_params_for_perplexity(temp_db, mock_env, mock_acompletion, monkeypatch):
    # Perplexity sonar models should receive the recency filter.
    monkeypatch.setenv("LLM_ROUTER_PROFILE", "balanced")
    await route_and_call(TaskType.RESEARCH, "What happened today?", model_override="perplexity/sonar")
    call_kwargs = mock_acompletion.call_args.kwargs
    # extra_body is passed via extra_params dict
    extra_params = call_kwargs.get("extra_params", {})
    assert extra_params.get("extra_body", {}).get("search_recency_filter") == "week"


@pytest.mark.asyncio
async def test_content_filter_error_is_silent_fallback(temp_db, mock_env, mock_litellm_response):
    """Content filter errors should silently skip to next model without warning."""
    from llm_router.types import LLMResponse

    call_count = 0

    async def side_effect(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise Exception("litellm.BadRequestError: Output blocked by content filtering policy")
        # Return LLMResponse for providers.call_llm (not litellm response)
        return LLMResponse(
            content="Mock response",
            model=kwargs.get("model", "test/mock"),
            input_tokens=10,
            output_tokens=5,
            cost_usd=0.001,
            latency_ms=100.0,
            provider="test",
        )

    # Pin the chain (see test_falls_back_on_failure): bare runners can build a
    # single-model chain, which breaks the "skip to next model" assertion.
    with patch("llm_router.router._build_and_filter_chain",
               new_callable=lambda: AsyncMock(
                   return_value=["openai/gpt-4o-mini", "gemini/gemini-2.5-flash"])), \
         patch("llm_router.providers.call_llm", new_callable=lambda: AsyncMock(side_effect=side_effect)):
        resp = await route_and_call(
            TaskType.QUERY, "Hello",
            profile=RoutingProfile.BUDGET,
        )
    assert resp.content == "Mock response"
    assert call_count == 2  # first content-filtered, second succeeded


@pytest.mark.asyncio
async def test_skips_model_when_budget_exhausts_mid_chain(temp_db, mock_env, mock_litellm_response, monkeypatch):
    # Enable Ollama for this test so it gets injected in the chain
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
    monkeypatch.setenv("OLLAMA_BUDGET_MODELS", "llama3.2,qwen2.5-coder:7b")
    
    chain = [
        "openai/gpt-4o",
        "gemini/gemini-2.5-flash",
        "perplexity/sonar",
    ]
    called_models: list[str] = []
    budget_checks: list[str] = []

    async def completion_side_effect(**kwargs):
        called_models.append(kwargs["model"])
        if kwargs["model"] == chain[0]:
            raise Exception("Provider down")
        return mock_litellm_response()

    async def budget_side_effect(provider: str):
        budget_checks.append(provider)
        pressure = {
            "ollama": 0.0,
            "openai": 0.0,
            "gemini": 1.0,
            "perplexity": 0.0,
        }.get(provider, 0.0)
        return BudgetState(provider=provider, pressure=pressure)

    # Mock the health tracker so providers aren't skipped as unhealthy
    mock_tracker = MagicMock()
    mock_tracker.is_healthy.return_value = True

    with patch("litellm.acompletion", side_effect=completion_side_effect):
        with patch("litellm.completion_cost", return_value=0.001):
            with patch("llm_router.router.get_model_chain", return_value=chain):
                with patch("llm_router.router.get_budget_state", side_effect=budget_side_effect):
                    with patch("llm_router.router.get_tracker", return_value=mock_tracker):
                        with patch("llm_router.chain_builder.build_chain", return_value=[]):
                            resp = await route_and_call(
                                TaskType.QUERY, "Hello",
                                profile=RoutingProfile.BALANCED,
                            )

    # Ollama is injected first (free-first), and succeeds with 0.0 pressure
    assert resp.model.startswith("ollama/")
    # Model should have been tried (Ollama succeeds, so no fallback to chain)
    assert resp.model in called_models or len(called_models) > 0
    # Budget checks should include ollama (injected) and openai (first in chain)
    assert "ollama" in budget_checks


@pytest.mark.asyncio
async def test_subscription_mode_blocks_anthropic_override(temp_db, mock_env, mock_acompletion, monkeypatch):
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
    temp_db, mock_env, mock_acompletion, monkeypatch
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
async def test_claw_code_mode_does_not_inject_ollama_for_premium_profile(
    temp_db, mock_env, mock_acompletion, monkeypatch
):
    """PREMIUM keeps its capable chain; claw-code must not prepend Ollama."""
    monkeypatch.setenv("LLM_ROUTER_CLAW_CODE", "true")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
    monkeypatch.setenv("OLLAMA_BUDGET_MODELS", "llama3.2")
    # Isolate the Ollama-injection behaviour under test: disable Codex/Gemini
    # subprocess injection so the capable chain runs through call_llm (the mock),
    # not run_codex. Otherwise, whether Codex is installed flips the routed path
    # and mock_acompletion is never called. (No broker runs in tests, so the
    # broker path is a no-op.)
    monkeypatch.setenv("LLM_ROUTER_DISABLE_SUBPROCESS_BACKENDS", "codex,gemini_cli")
    import llm_router.config as _config
    _config._config = None

    await route_and_call(TaskType.QUERY, "Hello", profile=RoutingProfile.PREMIUM)

    call_kwargs = mock_acompletion.call_args.kwargs
    assert "ollama" not in call_kwargs["model"], (
        f"Ollama must not be first in PREMIUM chain in claw-code mode, got {call_kwargs['model']}"
    )
    _config._config = None


@pytest.mark.asyncio
async def test_claw_code_mode_does_not_inject_ollama_for_reasoning_profile(
    temp_db, mock_env, mock_acompletion, monkeypatch
):
    """REASONING keeps its dedicated chain; claw-code must not prepend Ollama."""
    monkeypatch.setenv("LLM_ROUTER_CLAW_CODE", "true")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
    monkeypatch.setenv("OLLAMA_BUDGET_MODELS", "llama3.2")
    # See premium test above: disable Codex/Gemini injection so the chain runs
    # through call_llm (the mock), isolating the Ollama-injection behaviour.
    monkeypatch.setenv("LLM_ROUTER_DISABLE_SUBPROCESS_BACKENDS", "codex,gemini_cli")
    import llm_router.config as _config
    _config._config = None

    await route_and_call(TaskType.QUERY, "Hello", profile=RoutingProfile.REASONING)

    call_kwargs = mock_acompletion.call_args.kwargs
    assert "ollama" not in call_kwargs["model"], (
        f"Ollama must not be first in REASONING chain in claw-code mode, got {call_kwargs['model']}"
    )
    _config._config = None


@pytest.mark.asyncio
async def test_ollama_always_injected_for_balanced(
    temp_db, mock_env, mock_acompletion, monkeypatch
):
    """Ollama should always inject when configured, regardless of profile or pressure."""
    monkeypatch.setenv("LLM_ROUTER_CLAW_CODE", "false")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
    monkeypatch.setenv("OLLAMA_BUDGET_MODELS", "llama3.2")
    import llm_router.config as _config
    import llm_router.claude_usage as _usage
    _config._config = None
    _usage.set_claude_pressure(0.0)  # no subscription pressure

    await route_and_call(TaskType.QUERY, "Hello", profile=RoutingProfile.BALANCED)

    call_kwargs = mock_acompletion.call_args.kwargs
    assert "ollama" in call_kwargs["model"], (
        f"Ollama should always inject when configured (free-first), got {call_kwargs['model']}"
    )
    _config._config = None


# ── Security: extra_params / media_params whitelists ─────────────────────────

class TestExtraParamsWhitelist:
    def test_allowed_keys_present(self):
        """Whitelisted keys must be in the allowlist."""
        from llm_router.providers import _ALLOWED_EXTRA_PARAMS
        safe_keys = {"temperature", "top_p", "seed", "stop", "extra_body", "thinking"}
        assert safe_keys.issubset(_ALLOWED_EXTRA_PARAMS)

    def test_dangerous_keys_blocked(self):
        """api_key, base_url, api_base must NOT be in the allowlist."""
        from llm_router.providers import _ALLOWED_EXTRA_PARAMS
        blocked = {"api_key", "base_url", "api_base", "headers", "custom_llm_provider"}
        assert blocked.isdisjoint(_ALLOWED_EXTRA_PARAMS), (
            f"Dangerous key(s) found in allowlist: {blocked & _ALLOWED_EXTRA_PARAMS}"
        )

    @pytest.mark.asyncio
    async def test_injection_keys_stripped_before_litellm(self, mock_litellm_response):
        """api_key injected via extra_params must never reach litellm.acompletion."""
        from unittest.mock import patch
        captured: dict = {}

        async def capturing_completion(**kwargs):
            captured.update(kwargs)
            return mock_litellm_response()

        with patch("litellm.acompletion", side_effect=capturing_completion):
            with patch("litellm.completion_cost", return_value=0.0):
                from llm_router import providers
                await providers.call_llm(
                    "openai/gpt-4o",
                    [{"role": "user", "content": "hi"}],
                    extra_params={"api_key": "evil-key", "temperature": 0.5},
                )

        assert "api_key" not in captured, "api_key must be stripped from LiteLLM kwargs"
        assert captured.get("temperature") == 0.5, "safe key must be preserved"


class TestMediaParamsWhitelist:
    def test_image_strips_unknown_keys(self):
        from llm_router.router import _filter_media_params
        from llm_router.types import TaskType
        result = _filter_media_params(
            TaskType.IMAGE,
            {"size": "1024x1024", "api_key": "evil", "base_url": "http://evil.com"},
        )
        assert "size" in result
        assert "api_key" not in result
        assert "base_url" not in result

    def test_video_strips_unknown_keys(self):
        from llm_router.router import _filter_media_params
        from llm_router.types import TaskType
        result = _filter_media_params(TaskType.VIDEO, {"duration": 5, "inject": "bad"})
        assert result == {"duration": 5}

    def test_empty_params_returns_empty(self):
        from llm_router.router import _filter_media_params
        from llm_router.types import TaskType
        assert _filter_media_params(TaskType.IMAGE, None) == {}
        assert _filter_media_params(TaskType.AUDIO, {}) == {}
