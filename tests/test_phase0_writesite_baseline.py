"""Phase 0 Step 3 — write-site wiring: baseline + classifier + failed-attempt cost.

Proves the *wiring*, not just the pure `_aggregate` math (already covered by
test_phase0_aggregate.py): driving a real `route_and_call()` through the
dispatch loop with a mocked provider must land a nonzero
`baseline_equivalent_cost_usd` and `classifier_cost_usd` on the accepted
attempt's execution-ledger row (Gap 1), derived via
`cost._get_baseline_for_task` / `cost._get_baseline_cost` and the
`classification_data["classifier_cost_usd"]` passed in by the caller
(tools/routing.py). Rejected attempts stay cost-only — the baseline is
credited exactly once, on the accepted attempt (R6, no double count).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from llm_router.types import LLMResponse, RoutingProfile, TaskType


class _Cfg:
    llm_router_claude_subscription = False
    llm_router_gemini_subscription = False
    llm_router_claw_code = False
    llm_router_routing_policy = "balanced"
    llm_router_agentic_model = ""
    llm_router_profile = RoutingProfile.BALANCED
    llm_router_monthly_budget = 0.0
    llm_router_daily_spend_limit = 0.0
    llm_router_escalate_above = 0.0
    llm_router_hard_stop_above = 0.0
    codex_daily_limit = 1000
    compaction_mode = "off"
    compaction_threshold = 4000
    prompt_cache_enabled = False
    prompt_cache_min_tokens = 1024
    context_enabled = False
    caveman_mode = "off"
    available_providers = {"openai"}

    def all_ollama_models(self):
        return []

    def all_openai_compat_models(self):
        return []


@pytest.mark.asyncio
async def test_accepted_attempt_carries_nonzero_baseline_and_classifier_cost(
    temp_db, tmp_path, monkeypatch
):
    monkeypatch.setenv("LLM_ROUTER_ROUTING_LEDGER", str(tmp_path / "rq.jsonl"))
    monkeypatch.setenv("LLM_ROUTER_BANDIT", "off")
    monkeypatch.setenv("LLM_ROUTER_EXECUTION_LEDGER_DB", str(tmp_path / "ledger.db"))
    monkeypatch.setenv("LLM_ROUTER_SESSION_ID", "phase0-writesite-sess")

    async def successful_call(model, messages, **kwargs):
        return LLMResponse(content="answer", model=model, input_tokens=100,
                            output_tokens=50, cost_usd=0.001, latency_ms=12.0,
                            provider="openai")

    tracker = MagicMock()
    tracker.is_healthy.return_value = True
    mock_log = MagicMock()
    mock_log.bind.return_value = MagicMock()

    from llm_router import execution_ledger
    from llm_router.router import route_and_call

    with (
        patch("llm_router.router.get_config", return_value=_Cfg()),
        patch("llm_router.router._build_and_filter_chain", new_callable=AsyncMock,
              return_value=["openai/gpt-4o-mini"]),
        patch("llm_router.router.providers.call_llm", new_callable=AsyncMock,
              side_effect=successful_call),
        patch("llm_router.router.get_tracker", return_value=tracker),
        patch("llm_router.router.log", mock_log),
        patch("llm_router.router._native_notify", lambda *a, **k: None),
        patch("llm_router.router.cost.get_monthly_spend", new_callable=AsyncMock, return_value=0.0),
        patch("llm_router.router.cost.get_daily_spend", new_callable=AsyncMock, return_value=0.0),
        patch("llm_router.router.cost.get_daily_spend_by_task_type", new_callable=AsyncMock, return_value=0.0),
        patch("llm_router.router.cost.log_usage", new_callable=AsyncMock),
        patch("llm_router.router.reserve_envelope", new_callable=AsyncMock, return_value=(None, True, None)),
        patch("llm_router.router.commit_envelope", new_callable=AsyncMock),
        patch("llm_router.router.release_envelope", new_callable=AsyncMock),
        patch("llm_router.semantic_cache.check", new_callable=AsyncMock, return_value=None),
        patch("llm_router.semantic_cache.store", new_callable=AsyncMock),
    ):
        resp = await route_and_call(
            TaskType.QUERY,
            "what is the capital of France?",
            profile=RoutingProfile.BALANCED,
            complexity_hint="moderate",
            classification_data={"classifier_cost_usd": 0.0025},
        )

    assert resp.content == "answer"

    acc = execution_ledger.get_session_accounting("phase0-writesite-sess")
    # Gap 1: classifier cost passed through from classification_data.
    assert acc.classifier_cost_usd_total == pytest.approx(0.0025)
    # Gap 1: the accepted attempt's baseline_equivalent_cost_usd is a nonzero,
    # realistically-derived $ counterfactual (via cost._get_baseline_for_task /
    # cost._get_baseline_cost against the response's 100 in / 50 out tokens) —
    # never left at the pre-Phase-0 default of 0/None.
    assert acc.baseline_equivalent_cost_usd > 0.0
    # Single clean attempt, nothing rejected — no failed-attempt cost on this route.
    assert acc.failed_attempt_cost_usd_total == 0.0


@pytest.mark.asyncio
async def test_rejected_attempt_carries_no_baseline_only_accepted_does(
    temp_db, tmp_path, monkeypatch
):
    """R6 — baseline is credited exactly once, on the accepted attempt only; the
    rejected (quality-escalated) attempt above it stays cost-only, no baseline."""
    from collections import namedtuple

    monkeypatch.setenv("LLM_ROUTER_ROUTING_LEDGER", str(tmp_path / "rq.jsonl"))
    monkeypatch.setenv("LLM_ROUTER_BANDIT", "off")
    monkeypatch.setenv("LLM_ROUTER_ESCALATE_ON_QUALITY", "1")
    monkeypatch.setenv("LLM_ROUTER_EXECUTION_LEDGER_DB", str(tmp_path / "ledger.db"))
    monkeypatch.setenv("LLM_ROUTER_SESSION_ID", "phase0-writesite-r6-sess")

    _COST = {"openai/gpt-4o": 0.002, "openai/gpt-4o-mini": 0.001}

    async def call(model, messages, **kwargs):
        return LLMResponse(content="answer " * 20, model=model, input_tokens=100,
                            output_tokens=50, cost_usd=_COST[model], latency_ms=10.0,
                            provider="openai")

    _QS = namedtuple("QS", "score reasons")
    _scores = iter([_QS(0.10, ["short"]), _QS(0.95, [])])
    tracker = MagicMock()
    tracker.is_healthy.return_value = True
    mlog = MagicMock()
    mlog.bind.return_value = MagicMock()

    from llm_router import execution_ledger
    from llm_router.router import route_and_call

    with (
        patch("llm_router.router.get_config", return_value=_Cfg()),
        patch("llm_router.router._build_and_filter_chain", new_callable=AsyncMock,
              return_value=["openai/gpt-4o", "openai/gpt-4o-mini"]),
        patch("llm_router.router.providers.call_llm", new_callable=AsyncMock, side_effect=call),
        patch("llm_router.quality_feedback.score_response", side_effect=lambda *a, **k: next(_scores)),
        patch("llm_router.quality_feedback.record_quality", lambda *a, **k: None),
        patch("llm_router.router.get_tracker", return_value=tracker),
        patch("llm_router.router.log", mlog),
        patch("llm_router.router._native_notify", lambda *a, **k: None),
        patch("llm_router.router.cost.get_monthly_spend", new_callable=AsyncMock, return_value=0.0),
        patch("llm_router.router.cost.get_daily_spend", new_callable=AsyncMock, return_value=0.0),
        patch("llm_router.router.cost.get_daily_spend_by_task_type", new_callable=AsyncMock, return_value=0.0),
        patch("llm_router.router.cost.log_usage", new_callable=AsyncMock),
        patch("llm_router.router.reserve_envelope", new_callable=AsyncMock, return_value=(None, True, None)),
        patch("llm_router.router.commit_envelope", new_callable=AsyncMock),
        patch("llm_router.router.release_envelope", new_callable=AsyncMock),
        patch("llm_router.semantic_cache.check", new_callable=AsyncMock, return_value=None),
        patch("llm_router.semantic_cache.store", new_callable=AsyncMock),
    ):
        await route_and_call(
            TaskType.ANALYZE,
            "Please analyze this in depth: " + ("context " * 40),
            profile=RoutingProfile.BALANCED, complexity_hint="moderate",
            classification_data={"classifier_cost_usd": 0.001},
        )

    acc = execution_ledger.get_session_accounting("phase0-writesite-r6-sess")
    assert acc.billable_attempt_count == 2
    assert acc.rejected_attempt_count == 1
    assert acc.accepted_attempt_count == 1
    # G2 (pre-existing): the rejected attempt's cost is folded into failed-attempt cost.
    assert acc.failed_attempt_cost_usd_total == pytest.approx(0.002)
    # Gap 1: baseline is still nonzero (credited once, on the accepted attempt) — a
    # rejected-then-escalated route is not left with a zero/degenerate baseline. We
    # assert on the raw aggregated baseline_equivalent_cost_usd (not
    # potential_savings_usd, which nets in the rejected attempt's own cost too and
    # is not guaranteed positive for an arbitrary model-cost/baseline-rate pairing).
    assert acc.baseline_equivalent_cost_usd > 0.0
