"""RED-1 re-audit: a failure INSIDE _finalize_successful_route (telemetry that
runs AFTER the response is generated + billed) must never fail the routed turn.

Finding (d): on the primary chain, a finalize exception was caught by the generic
per-model provider-error handler → logged as "model failed" → wrote a CONTRADICTORY
attempt_failed for an already-attempt_completed model → discarded the billed
response and made a second real provider call. Finding (c): on the idempotency
dedupe path, a finalize/identity-resolution failure propagated out of route_and_call,
breaking the documented fail-open guarantee.
"""
import os
import sqlite3
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import tests.test_tq007_daily_cap_downgrade as t
from llm_router import router
from llm_router.types import LLMResponse, RoutingProfile, TaskType


def _common_patches(es, ledger_db):
    p = es.enter_context
    p(patch.dict(os.environ, {"LLM_ROUTER_ENFORCE": "off",
                              "LLM_ROUTER_EXECUTION_LEDGER_DB": str(ledger_db),
                              "LLM_ROUTER_SESSION_ID": "finalsess"}))
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
    return p


@pytest.mark.asyncio
async def test_primary_finalize_failure_not_misclassified_as_provider_error(tmp_path):
    ledger_db = tmp_path / "ledger.db"
    calls = []

    async def fake_call_llm(model, *a, **k):
        calls.append(model)
        return LLMResponse(content="ok", model=model, input_tokens=1, output_tokens=1,
                           cost_usd=0.0, latency_ms=1.0, provider=model.split("/")[0])

    with ExitStack() as es:
        p = _common_patches(es, ledger_db)
        p(patch("llm_router.router._build_and_filter_chain", new_callable=AsyncMock,
                return_value=["ollama/a", "ollama/b"]))
        p(patch("llm_router.router.providers.call_llm", side_effect=fake_call_llm))
        # Finalization (telemetry) blows up — must NOT fail or re-route the turn.
        p(patch("llm_router.router._finalize_successful_route", new_callable=AsyncMock,
                side_effect=RuntimeError("boom-finalize")))
        resp = await router.route_and_call(TaskType.CODE, "hello", profile=RoutingProfile.BALANCED)
        await router.drain_bg_tasks(3.0)

    assert resp.content == "ok", "a billed response was discarded on a finalize failure"
    assert len(calls) == 1, f"a second provider call was made to recover: {calls}"
    counts = dict(sqlite3.connect(str(ledger_db)).execute(
        "SELECT event_type, COUNT(*) FROM execution_events GROUP BY event_type").fetchall())
    assert counts.get("attempt_completed", 0) == 1, f"expected 1 completed, got {counts}"
    assert counts.get("attempt_failed", 0) == 0, \
        f"finalize failure wrote a contradictory attempt_failed: {counts}"


@pytest.mark.asyncio
async def test_idempotency_dedupe_is_failopen_on_finalize_error(tmp_path, monkeypatch):
    ledger_db = tmp_path / "ledger.db"
    monkeypatch.setenv("LLM_ROUTER_IDEMPOTENCY_PATH", str(tmp_path / "idem.db"))
    from llm_router import idempotency
    idempotency.reset_store_for_tests()
    prior = LLMResponse(content="cached-answer", model="ollama/x", provider="ollama",
                        input_tokens=1, output_tokens=1, cost_usd=0.0, latency_ms=1.0)
    idempotency.get_store().store("dedupe-key", prior)

    with ExitStack() as es:
        _common_patches(es, ledger_db)
        # Finalize raises on the dedupe path — the turn must still return the cached
        # response (fail-open), not propagate the error.
        es.enter_context(patch("llm_router.router._finalize_successful_route",
                               new_callable=AsyncMock, side_effect=RuntimeError("boom")))
        resp = await router.route_and_call(
            TaskType.QUERY, "hello", profile=RoutingProfile.BALANCED,
            idempotency_key="dedupe-key")
        await router.drain_bg_tasks(3.0)

    assert resp is not None and resp.content == "cached-answer", \
        "idempotency dedupe did not fail open on a finalize error"
    idempotency.reset_store_for_tests()
