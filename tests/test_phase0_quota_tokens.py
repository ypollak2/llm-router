"""Phase 0 Step 5 — write-site wiring: baseline_tokens (Gap 2, quota derivation).

Proves the *wiring* of the accepted-attempt write site: driving a real
`route_and_call()` through the dispatch loop with a mocked provider must land
a correct, non-fabricated `baseline_tokens` value (the actual_proxy — the
accepted attempt's own input+output token count) on the ledger row, for both
subscription and metered host modes, and that metered mode never lets a
token-based figure leak into the $ accounting.

# TODO(phase-0): a single router.py-driven route cannot honestly demonstrate
# `realized_quota_tokens_saved > 0` end-to-end. Per the brief's own Gap 2
# definition, `baseline_tokens` is an *actual_proxy* — literally the accepted
# attempt's own (input_tokens + output_tokens) — so on the SAME row it is by
# construction equal to the tokens `_aggregate()` also folds into
# `route_actual_tokens[rid]`. Since `route_actual_tokens` sums tokens across
# EVERY billable row on the route (accepted AND any prior rejects) while
# `route_baseline_tokens` only ever receives the accepted attempt's own value,
# route_actual_tokens >= route_baseline_tokens always holds for any real
# router.py-driven route, so `quota = max(0, baseline - actual)` is
# structurally 0 in this test's scope. This is NOT a bug in `_aggregate` —
# `_aggregate`'s quota math is proven correct for arbitrary divergent inputs
# by test_phase0_aggregate.py::test_quota_tokens_saved_only_on_realized_routes_
# bucketed_by_host_mode (Step 2), which hand-builds a baseline_tokens value
# distinct from actual_tokens on synthetic LedgerEvent rows. A genuine
# baseline > actual divergence — and therefore a realistically demonstrated
# quota > 0 — only shows up across a heterogeneous corpus of MANY different
# routes/task-types/models (Step 7's soak replay), never on one deterministic
# router-driven call. This test therefore asserts the write-site's honesty
# (baseline_tokens is present, correct, and host-mode-appropriate) rather than
# a quota figure that would require deviating from the actual_proxy
# definition to fabricate.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
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


def _accepted_row(db: Path, session_id: str) -> sqlite3.Row:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        rows = list(conn.execute(
            "SELECT input_tokens, output_tokens, baseline_tokens, host_mode, "
            "measured_cost_usd FROM execution_events "
            "WHERE session_id = ? AND event_type = 'attempt_completed' AND accepted = 1",
            (session_id,),
        ))
    finally:
        conn.close()
    assert rows, f"no accepted attempt_completed row found for session {session_id}"
    return rows[0]


async def _drive_route_and_call(*, session_id: str, tmp_path: Path, monkeypatch,
                                 subscription: bool) -> None:
    monkeypatch.setenv("LLM_ROUTER_ROUTING_LEDGER", str(tmp_path / f"rq-{session_id}.jsonl"))
    monkeypatch.setenv("LLM_ROUTER_BANDIT", "off")
    monkeypatch.setenv("LLM_ROUTER_EXECUTION_LEDGER_DB", str(tmp_path / "ledger.db"))
    monkeypatch.setenv("LLM_ROUTER_SESSION_ID", session_id)
    if subscription:
        monkeypatch.delenv("LLM_ROUTER_CLAUDE_SUBSCRIPTION", raising=False)
    else:
        monkeypatch.setenv("LLM_ROUTER_CLAUDE_SUBSCRIPTION", "0")  # explicitly metered

    async def successful_call(model, messages, **kwargs):
        return LLMResponse(content="answer", model=model, input_tokens=100,
                            output_tokens=50, cost_usd=0.001, latency_ms=12.0,
                            provider="openai")

    tracker = MagicMock()
    tracker.is_healthy.return_value = True
    mock_log = MagicMock()
    mock_log.bind.return_value = MagicMock()

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


@pytest.mark.asyncio
async def test_subscription_accepted_attempt_writes_actual_proxy_baseline_tokens(
    temp_db, tmp_path, monkeypatch
):
    """Subscription host mode: baseline_tokens must be written on the accepted
    attempt, equal to the honest actual_proxy (its own input+output tokens) —
    never left NULL/0, never a fabricated larger figure."""
    sid = "phase0-quota-sub-sess"
    await _drive_route_and_call(session_id=sid, tmp_path=tmp_path,
                                 monkeypatch=monkeypatch, subscription=True)

    row = _accepted_row(tmp_path / "ledger.db", sid)
    assert row["host_mode"] == "subscription"
    assert row["input_tokens"] == 100
    assert row["output_tokens"] == 50
    # Gap 2: actual_proxy — the accepted attempt's own token count, not a
    # fabricated/precise-tokenized larger baseline.
    assert row["baseline_tokens"] == 150


@pytest.mark.asyncio
async def test_metered_accepted_attempt_writes_baseline_tokens_without_dollar_leak(
    temp_db, tmp_path, monkeypatch
):
    """Metered host mode: baseline_tokens is still written honestly (tokens are
    tokens regardless of host_mode), but the $ actually charged must remain the
    real measured_cost_usd — never blended with or derived from the token
    quota figure."""
    sid = "phase0-quota-metered-sess"
    await _drive_route_and_call(session_id=sid, tmp_path=tmp_path,
                                 monkeypatch=monkeypatch, subscription=False)

    row = _accepted_row(tmp_path / "ledger.db", sid)
    assert row["host_mode"] == "metered"
    assert row["baseline_tokens"] == 150
    # The real dollars charged came from the mocked response's cost_usd, not
    # some function of baseline_tokens — no $-from-tokens fabrication.
    assert row["measured_cost_usd"] == pytest.approx(0.001)
