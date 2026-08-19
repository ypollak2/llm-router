"""`get_quality_report` must not present unattributed rows as routing behaviour.

THE DEFECT THIS PINS
--------------------
`routing_decisions` mixes two populations that mean different things:

* rows where the classifier ran — the router chose a model, and the row is evidence
  about routing;
* rows where `classifier_type='unknown'` — the classifier never ran, so the row says
  nothing whatsoever about routing.

Aggregating them together made the report state the opposite of the truth. On the real
database, 28,536 rows (69.4% of the table, every one `classifier_type='unknown'`, all
written by an unisolated test suite) named `openai/gpt-4o-mini`. The dashboard therefore
showed gpt-4o-mini as the dominant destination — for a model the router *never chose* —
while the genuine top destination, `ollama/hermes3:8b` at 38.6% of attributed decisions,
appeared as 11.7%.

Understating local routing threefold, in the surface users read to decide whether the
product does what it claims, is the same defect class as every other finding in this
audit: a number that looks right and is wrong.

WHY THE UNATTRIBUTED ROWS ARE REPORTED RATHER THAN DROPPED
----------------------------------------------------------
Filtering them silently would produce a tidy, correct-looking table and destroy the only
visible sign that something is writing rows nobody can account for. That invisibility is
precisely why this survived. They are surfaced in their own bucket, with the reason
attached.
"""

from __future__ import annotations

import pytest

from llm_router import cost


@pytest.mark.asyncio
async def test_by_model_excludes_rows_where_the_classifier_never_ran(temp_db):
    """The load-bearing assertion: a model that only ever appears on unattributed rows
    must not show up as a routing destination."""
    await cost.log_routing_decision(
        prompt="alpha", task_type="code", profile="balanced",
        classifier_type="heuristic", classifier_model="h", classifier_confidence=0.9,
        classifier_latency_ms=1.0, complexity="simple", recommended_model="hermes3:8b",
        base_model="hermes3:8b", was_downshifted=False, budget_pct_used=0.0,
        quality_mode="balanced", final_model="hermes3:8b", final_provider="ollama",
        success=True, input_tokens=10, output_tokens=20, cost_usd=0.0, latency_ms=5.0,
    )
    await cost.log_routing_decision(
        prompt="beta", task_type="code", profile="balanced",
        classifier_type="unknown", classifier_model="", classifier_confidence=0.0,
        classifier_latency_ms=0.0, complexity="simple", recommended_model="",
        base_model="", was_downshifted=False, budget_pct_used=0.0,
        quality_mode="balanced", final_model="openai/gpt-4o-mini", final_provider="openai",
        success=True, input_tokens=62, output_tokens=164, cost_usd=0.000108, latency_ms=9.0,
    )

    report = await cost.get_quality_report(days=7)

    assert "hermes3:8b" in report["by_model"], "an attributed decision vanished"
    assert "openai/gpt-4o-mini" not in report["by_model"], (
        "a model the classifier never chose is being reported as a routing destination — "
        "this is the defect that made the dashboard show 69% gpt-4o-mini"
    )


@pytest.mark.asyncio
async def test_unattributed_rows_are_surfaced_not_hidden(temp_db):
    """Dropping them would be a different bug wearing the same fix."""
    await cost.log_routing_decision(
        prompt="gamma", task_type="code", profile="balanced",
        classifier_type="unknown", classifier_model="", classifier_confidence=0.0,
        classifier_latency_ms=0.0, complexity="simple", recommended_model="",
        base_model="", was_downshifted=False, budget_pct_used=0.0,
        quality_mode="balanced", final_model="openai/gpt-4o-mini", final_provider="openai",
        success=True, input_tokens=62, output_tokens=164, cost_usd=0.000108, latency_ms=9.0,
    )

    report = await cost.get_quality_report(days=7)

    assert report["unattributed_decisions"] == 1
    assert "openai/gpt-4o-mini" in report["unattributed_by_model"]
    assert report["unattributed_reason"], "the bucket must say WHY these rows are excluded"


@pytest.mark.asyncio
async def test_totals_still_account_for_every_row(temp_db):
    """attributed + unattributed must equal the total. A split that loses rows is worse
    than the mixing it replaced, because the loss is silent."""
    for i, ctype in enumerate(["heuristic", "gateway", "unknown", "unknown"]):
        await cost.log_routing_decision(
            prompt=f"prompt-{i}", task_type="code", profile="balanced",
            classifier_type=ctype, classifier_model="m", classifier_confidence=0.5,
            classifier_latency_ms=1.0, complexity="simple", recommended_model="x",
            base_model="x", was_downshifted=False, budget_pct_used=0.0,
            quality_mode="balanced", final_model="hermes3:8b", final_provider="ollama",
            success=True, input_tokens=10, output_tokens=10, cost_usd=0.0, latency_ms=1.0,
        )

    report = await cost.get_quality_report(days=7)

    assert (
        report["attributed_decisions"] + report["unattributed_decisions"]
        == report["total_decisions"]
    ), "the split drops rows"
    assert report["unattributed_decisions"] == 2
