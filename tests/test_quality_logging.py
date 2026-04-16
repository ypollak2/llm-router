"""Tests for quality logging — routing_decisions table and reports."""

from __future__ import annotations

import pytest

from llm_router import cost


async def _log_decision(prompt: str = "test prompt", **overrides) -> None:
    """Helper to log a routing decision with sensible defaults."""
    defaults = dict(
        prompt=prompt,
        task_type="query",
        profile="balanced",
        classifier_type="heuristic",
        classifier_model=None,
        classifier_confidence=0.9,
        classifier_latency_ms=1.0,
        complexity="simple",
        recommended_model="openai/gpt-4o-mini",
        base_model="openai/gpt-4o-mini",
        was_downshifted=False,
        budget_pct_used=0.1,
        quality_mode="balanced",
        final_model="openai/gpt-4o-mini",
        final_provider="openai",
        success=True,
        input_tokens=100,
        output_tokens=50,
        cost_usd=0.001,
        latency_ms=200.0,
    )
    defaults.update(overrides)
    await cost.log_routing_decision(**defaults)


@pytest.mark.asyncio
async def test_log_routing_decision_persists(temp_db):
    """A logged decision should be retrievable via quality report."""
    await _log_decision()
    report = await cost.get_quality_report(days=7)
    assert report["total_decisions"] == 1
    assert report["success_rate"] == 1.0


@pytest.mark.asyncio
async def test_quality_report_empty(temp_db):
    """Empty database returns zeroed report."""
    report = await cost.get_quality_report(days=7)
    assert report["total_decisions"] == 0
    assert report["by_classifier"] == {}
    assert report["by_task_type"] == {}
    assert report["total_cost_usd"] == 0.0


@pytest.mark.asyncio
async def test_quality_report_by_classifier(temp_db):
    """Report groups decisions by classifier type."""
    await _log_decision(prompt="a", classifier_type="heuristic")
    await _log_decision(prompt="b", classifier_type="heuristic")
    await _log_decision(prompt="c", classifier_type="ollama")

    report = await cost.get_quality_report(days=7)
    assert report["by_classifier"]["heuristic"] == 2
    assert report["by_classifier"]["ollama"] == 1


@pytest.mark.asyncio
async def test_quality_report_by_task_type(temp_db):
    """Report groups decisions by task type."""
    await _log_decision(prompt="a", task_type="query")
    await _log_decision(prompt="b", task_type="code")
    await _log_decision(prompt="c", task_type="code")

    report = await cost.get_quality_report(days=7)
    assert report["by_task_type"]["code"] == 2
    assert report["by_task_type"]["query"] == 1


@pytest.mark.asyncio
async def test_quality_report_by_model(temp_db):
    """Report groups decisions by final model with cost and latency."""
    await _log_decision(prompt="a", final_model="openai/gpt-4o", cost_usd=0.01, latency_ms=500)
    await _log_decision(prompt="b", final_model="openai/gpt-4o", cost_usd=0.02, latency_ms=300)
    await _log_decision(prompt="c", final_model="gemini/flash", cost_usd=0.001, latency_ms=100)

    report = await cost.get_quality_report(days=7)
    gpt = report["by_model"]["openai/gpt-4o"]
    assert gpt["count"] == 2
    assert gpt["total_cost"] == pytest.approx(0.03)
    assert gpt["avg_latency"] == pytest.approx(400.0)


@pytest.mark.asyncio
async def test_quality_report_downshift_rate(temp_db):
    """Downshift rate should reflect proportion of downshifted decisions."""
    await _log_decision(prompt="a", was_downshifted=True)
    await _log_decision(prompt="b", was_downshifted=False)

    report = await cost.get_quality_report(days=7)
    assert report["downshift_rate"] == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_quality_report_aggregates(temp_db):
    """Report correctly sums tokens, cost, and averages confidence/latency."""
    await _log_decision(
        prompt="a", input_tokens=100, output_tokens=50,
        cost_usd=0.01, latency_ms=200, classifier_confidence=0.8,
    )
    await _log_decision(
        prompt="b", input_tokens=200, output_tokens=100,
        cost_usd=0.02, latency_ms=400, classifier_confidence=1.0,
    )

    report = await cost.get_quality_report(days=7)
    assert report["total_decisions"] == 2
    assert report["total_cost_usd"] == pytest.approx(0.03)
    assert report["total_tokens"] == 450  # 100+50+200+100
    assert report["avg_confidence"] == pytest.approx(0.9)
    assert report["avg_latency_ms"] == pytest.approx(300.0)


@pytest.mark.asyncio
async def test_rate_routing_decision_latest(temp_db):
    """rate_routing_decision(None) rates the most recent decision."""
    await _log_decision()
    rated_id = await cost.rate_routing_decision(None, good=True)
    assert rated_id is not None
    assert rated_id > 0


@pytest.mark.asyncio
async def test_rate_routing_decision_specific(temp_db):
    """rate_routing_decision(id) rates a specific decision."""
    await _log_decision(prompt="first")
    await _log_decision(prompt="second")
    # Rate the first (id=1)
    rated_id = await cost.rate_routing_decision(1, good=False)
    assert rated_id == 1


@pytest.mark.asyncio
async def test_rate_routing_decision_missing(temp_db):
    """Returns None when there are no decisions to rate."""
    result = await cost.rate_routing_decision(None, good=True)
    assert result is None


@pytest.mark.asyncio
async def test_get_daily_spend_empty(temp_db):
    """Returns 0.0 when no usage data exists for today."""
    spend = await cost.get_daily_spend()
    assert spend == 0.0


@pytest.mark.asyncio
async def test_get_daily_spend_with_usage(temp_db):
    """Returns sum of today's external usage costs."""
    from llm_router.types import LLMResponse, RoutingProfile, TaskType
    for _ in range(3):
        resp = LLMResponse(
            content="x", model="openai/gpt-4o-mini",
            input_tokens=10, output_tokens=5,
            cost_usd=0.002, latency_ms=100.0, provider="openai",
        )
        await cost.log_usage(resp, TaskType.QUERY, RoutingProfile.BUDGET)
    spend = await cost.get_daily_spend()
    assert spend == pytest.approx(0.006)
