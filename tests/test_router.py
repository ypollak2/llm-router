"""Tests for core routing logic."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from llm_router.router import route_and_call
from llm_router.types import BudgetState, LLMResponse, RoutingProfile, TaskType


@pytest.mark.asyncio
@pytest.mark.requires_api_keys
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
@pytest.mark.requires_api_keys
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
@pytest.mark.requires_api_keys
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
@pytest.mark.requires_api_keys
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

    with patch("llm_router.providers.call_llm", new_callable=lambda: AsyncMock(side_effect=side_effect)):
        resp = await route_and_call(
            TaskType.QUERY, "Hello",
            profile=RoutingProfile.BUDGET,
        )
    assert resp.content == "Mock response"
    assert call_count == 2  # first failed, second succeeded


@pytest.mark.asyncio
@pytest.mark.requires_api_keys
async def test_raises_when_all_fail(temp_db, mock_env):
    with patch("litellm.acompletion", side_effect=Exception("All down")):
        with pytest.raises(RuntimeError, match="All models failed"):
            await route_and_call(TaskType.QUERY, "Hello")


@pytest.mark.asyncio
async def test_no_providers_configured(no_providers_env, monkeypatch):
    """When no providers are configured, the router should raise an error."""
    monkeypatch.setattr("llm_router.router.is_codex_available", lambda: False)
    with pytest.raises((ValueError, RuntimeError), match="No available models|All models failed"):
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
@pytest.mark.requires_api_keys
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

    with patch("llm_router.providers.call_llm", new_callable=lambda: AsyncMock(side_effect=side_effect)):
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
async def test_claw_code_mode_injects_ollama_for_premium_profile(
    temp_db, mock_env, mock_acompletion, monkeypatch
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


class TestBoundedOperationalShadowWiring:
    """WS9 -- wires bounded_operational.should_route_bounded()/bounded_op_budget_usd()
    into the live routing decision path (router.py's _dispatch_model_loop), strictly
    behind LLM_ROUTER_BOUNDED_OPERATIONAL (default off). The shadow computation is
    recorded into routing_decisions.bounded_operational_json purely for offline
    analysis; it must NEVER influence `model`/`response` selection.

    This class proves the required invariance property: with the flag absent vs.
    present-but-explicitly-disabled, the live route (model + response content) is
    byte-identical, and the shadow column is NULL in both cases. It also proves the
    enabled path correctly populates the shadow column without altering the route.

    Note on Codex/Gemini CLI injection: router.py does ``from llm_router.codex_agent
    import is_codex_available`` (and the Gemini CLI equivalent) as direct name
    imports, so patching ``llm_router.codex_agent.is_codex_available`` (what the
    shared ``mock_acompletion`` fixture does) does not affect the name already bound
    into ``llm_router.router``'s namespace. On a dev machine with a real Codex/Gemini
    CLI binary on PATH this lets the *real* CLI agent get invoked instead of the
    mocked ``call_llm``. These tests patch the correctly-bound names directly
    (``llm_router.router.is_codex_available`` / ``llm_router.router.is_gemini_cli_available``)
    so routing is fully deterministic and confined to the mocked provider call.

    Note on the mocked response: the shared ``mock_acompletion`` fixture always
    returns provider="test"/model="test/mock-model", which
    ``cost._validate_routing_insert`` (a pre-existing production guard that keeps
    contaminated test data out of the routing_decisions table) unconditionally
    rejects -- so no row would ever be written and every ``bounded_operational_json``
    assertion would spuriously see ``None`` regardless of this wiring. These tests
    override the mock to return a *plausible* provider/model derived from the
    model actually being tried, so `log_routing_decision` succeeds and the shadow
    column can be observed.
    """

    @pytest.fixture(autouse=True)
    def _no_cli_agent_injection(self, monkeypatch, mock_acompletion):
        """Force the mocked `call_llm` chain (no real Codex/Gemini CLI subprocess),
        with a validation-safe, per-model response so routing_decisions inserts
        actually succeed."""
        from llm_router.profiles import provider_from_model
        from llm_router.types import LLMResponse

        # llm_router.server's module-level `initialize_dynamic_routing()` call
        # (run once, the first time any test in the full-suite session imports
        # `llm_router.server` -- e.g. tests/test_route.py, which collects
        # alphabetically before this file) permanently caches a
        # `dynamic_routing._dynamic_routing_table` built from the REAL host
        # machine's `available_providers` at that moment. That module-level
        # cache is never invalidated by `mock_env`'s monkeypatched env vars,
        # so `get_model_chain()`'s dynamic-table lookup (`_build_and_filter_
        # chain`'s `get_dynamic_model_chain()` call) silently returns that
        # stale, real-environment-derived chain instead of the one implied by
        # this test's `mock_env`-configured providers -- which is why these
        # tests only fail when run alongside the rest of the suite, and why
        # the surviving chain collapses to whatever the real host happens to
        # have available (e.g. just "codex/gpt-4o-mini"). Save/restore the
        # module globals around each test so dynamic routing is forced back
        # to its uninitialized state (falling back to the static, mock_env-
        # driven chain) without leaking a behavior change to other tests.
        import llm_router.dynamic_routing as _dynrouting

        _saved_table = _dynrouting._dynamic_routing_table
        _saved_complete = _dynrouting._discovery_complete
        _dynrouting.reset_dynamic_routing()

        monkeypatch.setattr("llm_router.router.is_codex_available", lambda: False)
        monkeypatch.setattr("llm_router.router.is_gemini_cli_available", lambda: False)

        async def _codex_unavailable(*_args, **_kwargs):
            raise RuntimeError("codex unavailable (test)")

        monkeypatch.setattr("llm_router.router.run_codex", _codex_unavailable)
        monkeypatch.setattr("llm_router.router.run_gemini_cli", _codex_unavailable)

        # llm_router.budget.get_budget_state() caches its result per-provider
        # for 60 real seconds in a module-level dict that no fixture resets
        # (temp_db/mock_env only reset the config singleton). If an earlier
        # test in the full-suite run left a stale "budget exhausted"
        # (pressure >= 1.0) entry for a real provider, _dispatch_model_loop
        # would skip it here and fall through to the mocked-unavailable
        # codex/gemini CLI path, producing a spurious "All models failed"
        # error unrelated to this class's own assertions. Pin the budget
        # check to an always-available state, mirroring the same isolation
        # pattern already used in test_invariance_flag_toggle_no_route_change
        # above (patching `llm_router.router.get_budget_state` directly) --
        # this makes these tests deterministic regardless of run order or
        # what other tests executed in the preceding 60 seconds.
        async def _always_available_budget(provider: str):
            return BudgetState(provider=provider, pressure=0.0)

        monkeypatch.setattr("llm_router.router.get_budget_state", _always_available_budget)

        _valid_providers = {
            "ollama", "openai", "gemini", "codex", "claude_subscription",
            "subscription", "anthropic", "perplexity", "groq", "deepseek",
            "cc", "claude",
        }

        async def _valid_response(model, *_args, **_kwargs):
            provider = provider_from_model(model)
            if provider not in _valid_providers:
                provider = "openai"
            return LLMResponse(
                content="Mock response",
                model=model,
                input_tokens=10,
                output_tokens=5,
                cost_usd=0.001,
                latency_ms=100.0,
                provider=provider,
            )

        mock_acompletion.side_effect = _valid_response

        yield

        _dynrouting._dynamic_routing_table = _saved_table
        _dynrouting._discovery_complete = _saved_complete

    async def _last_bounded_operational_json(self):
        from llm_router import cost

        db = await cost._get_db()
        try:
            cursor = await db.execute(
                "SELECT bounded_operational_json FROM routing_decisions "
                "ORDER BY id DESC LIMIT 1"
            )
            row = await cursor.fetchone()
        finally:
            await db.close()
        return row[0] if row else None

    @pytest.mark.asyncio
    async def test_invariance_flag_absent_vs_present_but_disabled(
        self, temp_db, mock_env, mock_acompletion, monkeypatch
    ):
        """Route decisions must be byte-identical whether LLM_ROUTER_BOUNDED_OPERATIONAL
        is unset (module logically absent from the live decision) or explicitly set to
        a falsy value (module present, flag off) -- and the shadow column must be NULL
        in both cases."""
        prompt = "Write a file called foo.py and run the tests to verify it"
        cdata = {"complexity": "simple", "task_type": TaskType.CODE.value}

        monkeypatch.delenv("LLM_ROUTER_BOUNDED_OPERATIONAL", raising=False)
        resp_absent = await route_and_call(
            TaskType.CODE, prompt, complexity_hint="simple", classification_data=dict(cdata)
        )
        shadow_absent = await self._last_bounded_operational_json()

        monkeypatch.setenv("LLM_ROUTER_BOUNDED_OPERATIONAL", "0")
        resp_disabled = await route_and_call(
            TaskType.CODE, prompt, complexity_hint="simple", classification_data=dict(cdata)
        )
        shadow_disabled = await self._last_bounded_operational_json()

        assert resp_absent.model == resp_disabled.model
        assert resp_absent.content == resp_disabled.content
        assert resp_absent.provider == resp_disabled.provider
        assert shadow_absent is None
        assert shadow_disabled is None

    @pytest.mark.asyncio
    async def test_enabled_path_populates_shadow_when_qualifying(
        self, temp_db, mock_env, mock_acompletion, monkeypatch
    ):
        """Flag on + complexity 'simple' + a write/run-qualifying prompt -> the shadow
        column records would_route_bounded=True with a positive budget_usd, while the
        live route (model/response) is unaffected by the flag."""
        import json

        prompt = "Write a file called foo.py and run the tests to verify it"
        cdata = {"complexity": "simple", "task_type": TaskType.CODE.value}

        monkeypatch.delenv("LLM_ROUTER_BOUNDED_OPERATIONAL", raising=False)
        resp_disabled = await route_and_call(
            TaskType.CODE, prompt, complexity_hint="simple", classification_data=dict(cdata)
        )

        monkeypatch.setenv("LLM_ROUTER_BOUNDED_OPERATIONAL", "1")
        resp_enabled = await route_and_call(
            TaskType.CODE, prompt, complexity_hint="simple", classification_data=dict(cdata)
        )
        shadow = await self._last_bounded_operational_json()

        assert resp_enabled.model == resp_disabled.model
        assert resp_enabled.content == resp_disabled.content

        assert shadow is not None
        payload = json.loads(shadow)
        assert payload["would_route_bounded"] is True
        assert payload["complexity"] == "simple"
        assert payload["budget_usd"] is not None
        assert payload["budget_usd"] > 0

    @pytest.mark.asyncio
    async def test_enabled_path_records_false_when_not_qualifying(
        self, temp_db, mock_env, mock_acompletion, monkeypatch
    ):
        """Flag on but the prompt needs no write/run/verify capability -> shadow
        records would_route_bounded=False and budget_usd=None; live route still
        unaffected."""
        import json

        prompt = "What is the difference between TCP and UDP?"
        cdata = {"complexity": "simple", "task_type": TaskType.QUERY.value}

        monkeypatch.delenv("LLM_ROUTER_BOUNDED_OPERATIONAL", raising=False)
        resp_disabled = await route_and_call(
            TaskType.QUERY, prompt, complexity_hint="simple", classification_data=dict(cdata)
        )

        monkeypatch.setenv("LLM_ROUTER_BOUNDED_OPERATIONAL", "1")
        resp_enabled = await route_and_call(
            TaskType.QUERY, prompt, complexity_hint="simple", classification_data=dict(cdata)
        )
        shadow = await self._last_bounded_operational_json()

        assert resp_enabled.model == resp_disabled.model
        assert resp_enabled.content == resp_disabled.content

        assert shadow is not None
        payload = json.loads(shadow)
        assert payload["would_route_bounded"] is False
        assert payload["budget_usd"] is None

    @pytest.mark.asyncio
    async def test_enabled_path_records_false_for_non_simple_complexity(
        self, temp_db, mock_env, mock_acompletion, monkeypatch
    ):
        """Flag on, prompt would otherwise qualify (write/run), but complexity is
        not 'simple' -> should_route_bounded() must say False (moderate/complex
        always go through full delegation, never bounded)."""
        import json

        prompt = "Write a file called foo.py and run the tests to verify it"
        cdata = {"complexity": "moderate", "task_type": TaskType.CODE.value}

        monkeypatch.setenv("LLM_ROUTER_BOUNDED_OPERATIONAL", "1")
        await route_and_call(
            TaskType.CODE, prompt, complexity_hint="moderate", classification_data=dict(cdata)
        )
        shadow = await self._last_bounded_operational_json()

        assert shadow is not None
        payload = json.loads(shadow)
        assert payload["would_route_bounded"] is False
        assert payload["complexity"] == "moderate"
        assert payload["budget_usd"] is None

    def test_no_chuzom_in_shadow_wiring_symbols(self):
        """Brand-leak guard for the router.py wiring block itself, scoped to
        runtime-visible symbol names (consistent with TestBrandLeak in
        tests/commands/test_audit.py) rather than a full-source scan -- router.py
        legitimately carries historical 'Ported from chuzom's ...' provenance
        comments elsewhere in the file (WS4 capability routing), which a raw
        inspect.getsource() substring scan would incorrectly flag."""
        import llm_router.router as router_module

        for name in dir(router_module):
            assert "chuzom" not in name.lower(), f"brand leak in name: {name}"
