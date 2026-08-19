"""Regression: CHZ-AUD-A-01 — failed provider attempts must be recorded in the
execution ledger as attempt_failed events (were previously invisible)."""
import os
import sqlite3
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import tests.test_tq007_daily_cap_downgrade as t
from llm_router import router
from llm_router.types import LLMResponse, RoutingProfile, TaskType


@pytest.mark.asyncio
async def test_failed_attempts_emit_attempt_failed(tmp_path):
    ledger_db = tmp_path / "ledger.db"
    calls = []

    async def fake_call_llm(model, *a, **k):
        calls.append(model)
        if len(calls) <= 2:
            raise RuntimeError(f"boom-{model}")
        return LLMResponse(content="ok", model=model, input_tokens=1, output_tokens=1,
                           cost_usd=0.0, latency_ms=1.0, provider=model.split("/")[0])

    with ExitStack() as es:
        p = es.enter_context
        p(patch.dict(os.environ, {"LLM_ROUTER_ENFORCE": "off",
                                  "LLM_ROUTER_EXECUTION_LEDGER_DB": str(ledger_db),
                                  "LLM_ROUTER_SESSION_ID": "a01sess"}))
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
        p(patch("llm_router.quality_feedback.should_skip_model", return_value=False))
        p(patch("llm_router.router._build_and_filter_chain", new_callable=AsyncMock,
                return_value=["ollama/a", "ollama/b", "openai/gpt-4o"]))
        p(patch("llm_router.router.providers.call_llm", side_effect=fake_call_llm))
        try:
            resp = await router.route_and_call(TaskType.CODE, "hello", profile=RoutingProfile.BALANCED)
        finally:
            await router.drain_bg_tasks(3.0)

    assert resp.content == "ok" and len(calls) == 3
    con = sqlite3.connect(str(ledger_db))
    rows = con.execute("SELECT event_type, COUNT(*) FROM execution_events GROUP BY event_type").fetchall()
    counts = dict(rows)
    assert counts.get("attempt_failed", 0) == 2, f"expected 2 attempt_failed, got {counts}"
    assert counts.get("attempt_completed", 0) == 1, f"expected 1 attempt_completed, got {counts}"


@pytest.mark.asyncio
async def test_emergency_budget_fallback_failure_also_emits_attempt_failed(tmp_path):
    """CHZ-AUD-A-01 sibling: a provider failure in the EMERGENCY BUDGET fallback
    loop must also be recorded as attempt_failed — not only failures in the
    primary chain. Drives primary-chain exhaustion so the budget fallback runs
    and also fails, then reads the real ledger."""
    from llm_router.types import RoutingProfile as _RP

    ledger_db = tmp_path / "ledger.db"
    calls = []

    async def always_fail(model, *a, **k):
        calls.append(model)
        raise RuntimeError(f"boom-{model}")

    async def two_chains(task_type_, profile_, *a, **k):
        # primary (BALANCED) exhausts, then the emergency BUDGET chain is built.
        return ["ollama/eb1"] if profile_ == _RP.BUDGET else ["ollama/p1"]

    with ExitStack() as es:
        p = es.enter_context
        p(patch.dict(os.environ, {"LLM_ROUTER_ENFORCE": "off",
                                  "LLM_ROUTER_EXECUTION_LEDGER_DB": str(ledger_db),
                                  "LLM_ROUTER_SESSION_ID": "a01emerg"}))
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
        p(patch("llm_router.quality_feedback.should_skip_model", return_value=False))
        p(patch("llm_router.router._build_and_filter_chain", side_effect=two_chains))
        p(patch("llm_router.router.providers.call_llm", side_effect=always_fail))
        with pytest.raises(Exception):
            await router.route_and_call(TaskType.CODE, "hello", profile=RoutingProfile.BALANCED)
        await router.drain_bg_tasks(3.0)

    assert "ollama/p1" in calls and "ollama/eb1" in calls, f"emergency fallback not exercised: {calls}"
    con = sqlite3.connect(str(ledger_db))
    counts = dict(con.execute(
        "SELECT event_type, COUNT(*) FROM execution_events GROUP BY event_type").fetchall())
    # Both the primary AND the emergency-fallback failure must be recorded.
    assert counts.get("attempt_failed", 0) == 2, \
        f"expected 2 attempt_failed (primary + emergency), got {counts}"
