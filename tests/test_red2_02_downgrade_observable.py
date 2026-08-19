"""Regression: RED2-02 — a daily-cap downgrade must be observable on the response.

The TQ-007 downgrade mechanism was correct but invisible: RouteResult/LLMResponse
had no field recording that a cap forced a cheaper local route, so a user saw an
unexplained quality drop. LLMResponse now carries `cap_downgraded` +
`cap_downgrade_reason`, set by route_and_call when the downgrade fires.
"""
from __future__ import annotations

import os
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import tests.test_tq007_daily_cap_downgrade as t
from llm_router.repo_config import RepoConfig
from llm_router.types import RoutingProfile, TaskType


async def _run(chain, *, task_cap=None, enforce="hard"):
    caps = {"code": task_cap} if task_cap is not None else {}
    repo_cfg = RepoConfig(daily_caps=caps, enforce=enforce)
    with ExitStack() as es:
        p = es.enter_context
        p(patch.dict(os.environ, {"LLM_ROUTER_ENFORCE": enforce}))
        p(patch("llm_router.router.get_config", return_value=t._Cfg()))
        tr = MagicMock()
        tr.is_healthy.return_value = True
        p(patch("llm_router.router.get_tracker", return_value=tr))
        ml = MagicMock()
        ml.bind.return_value = MagicMock()
        p(patch("llm_router.router.log", ml))
        p(patch("llm_router.router._native_notify", lambda *a, **k: None))
        p(patch("llm_router.repo_config.effective_config", return_value=repo_cfg))
        p(patch("llm_router.router.cost.get_monthly_spend", new_callable=AsyncMock, return_value=0.0))
        spend = 9999.0 if task_cap is not None else 0.0
        p(patch("llm_router.router.cost.get_daily_spend", new_callable=AsyncMock, return_value=spend))
        p(patch("llm_router.router.cost.get_daily_spend_by_task_type", new_callable=AsyncMock, return_value=spend))
        p(patch("llm_router.policy.load_org_policy", return_value=None))
        p(patch("llm_router.policy.get_active_policy", return_value=None))
        p(patch("llm_router.router.cost.log_usage", new_callable=AsyncMock))
        p(patch("llm_router.router.reserve_envelope", new_callable=AsyncMock, return_value=(None, True, None)))
        p(patch("llm_router.router.commit_envelope", new_callable=AsyncMock))
        p(patch("llm_router.router.release_envelope", new_callable=AsyncMock))
        p(patch("llm_router.semantic_cache.check", new_callable=AsyncMock, return_value=None))
        p(patch("llm_router.semantic_cache.store", new_callable=AsyncMock))
        p(patch("llm_router.router._build_and_filter_chain", new_callable=AsyncMock, return_value=list(chain)))
        p(patch("llm_router.router.providers.call_llm", new_callable=AsyncMock,
                side_effect=lambda model, messages, **kw: t._response(model)))
        from llm_router.router import route_and_call
        return await route_and_call(TaskType.CODE, "hello", profile=RoutingProfile.BALANCED)


@pytest.mark.asyncio
async def test_downgraded_response_is_flagged():
    resp = await _run(["openai/gpt-4o", "ollama/qwen2.5:7b"], task_cap=0.0001)
    assert resp.provider == "ollama"
    assert resp.cap_downgraded is True, "cap downgrade not surfaced on the response"
    assert "cap" in resp.cap_downgrade_reason.lower() or "limit" in resp.cap_downgrade_reason.lower()


@pytest.mark.asyncio
async def test_normal_response_not_flagged():
    resp = await _run(["openai/gpt-4o"], task_cap=None)
    assert resp.cap_downgraded is False
    assert resp.cap_downgrade_reason == ""
