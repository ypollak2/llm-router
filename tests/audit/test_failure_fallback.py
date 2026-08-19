from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from llm_router.router import route_and_call
from llm_router.types import LLMResponse, RoutingProfile, TaskType


class _RouteConfig:
    llm_router_profile = RoutingProfile.BALANCED
    llm_router_monthly_budget = 0.0
    llm_router_daily_spend_limit = 0.0
    llm_router_escalate_above = 0.0
    llm_router_hard_stop_above = 0.0
    llm_router_claude_subscription = False
    llm_router_gemini_subscription = False
    llm_router_claw_code = False
    llm_router_routing_policy = "balanced"
    llm_router_agentic_model = ""
    codex_daily_limit = 1000
    compaction_mode = "off"
    compaction_threshold = 4000
    prompt_cache_enabled = False
    prompt_cache_min_tokens = 1024
    context_enabled = False
    caveman_mode = "off"
    available_providers = {"openai", "gemini"}


class RateLimitError(Exception):
    pass


@pytest.fixture
def routed_runtime(monkeypatch, temp_db):
    monkeypatch.setenv("LLM_ROUTER_BANDIT", "off")
    # These audit tests exercise the error/fallback path with mocked chains and
    # fixed model counts. Quality-gated escalation (default-on) would add a
    # second in-chain attempt on a low-scoring answer, changing the fallback
    # event count — so disable it to isolate the fallback behaviour under test.
    monkeypatch.setenv("LLM_ROUTER_ESCALATE_ON_QUALITY", "0")
    route_log = MagicMock()
    mock_log = MagicMock()
    mock_log.bind.return_value = route_log
    tracker = MagicMock()
    tracker.is_healthy.return_value = True

    patches = [
        patch("llm_router.router.get_config", return_value=_RouteConfig()),
        patch("llm_router.router.get_tracker", return_value=tracker),
        patch("llm_router.router.log", mock_log),
        patch("llm_router.router._native_notify", lambda *_args, **_kwargs: None),
        patch("llm_router.router.cost.get_monthly_spend", new_callable=AsyncMock, return_value=0.0),
        patch("llm_router.router.cost.get_daily_spend", new_callable=AsyncMock, return_value=0.0),
        patch("llm_router.router.cost.get_daily_spend_by_task_type", new_callable=AsyncMock, return_value=0.0),
        patch("llm_router.router.cost.log_usage", new_callable=AsyncMock),
        patch("llm_router.router.reserve_envelope", new_callable=AsyncMock, return_value=(None, True, None)),
        patch("llm_router.router.commit_envelope", new_callable=AsyncMock),
        patch("llm_router.router.release_envelope", new_callable=AsyncMock),
        patch("llm_router.semantic_cache.check", new_callable=AsyncMock, return_value=None),
        patch("llm_router.semantic_cache.store", new_callable=AsyncMock),
    ]
    entered = [p.start() for p in patches]
    try:
        yield SimpleNamespace(route_log=route_log, tracker=tracker, entered=entered)
    finally:
        for p in reversed(patches):
            p.stop()


def _response(model: str) -> LLMResponse:
    return LLMResponse(
        content=f"ok from {model}",
        model=model,
        input_tokens=7,
        output_tokens=3,
        cost_usd=0.001,
        latency_ms=15.0,
        provider=model.split("/", 1)[0],
    )


def _fallback_events(route_log: MagicMock):
    return [
        call for call in route_log.warning.call_args_list
        if call.args and call.args[0] == "routing_fallback"
    ]


@pytest.mark.asyncio
async def test_single_model_failure_falls_through_and_is_logged(routed_runtime):
    async def call_llm(model, messages, **kwargs):
        if model == "openai/gpt-4o":
            raise RuntimeError("provider down")
        return _response(model)

    with (
        patch("llm_router.router._build_and_filter_chain", new_callable=AsyncMock, return_value=[
            "openai/gpt-4o",
            "gemini/gemini-2.5-flash",
        ]),
        patch("llm_router.router.providers.call_llm", new_callable=AsyncMock, side_effect=call_llm),
    ):
        response = await route_and_call(TaskType.QUERY, "hello", profile=RoutingProfile.BALANCED)

    assert response.model == "gemini/gemini-2.5-flash"
    events = _fallback_events(routed_runtime.route_log)
    assert events
    assert events[0].kwargs["fallback_reason"] == "provider_error"


@pytest.mark.asyncio
async def test_all_models_failing_raises_clear_terminal_error_without_hanging(
    routed_runtime,
):
    async def fail_all(model, messages, **kwargs):
        raise RuntimeError(f"{model} unavailable")

    with (
        patch("llm_router.router._build_and_filter_chain", new_callable=AsyncMock, return_value=[
            "openai/gpt-4o",
            "gemini/gemini-2.5-flash",
        ]),
        patch("llm_router.router.providers.call_llm", new_callable=AsyncMock, side_effect=fail_all),
    ):
        with pytest.raises(RuntimeError, match=r"(?s)All models failed.*Chain failures"):
            await asyncio.wait_for(
                route_and_call(TaskType.QUERY, "hello", profile=RoutingProfile.BUDGET),
                timeout=2.0,
            )

    assert len(_fallback_events(routed_runtime.route_log)) == 2


@pytest.mark.asyncio
async def test_rate_limit_error_is_fallback_worthy(routed_runtime):
    async def call_llm(model, messages, **kwargs):
        if model == "openai/gpt-4o":
            raise RateLimitError("429 rate limit reached")
        return _response(model)

    with (
        patch("llm_router.router._build_and_filter_chain", new_callable=AsyncMock, return_value=[
            "openai/gpt-4o",
            "gemini/gemini-2.5-flash",
        ]),
        patch("llm_router.router.providers.call_llm", new_callable=AsyncMock, side_effect=call_llm),
    ):
        response = await route_and_call(TaskType.QUERY, "hello", profile=RoutingProfile.BALANCED)

    assert response.model == "gemini/gemini-2.5-flash"
    events = _fallback_events(routed_runtime.route_log)
    assert events
    assert events[0].kwargs["fallback_reason"] == "rate_limit"
