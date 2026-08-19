"""CF-1 integration: a real route_and_call() emits exactly one v2 completion row.

The unit tests in test_route_ledger_v2.py prove the ledger *logic*; this proves the
*wiring* — that driving route_and_call through the dispatch loop with a mocked provider
actually appends a v2 row with honest completion semantics (§18 CF-1: "every top-level
route emits exactly one v2 ledger row"), and that suppress_ledger=True suppresses it.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from llm_router.routing_quality import load_records
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


async def _run(prompt: str, ledger, monkeypatch, *, suppress: bool):
    monkeypatch.setenv("LLM_ROUTER_ROUTING_LEDGER", str(ledger))
    monkeypatch.setenv("LLM_ROUTER_BANDIT", "off")

    async def successful_call(model, messages, **kwargs):
        return LLMResponse(content="ok", model=model, input_tokens=10,
                           output_tokens=5, cost_usd=0.001, latency_ms=12.0,
                           provider="openai")

    tracker = MagicMock()
    tracker.is_healthy.return_value = True
    mock_log = MagicMock()
    mock_log.bind.return_value = MagicMock()

    from llm_router.router import route_and_call
    with (
        patch("llm_router.router.get_config", return_value=_Cfg()),
        patch("llm_router.router._build_and_filter_chain", new_callable=AsyncMock,
              return_value=["openai/gpt-4o"]),
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
        return await route_and_call(
            TaskType.QUERY, prompt, profile=RoutingProfile.BALANCED,
            suppress_ledger=suppress,
        )


@pytest.mark.asyncio
async def test_route_and_call_emits_one_completion_row(temp_db, tmp_path, monkeypatch):
    ledger = tmp_path / "rq.jsonl"
    resp = await _run("what is the capital of France?", ledger, monkeypatch, suppress=False)
    assert resp.content == "ok"
    rows = [r for r in load_records(str(ledger)) if not r.get("_invalid")]
    assert len(rows) == 1, f"expected exactly one ledger row, got {len(rows)}"
    r = rows[0]
    assert r["schema_version"] == 2
    assert r["route_kind"] == "completion"
    assert r["route_succeeded"] is True
    # honesty: no tools, no verification → None (never True)
    assert r["tool_execution_attempted"] is False and r["tool_execution_succeeded"] is None
    assert r["verification_attempted"] is False and r["verification_passed"] is None
    assert r["final_model"] == "openai/gpt-4o"
    assert r["final_tier"] == 2  # mid external
    # G2: a clean single-attempt route has NO failed-attempt cost; actual == final cost
    assert r["failed_attempt_cost_usd"] == 0.0
    assert abs(r["actual_cost_usd"] - 0.001) < 1e-9


@pytest.mark.asyncio
async def test_suppress_ledger_emits_no_row(temp_db, tmp_path, monkeypatch):
    ledger = tmp_path / "rq.jsonl"
    await _run("internal planner call", ledger, monkeypatch, suppress=True)
    rows = [r for r in load_records(str(ledger)) if not r.get("_invalid")]
    assert rows == [], "suppress_ledger=True must not emit a top-level row"


@pytest.mark.asyncio
async def test_failed_attempt_cost_folded_on_quality_escalation(temp_db, tmp_path, monkeypatch):
    """G2: a billable attempt rejected for low quality contributes its cost to
    failed_attempt_cost_usd, and actual_cost_usd = final cost + failed-attempt cost.
    The rejected response is NEVER double-counted as the final cost."""
    from collections import namedtuple

    ledger = tmp_path / "rq.jsonl"
    monkeypatch.setenv("LLM_ROUTER_ROUTING_LEDGER", str(ledger))
    monkeypatch.setenv("LLM_ROUTER_BANDIT", "off")
    monkeypatch.setenv("LLM_ROUTER_ESCALATE_ON_QUALITY", "1")

    _COST = {"openai/gpt-4o": 0.002, "openai/gpt-4o-mini": 0.001}

    async def call(model, messages, **kwargs):
        return LLMResponse(content="answer " * 20, model=model, input_tokens=100,
                           output_tokens=50, cost_usd=_COST[model], latency_ms=10.0,
                           provider="openai")

    _QS = namedtuple("QS", "score reasons")
    # first model scores LOW (forces one quality escalation), second scores fine
    _scores = iter([_QS(0.10, ["short"]), _QS(0.95, [])])
    tracker = MagicMock()
    tracker.is_healthy.return_value = True
    mlog = MagicMock()
    mlog.bind.return_value = MagicMock()

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
        )

    rows = [r for r in load_records(str(ledger)) if not r.get("_invalid")]
    assert len(rows) == 1
    r = rows[0]
    # escalation fired: first attempt (gpt-4o, $0.002) was rejected for low quality →
    # its cost is the failed-attempt cost; final is gpt-4o-mini ($0.001); actual = sum.
    assert r["final_model"] == "openai/gpt-4o-mini", "expected a quality escalation to the 2nd model"
    assert abs(r["failed_attempt_cost_usd"] - 0.002) < 1e-9
    assert abs(r["actual_cost_usd"] - 0.003) < 1e-9   # 0.001 final + 0.002 failed, no double count
    assert r["quality_escalation_occurred"] is True
    assert r["mis_route"] is True   # quality failure of the first-choice tier


@pytest.mark.asyncio
async def test_canonical_ledger_captures_rejected_attempt_cost(temp_db, tmp_path, monkeypatch):
    """INV-COST-001 (router boundary): the rejected/escalated attempt's cost reaches the
    USER-FACING canonical execution ledger — not only the orphaned routing_quality.jsonl.

    This is the fail-before/pass-after proof for audit P0-1: before router.py emits
    LedgerEvents, get_session_accounting sees zero events (the rejected cost was dropped
    by the winner-only cost.log_usage/session_spend path); after wiring it sees both
    attempts and reconciles actual = 0.003.
    """
    from collections import namedtuple

    monkeypatch.setenv("LLM_ROUTER_ROUTING_LEDGER", str(tmp_path / "rq.jsonl"))
    monkeypatch.setenv("LLM_ROUTER_BANDIT", "off")
    monkeypatch.setenv("LLM_ROUTER_ESCALATE_ON_QUALITY", "1")
    monkeypatch.setenv("LLM_ROUTER_EXECUTION_LEDGER_DB", str(tmp_path / "ledger.db"))
    monkeypatch.setenv("LLM_ROUTER_SESSION_ID", "wiring-sess")

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
        )

    acc = execution_ledger.get_session_accounting("wiring-sess")
    assert acc.billable_attempt_count == 2, "both the rejected and accepted attempt must be recorded"
    assert acc.rejected_attempt_count == 1
    assert acc.accepted_attempt_count == 1
    # INV-COST-001/002: the user-facing canonical total INCLUDES the rejected 0.002.
    assert acc.actual_cost_usd == pytest.approx(0.003)
    assert acc.terminal_states.get("accepted") == 1


@pytest.mark.asyncio
async def test_semantic_cache_hit_records_bypassed_terminal(temp_db, tmp_path, monkeypatch):
    """AC-6/INV-ROUTE-005: a semantic-cache hit is a terminal 'bypassed' state — it
    must be recorded (no billable attempt, but a real route outcome), not invisible."""
    monkeypatch.setenv("LLM_ROUTER_EXECUTION_LEDGER_DB", str(tmp_path / "ledger.db"))
    monkeypatch.setenv("LLM_ROUTER_SESSION_ID", "cache-sess")
    from llm_router import execution_ledger
    from llm_router.router import route_and_call

    cached = LLMResponse(content="cached", model="ollama/qwen3", input_tokens=10,
                         output_tokens=5, cost_usd=0.0, latency_ms=1.0, provider="ollama")
    tracker = MagicMock()
    tracker.is_healthy.return_value = True
    mlog = MagicMock()
    mlog.bind.return_value = MagicMock()
    with (
        patch("llm_router.router.get_config", return_value=_Cfg()),
        patch("llm_router.router._build_and_filter_chain", new_callable=AsyncMock,
              return_value=["openai/gpt-4o"]),
        patch("llm_router.router.get_tracker", return_value=tracker),
        patch("llm_router.router.log", mlog),
        patch("llm_router.router._native_notify", lambda *a, **k: None),
        patch("llm_router.router.cost.get_monthly_spend", new_callable=AsyncMock, return_value=0.0),
        patch("llm_router.router.cost.get_daily_spend", new_callable=AsyncMock, return_value=0.0),
        patch("llm_router.router.cost.get_daily_spend_by_task_type", new_callable=AsyncMock, return_value=0.0),
        patch("llm_router.router.reserve_envelope", new_callable=AsyncMock, return_value=(None, True, None)),
        patch("llm_router.router.commit_envelope", new_callable=AsyncMock),
        patch("llm_router.router.release_envelope", new_callable=AsyncMock),
        patch("llm_router.semantic_cache.check", new_callable=AsyncMock, return_value=cached),
    ):
        resp = await route_and_call(TaskType.QUERY, "capital of France?",
                                    profile=RoutingProfile.BALANCED)
    assert resp.content == "cached"
    acc = execution_ledger.get_session_accounting("cache-sess")
    assert acc.terminal_states.get("bypassed") == 1
    assert acc.billable_attempt_count == 0  # cache hit → no billable attempt
