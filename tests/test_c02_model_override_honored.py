"""Regression: CHZ-AUD-C-02 — an explicit model_override must be honored exactly,
never silently substituted by the process-global quality circuit-breaker."""
import os
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import tests.test_tq007_daily_cap_downgrade as t
from llm_router import router
from llm_router.types import LLMResponse, RoutingProfile, TaskType


@pytest.mark.asyncio
async def test_override_bypasses_circuit_breaker():
    called = []

    async def fake_call_llm(model, *a, **k):
        called.append(model)
        return LLMResponse(content="OVERRIDE_RAN", model=model, input_tokens=1,
                           output_tokens=1, cost_usd=0.0, latency_ms=1.0,
                           provider=model.split("/")[0])

    with ExitStack() as es:
        p = es.enter_context
        p(patch.dict(os.environ, {"LLM_ROUTER_ENFORCE": "off"}))
        p(patch("llm_router.router.get_config", return_value=t._Cfg()))
        tr = MagicMock()
        tr.is_healthy.return_value = True
        p(patch("llm_router.router.get_tracker", return_value=tr))
        ml = MagicMock()
        ml.bind.return_value = MagicMock()
        p(patch("llm_router.router.log", ml))
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
        # The circuit breaker trips for EVERY model — must NOT affect the override.
        p(patch("llm_router.quality_feedback.should_skip_model", return_value=True))
        p(patch("llm_router.router.providers.call_llm", side_effect=fake_call_llm))
        try:
            resp = await router.route_and_call(
                TaskType.CODE, "hello", profile=RoutingProfile.BALANCED,
                model_override="openai/gpt-4o",
            )
        finally:
            await router.drain_bg_tasks(2.0)

    assert "openai/gpt-4o" in called, (
        "CHZ-AUD-C-02: the explicit override was skipped by the circuit breaker "
        f"(silent substitution). Models actually called: {called}"
    )
    assert resp.content == "OVERRIDE_RAN"
