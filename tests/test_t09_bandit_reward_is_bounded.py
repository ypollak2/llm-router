"""The bandit's reward must trade quality against cost — T-09.

`expected_value` was `success_rate / max(avg_cost, 1e-9)`, and the 1e-9 floor
was documented as a feature ("free providers get a very large but finite
expected_value"). Very large is the defect:

    free, success 0.50, cost $0     -> 0.50 / 1e-9 = 5.0e8
    paid, success 0.99, cost $0.01  -> 0.99 / 0.01 =    99

A ratio makes price lexicographically dominant: no quality difference that can
exist closes a 1e7 gap, so the bandit was ranking by "is it free" with
success_rate as an unreachable tiebreaker — and it runs AFTER the
complexity-aware ordering that deliberately puts Ollama last for deep reasoning.

The replacement is a difference with units: `success * ANSWER_VALUE_USD - cost`,
the expected net dollars one call gains.
"""

from __future__ import annotations

import pytest

from llm_router.telemetry import ANSWER_VALUE_USD, MIN_JUDGED_FOR_SIGNAL, ModelStats


def stats(name, success, cost, *, n=50, judged=0, judge_mean=None):
    return ModelStats(model=name, n_samples=n, success_rate=success,
                      avg_cost=cost, avg_latency_ms=100.0,
                      judged_samples=judged, judge_mean=judge_mean)


FREE_MEDIOCRE = stats("ollama/qwen", 0.50, 0.0)
PAID_EXCELLENT = stats("anthropic/opus", 0.99, 0.02)
PAID_OVERPRICED = stats("anthropic/opus-pricey", 0.99, 0.10)


# ── the finding ──────────────────────────────────────────────────────────────

def test_a_better_paid_model_can_outrank_a_worse_free_one():
    """The gate. Under the old ratio this was impossible at any quality gap."""
    assert PAID_EXCELLENT.expected_value > FREE_MEDIOCRE.expected_value, (
        f"paid at {PAID_EXCELLENT.success_rate} "
        f"({PAID_EXCELLENT.expected_value:.4f}) still loses to free at "
        f"{FREE_MEDIOCRE.success_rate} ({FREE_MEDIOCRE.expected_value:.4f})"
    )


def test_free_still_wins_when_the_paid_model_is_not_worth_it():
    """Anti-vacuity: the fix must not simply invert the old bias.

    A reward that always prefers paid is as wrong as one that always prefers
    free, and it costs real money.
    """
    assert PAID_OVERPRICED.expected_value < FREE_MEDIOCRE.expected_value


def test_free_wins_at_equal_quality():
    """The behaviour the old formula was reaching for, preserved."""
    free = stats("ollama/qwen", 0.90, 0.0)
    paid = stats("anthropic/opus", 0.90, 0.02)
    assert free.expected_value > paid.expected_value


def test_the_reward_is_bounded_and_has_units():
    """No candidate can score 1e8. The number is dollars."""
    free_perfect = stats("free", 1.0, 0.0)
    assert free_perfect.expected_value == pytest.approx(ANSWER_VALUE_USD)
    assert abs(free_perfect.expected_value) < 1.0


def test_the_old_ratio_is_still_available_but_is_not_the_target():
    """Kept for reporting, and kept visibly separate from the ranking."""
    assert FREE_MEDIOCRE.success_per_dollar > 1e8
    assert FREE_MEDIOCRE.expected_value < 1.0
    ranked_old = sorted([FREE_MEDIOCRE, PAID_EXCELLENT], key=lambda s: -s.success_per_dollar)
    ranked_new = sorted([FREE_MEDIOCRE, PAID_EXCELLENT], key=lambda s: -s.expected_value)
    assert ranked_old[0].model != ranked_new[0].model, (
        "the two formulas agree on this pair — the fixture no longer "
        "demonstrates the finding"
    )


def test_the_bandit_ranks_on_expected_value():
    """Rule B: the call site, not the property in isolation."""
    import inspect
    from llm_router import bandit

    src = inspect.getsource(bandit)
    assert "s.expected_value" in src
    assert "success_per_dollar" not in src, (
        "the bandit is ranking on the old unbounded ratio"
    )


# ── the quality signal's provenance ──────────────────────────────────────────

def test_an_ungraded_model_reports_the_weak_signal_as_weak():
    """`success` means non-empty and not a deferral. It must say so.

    Measured on the live ledger 2026-09-22: judge_score was populated on
    0 of 1,598 rows. A reward that calls this "quality" is over-claiming.
    """
    value, source = FREE_MEDIOCRE.quality_signal
    assert source == "usable"
    assert value == FREE_MEDIOCRE.success_rate


def test_a_graded_model_uses_the_judge_and_says_so():
    graded = stats("m", 0.99, 0.01, judged=MIN_JUDGED_FOR_SIGNAL, judge_mean=0.40)
    value, source = graded.quality_signal
    assert source == "judge"
    assert value == pytest.approx(0.40)
    assert graded.expected_value == pytest.approx(0.40 * ANSWER_VALUE_USD - 0.01), (
        "the reward ignored the judge score in favour of the weak signal"
    )


def test_too_few_graded_samples_do_not_outrank_the_weak_signal():
    """One graded call is noise, not a measurement."""
    barely = stats("m", 0.99, 0.01, judged=MIN_JUDGED_FOR_SIGNAL - 1, judge_mean=0.10)
    value, source = barely.quality_signal
    assert source == "usable"
    assert value == pytest.approx(0.99)


def test_the_judge_columns_are_actually_queried():
    """Otherwise judged_samples is always 0 and the upgrade never fires."""
    import inspect
    from llm_router import telemetry

    src = inspect.getsource(telemetry.aggregate_stats)
    assert "COUNT(judge_score)" in src
    assert "AVG(judge_score)" in src
