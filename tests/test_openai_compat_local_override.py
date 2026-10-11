"""LOCAL-OVERRIDE-1: ``openai_compat`` (a self-hosted OpenAI-compatible server) as an
explicit-call backend.

Found 2026-10-11 by calling a local MLX server through ``model_override``:
  1. a failed local override walked the BUDGET emergency chain, which can hold paid
     cloud models, so a prompt meant for the local server left the machine;
  2. the call's ``routing_decisions`` row was rejected (``invalid provider
     'openai_compat'``), so the call was never counted;
  3. bookkeeping called it paid: tier ``paid_api``, cost not forced to $0, and no
     free-local quota bucket.

Owner decision: only LOCAL overrides lose the fallback. A failed cloud override still
falls back, and the control test below pins that scope.
"""
from __future__ import annotations

import os
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import tests.test_tq007_daily_cap_downgrade as t
from llm_router import cost
from llm_router import router as router_module
from llm_router.quota_savings import _FREE_LOCAL_PROVIDERS
from llm_router.tiers import tier_of
from llm_router.types import LLMResponse, RoutingProfile, TaskType
from tests.test_cost import _last_usage_row
from tests.test_mcp_qa_no_local import _response

LOCAL = "openai_compat//Users/me/models/Qwen-MLX/6-bit"
CLOUD = "openai/gpt-4o"
EMERGENCY = ["openai/gpt-4o-mini", "gemini/gemini-2.5-flash"]


async def _run_failing_override(model_override, profile=RoutingProfile.BALANCED, task_type=TaskType.CODE):
    """route_and_call with ``model_override`` failing and a cloud-only BUDGET chain.

    Same stubs as ``tests/test_mcp_qa_no_local._run``, which cannot be reused here:
    its call list is lost when route_and_call raises, and a failed local override
    must raise. Returns (called models, raised exception, log mock, route_log mock).
    """
    called: list[str] = []

    async def fake_call_llm(model, *a, **k):
        called.append(model)
        if model == model_override:
            raise RuntimeError(f"stub failure for {model}")
        return _response(model)

    async def fake_call_media(task_type, provider, model_name, *a, **k):
        # Media task types are dispatched through _call_media, not call_llm.
        return await fake_call_llm(f"{provider}/{model_name}")

    async def fake_build(task_type, profile, override, *a, **k):
        # Like the real builder: an override is the whole chain, whatever the profile;
        # the emergency build passes override=None and gets the cloud BUDGET chain.
        return [override] if override else list(EMERGENCY)

    mock_log = MagicMock()
    route_log = MagicMock()
    mock_log.bind.return_value = route_log
    tracker = MagicMock()
    tracker.is_healthy.return_value = True
    env = {"LLM_ROUTER_ENFORCE": "off", "LLM_ROUTER_BANDIT": "off"}
    raised = None

    with ExitStack() as es:
        p = es.enter_context
        p(patch.dict(os.environ, env))
        p(patch("llm_router.router.get_config", return_value=t._Cfg()))
        p(patch("llm_router.router.get_tracker", return_value=tracker))
        p(patch("llm_router.router.log", mock_log))
        p(patch("llm_router.router._native_notify", lambda *a, **k: None))
        for fn in ("get_monthly_spend", "get_daily_spend", "get_daily_spend_by_task_type"):
            p(patch(f"llm_router.router.cost.{fn}", new_callable=AsyncMock, return_value=0.0))
        p(patch("llm_router.router.cost.log_usage", new_callable=AsyncMock))
        p(patch("llm_router.policy.load_org_policy", return_value=None))
        p(patch("llm_router.policy.get_active_policy", return_value=None))
        p(patch("llm_router.router.reserve_envelope", new_callable=AsyncMock, return_value=(None, True, "k")))
        p(patch("llm_router.router.commit_envelope", new_callable=AsyncMock))
        p(patch("llm_router.router.release_envelope", new_callable=AsyncMock))
        p(patch("llm_router.semantic_cache.check", new_callable=AsyncMock, return_value=None))
        p(patch("llm_router.semantic_cache.store", new_callable=AsyncMock))
        p(patch("llm_router.router._build_and_filter_chain", side_effect=fake_build))
        p(patch("llm_router.router.providers.call_llm", side_effect=fake_call_llm))
        p(patch("llm_router.router._call_media", side_effect=fake_call_media))
        try:
            await router_module.route_and_call(
                task_type, "hello", profile=profile,
                complexity_hint="moderate", model_override=model_override,
            )
        except Exception as exc:  # the outcome under test, inspected by the caller
            raised = exc
        finally:
            await router_module.drain_bg_tasks(2.0)
    return called, raised, mock_log, route_log


def _skip_events(route_log):
    return [c for c in route_log.info.call_args_list if c.args[:1] == ("emergency_fallback_skipped",)]


@pytest.mark.asyncio
async def test_a_failed_local_override_does_not_reach_the_cloud():
    called, raised, _, route_log = await _run_failing_override(LOCAL)
    assert called == [LOCAL], called
    assert raised is not None and f"stub failure for {LOCAL}" in str(raised), repr(raised)
    events = _skip_events(route_log)
    assert len(events) == 1, route_log.info.call_args_list
    assert events[0].kwargs["reason"] == "local_model_override"
    assert events[0].kwargs["model"] == LOCAL


@pytest.mark.asyncio
async def test_a_failed_cloud_override_still_falls_back():
    """Control and scope pin: the harness does reach the emergency chain, and the guard
    is limited to local overrides (owner decision)."""
    called, raised, _, route_log = await _run_failing_override(CLOUD)
    assert called[0] == CLOUD, called
    assert called[1:2] == EMERGENCY[:1], called
    assert raised is None
    assert _skip_events(route_log) == []


@pytest.mark.asyncio
async def test_a_bare_local_provider_override_is_guarded_too():
    """``model_override="ollama"`` (no slash) passes the format check, so the guard must
    read the provider from the bare name, not through ``provider_from_model``."""
    called, raised, _, route_log = await _run_failing_override("ollama")
    assert called == ["ollama"], called
    assert raised is not None and "stub failure for ollama" in str(raised), repr(raised)
    assert [e.kwargs["reason"] for e in _skip_events(route_log)] == ["local_model_override"]


@pytest.mark.asyncio
async def test_no_skip_is_logged_where_no_fallback_would_run():
    """A BUDGET call never had an emergency fallback, so it must not log a skip."""
    called, raised, _, route_log = await _run_failing_override(LOCAL, profile=RoutingProfile.BUDGET)
    assert called == [LOCAL], called
    assert raised is not None
    assert _skip_events(route_log) == []


@pytest.mark.asyncio
async def test_no_skip_is_logged_for_a_media_task():
    """Media task types never had an emergency fallback either (mutation M7 of the
    independent red-check left the media half of the condition unpinned)."""
    called, raised, _, route_log = await _run_failing_override(LOCAL, task_type=TaskType.IMAGE)
    assert called == [LOCAL], called
    assert raised is not None
    assert _skip_events(route_log) == []


def test_openai_compat_is_a_free_local_tier():
    assert tier_of(LOCAL) == "free_local"


def test_openai_compat_is_in_the_free_local_quota_bucket():
    assert "openai_compat" in _FREE_LOCAL_PROVIDERS


@pytest.mark.asyncio
async def test_an_openai_compat_usage_row_costs_nothing(temp_db):
    """A cost litellm computes for a self-hosted server is not money spent."""
    resp = LLMResponse(
        content="x", model=LOCAL, input_tokens=100, output_tokens=50,
        cost_usd=0.25, latency_ms=500.0, provider="openai_compat",
    )
    await cost.log_usage(resp, TaskType.CODE, RoutingProfile.BUDGET, complexity="moderate")
    row = await _last_usage_row(temp_db)
    assert row["cost_usd"] == 0.0
