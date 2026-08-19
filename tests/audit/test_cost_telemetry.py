"""Audit — Section 6: Cost & telemetry accuracy.

Covers three areas:

1. ``cost.log_usage`` (called from ``router._dispatch_model_loop`` at the point
   a candidate model actually succeeds) must log the provider/model/cost of
   the model that ACTUALLY executed, not an earlier candidate that was
   attempted and failed in the same chain walk.
2. The dashboard token-axis formatter (``llm_router.ui.session_summary``) must
   never mix unit suffixes (M / k / raw) on a single Y-axis, regardless of
   how wide the value range is.
3. Session/lifetime cost totals surfaced to the user (the ``llm_savings``
   MCP tool, backed by ``llm_router.commands.gain.SavingsAnalytics``) must
   reflect exactly the models that were actually recorded — no more, no
   less. This test surfaces a real schema-mismatch bug: see the
   ``test_savings_analytics_*`` tests and REPORT_B.md.

Uses the ``temp_db`` / ``mock_env`` fixtures from tests/conftest.py so no
production ``~/.llm-router`` state is touched. Per the known HOME-isolation gap
documented in conftest.py, any assertion that depends on a clean filesystem
explicitly patches ``pathlib.Path.home`` rather than relying on ambient
state.
"""
from __future__ import annotations

import pathlib

import pytest

from llm_router import cost
from llm_router.router import route_and_call
from llm_router.types import LLMResponse, RoutingProfile, TaskType


# ── 1. Logged usage matches the model that ACTUALLY executed ────────────────


@pytest.mark.asyncio
async def test_log_usage_reflects_actually_executed_model_not_failed_candidate(
    temp_db, mock_env, monkeypatch, tmp_path
):
    """First candidate raises, second candidate succeeds — the logged
    provider/model/cost in cost.log_usage must be the SECOND (successful)
    model's, never the first (failed) one's.
    """
    # Per the documented HOME-isolation gap: this dev box has a real
    # ~/.llm-router/routing.yaml (user-level model pin) and a real
    # ~/.llm-router/.env (sets OLLAMA_BASE_URL) that mock_env does not
    # isolate from. llm_router.repo_config.effective_config() and
    # llm_router.config's dotenv loader both resolve Path.home() at CALL time
    # (not import time, unlike the audit/session-spend/receipt-store
    # modules), so patching Path.home() here reliably keeps this test's
    # chain-building free of the operator's real local config.
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OLLAMA_BASE_URL", "")
    monkeypatch.setenv("OLLAMA_BUDGET_MODELS", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    # Belt-and-suspenders on top of the env override above: this dev box has
    # a live Ollama daemon + discovery cache, so all_ollama_models() can
    # still resolve local models via probe_ollama() regardless of the env
    # vars. Force it to report no local models so the test's two-model
    # chain is actually what gets dispatched.
    monkeypatch.setattr("llm_router.config.RouterConfig.all_ollama_models", lambda self: [])
    monkeypatch.setattr("llm_router.dynamic_routing.get_dynamic_model_chain", lambda *a, **k: None)
    monkeypatch.setattr("llm_router.router.is_codex_available", lambda: False)
    monkeypatch.setattr("llm_router.router.is_gemini_cli_available", lambda: False)
    import llm_router.config as config_module
    config_module._config = None

    from llm_router.health import get_tracker
    tracker = get_tracker()
    monkeypatch.setattr("llm_router.router.get_tracker", lambda: tracker)

    failing_response_model = "openai/gpt-4o-mini"
    succeeding_response = LLMResponse(
        content="the real answer",
        model="anthropic/claude-haiku-4-5-20251001",
        input_tokens=12,
        output_tokens=7,
        cost_usd=0.0021,
        latency_ms=42.0,
        provider="anthropic",
    )

    call_count = {"n": 0}

    async def _fake_call_llm(model, *args, **kwargs):
        call_count["n"] += 1
        if model == failing_response_model:
            raise RuntimeError("simulated transient provider failure")
        return succeeding_response

    logged_calls = []
    real_log_usage = cost.log_usage

    async def _spy_log_usage(response, task_type, profile, **kwargs):
        logged_calls.append(response)
        return await real_log_usage(response, task_type, profile, **kwargs)

    monkeypatch.setattr("llm_router.providers.call_llm", _fake_call_llm)
    monkeypatch.setattr("llm_router.router.cost.log_usage", _spy_log_usage)
    # Force a two-model chain: the failing one first, the succeeding one second.
    monkeypatch.setattr(
        "llm_router.router.get_model_chain",
        lambda *a, **k: [failing_response_model, succeeding_response.model],
    )

    resp = await route_and_call(TaskType.QUERY, "hello", profile=RoutingProfile.BUDGET)

    assert call_count["n"] == 2, "expected exactly one failed + one successful attempt"
    assert resp.model == succeeding_response.model
    assert len(logged_calls) == 1, "log_usage must be called exactly once per successful turn"
    logged = logged_calls[0]
    assert logged.model == succeeding_response.model
    assert logged.provider == succeeding_response.provider
    assert logged.cost_usd == succeeding_response.cost_usd
    assert logged.model != failing_response_model


@pytest.mark.asyncio
async def test_usage_db_row_matches_successful_model_after_chain_fallback(
    temp_db, mock_env, monkeypatch, tmp_path
):
    """End-to-end: after a fallback chain walk, the row actually persisted to
    the usage table names the successful model, not the failed one."""
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OLLAMA_BASE_URL", "")
    monkeypatch.setenv("OLLAMA_BUDGET_MODELS", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr("llm_router.config.RouterConfig.all_ollama_models", lambda self: [])
    monkeypatch.setattr("llm_router.dynamic_routing.get_dynamic_model_chain", lambda *a, **k: None)
    monkeypatch.setattr("llm_router.router.is_codex_available", lambda: False)
    monkeypatch.setattr("llm_router.router.is_gemini_cli_available", lambda: False)
    import llm_router.config as config_module
    config_module._config = None

    from llm_router.health import get_tracker
    tracker = get_tracker()
    monkeypatch.setattr("llm_router.router.get_tracker", lambda: tracker)

    failing_model = "openai/gpt-4o-mini"
    succeeding_model = "anthropic/claude-haiku-4-5-20251001"
    succeeding_response = LLMResponse(
        content="ok",
        model=succeeding_model,
        input_tokens=10,
        output_tokens=6,
        cost_usd=0.0015,
        latency_ms=30.0,
        provider="anthropic",
    )

    async def _fake_call_llm(model, *args, **kwargs):
        if model == failing_model:
            raise RuntimeError("simulated failure")
        return succeeding_response

    monkeypatch.setattr("llm_router.providers.call_llm", _fake_call_llm)
    monkeypatch.setattr(
        "llm_router.router.get_model_chain",
        lambda *a, **k: [failing_model, succeeding_model],
    )

    await route_and_call(TaskType.QUERY, "hello world", profile=RoutingProfile.BUDGET)

    import aiosqlite
    conn = await aiosqlite.connect(str(temp_db))
    try:
        cursor = await conn.execute(
            "SELECT model, provider, cost_usd FROM usage ORDER BY id DESC LIMIT 1"
        )
        row = await cursor.fetchone()
    finally:
        await conn.close()

    assert row is not None
    model, provider, cost_usd = row
    assert model == succeeding_model
    assert provider == "anthropic"
    assert model != failing_model


# ── 2. Dashboard token Y-axis never mixes units ──────────────────────────────


from llm_router.ui.session_summary import _fmt_tok_axis, _tok_axis_unit  # noqa: E402


def test_tok_axis_unit_locks_whole_axis_to_one_scale():
    """_tok_axis_unit must pick exactly one (divisor, suffix) pair per axis,
    driven by the max tick value — never a per-tick decision."""
    divisor, suffix = _tok_axis_unit(3_200_000)
    assert (divisor, suffix) == (1_000_000.0, "M")

    divisor, suffix = _tok_axis_unit(900)
    assert (divisor, suffix) == (1.0, "")

    divisor, suffix = _tok_axis_unit(450_700)
    assert (divisor, suffix) == (1_000.0, "k")


def test_fmt_tok_axis_all_ticks_share_one_suffix_across_wide_range():
    """Regression for the bug fixed today: rendering a Y-axis whose ticks span
    under 1,000 up through several million must never mix "M" and "k" (or any
    other combination of suffixes) within a single axis. Every tick is
    formatted against the ONE unit implied by the axis max, per
    _tok_axis_unit — not its own individual magnitude."""
    # A realistic set of Y-axis tick values a "tokens saved/day" chart might
    # generate: from near-zero up to multiple millions.
    max_tick = 3_200_000
    ticks = [0, 450, 900_734, 1_500_000, 3_200_000]

    divisor, suffix = _tok_axis_unit(max_tick)
    labels = [_fmt_tok_axis(t, divisor, suffix) for t in ticks]

    # Every non-zero label must end in the SAME suffix (possibly empty).
    non_zero_labels = [lbl for lbl, t in zip(labels, ticks) if t != 0]
    suffixes_used = {
        lbl[-1] if lbl and lbl[-1].isalpha() else ""
        for lbl in non_zero_labels
    }
    assert suffixes_used == {suffix}, (
        f"Y-axis mixed unit suffixes: {labels!r} (expected all '{suffix}')"
    )
    # Specifically assert the mixed-unit bug (e.g. "3.2M" next to "901.3k")
    # cannot occur: no label contains "k" while another contains "M".
    has_m = any("M" in lbl for lbl in labels)
    has_k = any("k" in lbl for lbl in labels)
    assert not (has_m and has_k), f"Mixed M/k units on one axis: {labels!r}"


def test_fmt_tok_axis_small_range_never_introduces_k_or_m():
    """When every tick is under 1,000 the axis must render raw digits only —
    no stray 'k'/'M' from a per-tick (rather than axis-wide) unit decision."""
    max_tick = 950
    ticks = [0, 100, 500, 950]
    divisor, suffix = _tok_axis_unit(max_tick)
    labels = [_fmt_tok_axis(t, divisor, suffix) for t in ticks]
    assert labels == ["0", "100", "500", "950"]


# ── 3. Session/lifetime totals reflect exactly the recorded models ──────────


@pytest.mark.asyncio
async def test_quality_report_by_model_excludes_never_recorded_models(temp_db, mock_env):
    """cost.get_quality_report's by_model breakdown must contain exactly the
    models that were logged via log_routing_decision — no phantom model Z."""
    await cost.log_routing_decision(
        prompt="prompt one",
        task_type="query",
        profile="balanced",
        classifier_type="heuristic",
        classifier_model=None,
        classifier_confidence=0.9,
        classifier_latency_ms=1.0,
        complexity="simple",
        recommended_model="anthropic/claude-haiku-4-5-20251001",
        base_model="anthropic/claude-haiku-4-5-20251001",
        was_downshifted=False,
        budget_pct_used=0.1,
        quality_mode="balanced",
        final_model="anthropic/claude-haiku-4-5-20251001",
        final_provider="anthropic",
        success=True,
        input_tokens=10,
        output_tokens=5,
        cost_usd=0.002,
        latency_ms=50.0,
    )
    await cost.log_routing_decision(
        prompt="prompt two",
        task_type="code",
        profile="balanced",
        classifier_type="heuristic",
        classifier_model=None,
        classifier_confidence=0.9,
        classifier_latency_ms=1.0,
        complexity="moderate",
        recommended_model="gemini/gemini-2.5-flash",
        base_model="gemini/gemini-2.5-flash",
        was_downshifted=False,
        budget_pct_used=0.1,
        quality_mode="balanced",
        final_model="gemini/gemini-2.5-flash",
        final_provider="gemini",
        success=True,
        input_tokens=20,
        output_tokens=8,
        cost_usd=0.004,
        latency_ms=80.0,
    )

    report = await cost.get_quality_report(days=7)

    assert report["total_decisions"] == 2
    recorded_models = set(report["by_model"].keys())
    assert recorded_models == {
        "anthropic/claude-haiku-4-5-20251001",
        "gemini/gemini-2.5-flash",
    }
    # A model that was never logged must not appear, and must not
    # contribute any cost to the total.
    assert "openai/gpt-4o" not in recorded_models
    expected_total_cost = 0.002 + 0.004
    assert report["total_cost_usd"] == pytest.approx(expected_total_cost)


@pytest.mark.asyncio
async def test_usage_summary_total_cost_matches_only_recorded_rows(temp_db, mock_env):
    """cost.get_usage_summary's lifetime total must equal the sum of the
    ACTUAL rows in the usage table — not include any unrecorded provider."""
    resp_a = LLMResponse(
        content="a", model="anthropic/claude-haiku-4-5-20251001", input_tokens=10,
        output_tokens=5, cost_usd=0.003, latency_ms=10.0, provider="anthropic",
    )
    resp_b = LLMResponse(
        content="b", model="gemini/gemini-2.5-flash", input_tokens=10,
        output_tokens=5, cost_usd=0.007, latency_ms=10.0, provider="gemini",
    )
    await cost.log_usage(resp_a, TaskType.QUERY, RoutingProfile.BUDGET)
    await cost.log_usage(resp_b, TaskType.QUERY, RoutingProfile.BALANCED)

    summary = await cost.get_usage_summary(period="all")
    assert "$0.0100" in summary  # 0.003 + 0.007
    assert "openai" not in summary.lower()
    assert "gpt-4o" not in summary.lower()


@pytest.mark.asyncio
async def test_savings_analytics_reports_real_decisions_not_zero(temp_db, mock_env):
    """DOCUMENTS A REAL BUG (see REPORT_B.md "bugs found").

    ``llm_router.commands.gain.SavingsAnalytics.get_routing_decisions`` (the
    engine behind the ``llm_savings`` MCP tool) selects columns
    ``original_tool``, ``selected_model``, ``estimated_cost_usd`` from the
    ``routing_decisions`` table. Those columns do not exist in the real
    schema (``llm_router.cost.CREATE_ROUTING_DECISIONS_TABLE`` defines
    ``final_model`` / ``final_provider`` / ``cost_usd`` — there is no
    ``original_tool``, ``selected_model``, or ``estimated_cost_usd`` column
    anywhere in that table). The query therefore always raises
    ``sqlite3.OperationalError: no such column`` — silently caught by a
    bare ``except sqlite3.Error: return []`` — so ``llm_savings`` reports
    ZERO decisions and zero savings unconditionally, no matter how much
    real routing has happened.

    This test encodes the CORRECT expected behavior (savings computed from
    real recorded decisions) and is expected to FAIL against the current
    code — that failure is the point: it's a ready-made regression test
    for whoever fixes the column mismatch."""
    from llm_router.commands.gain import SavingsAnalytics
    from llm_router.config import get_config

    await cost.log_routing_decision(
        prompt="hi",
        task_type="query",
        profile="balanced",
        classifier_type="heuristic",
        classifier_model=None,
        classifier_confidence=0.9,
        classifier_latency_ms=1.0,
        complexity="simple",
        recommended_model="anthropic/claude-haiku-4-5-20251001",
        base_model="anthropic/claude-haiku-4-5-20251001",
        was_downshifted=False,
        budget_pct_used=0.1,
        quality_mode="balanced",
        final_model="anthropic/claude-haiku-4-5-20251001",
        final_provider="anthropic",
        success=True,
        input_tokens=10,
        output_tokens=5,
        cost_usd=0.002,
        latency_ms=50.0,
    )

    config = get_config()
    analytics = SavingsAnalytics(db_path=pathlib.Path(config.llm_router_db_path))
    savings = analytics.compute_savings(days=30)

    # This is the CORRECT expectation: one real decision was recorded, so
    # the dashboard should show total_decisions == 1, not 0.
    assert savings["total_decisions"] == 1, (
        "SavingsAnalytics reported 0 decisions despite one real row in "
        "routing_decisions — see the column-name mismatch documented in "
        "llm_router/commands/gain.py get_routing_decisions() vs. the actual "
        "schema in llm_router/cost.py CREATE_ROUTING_DECISIONS_TABLE."
    )
