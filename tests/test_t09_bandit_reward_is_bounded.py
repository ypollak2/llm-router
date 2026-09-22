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

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

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
    """Rule B: the call site, not the property in isolation.

    R13/A-10. This docstring said exactly that and then ran
    `"s.expected_value" in inspect.getsource(bandit)`, which is satisfied by
    the phrase appearing in a comment — including the module docstring, which
    mentions `expected_value` on line 11. The assertion could not distinguish
    "the bandit ranks on it" from "somebody wrote it down".

    Now asserted on the AST of the `max(..., key=...)` expressions that DO the
    ranking. Comments are not in the AST, so a phrase left behind cannot
    satisfy this.
    """
    import ast
    import inspect

    from llm_router import bandit

    tree = ast.parse(inspect.getsource(bandit))
    ranking_keys = [
        ast.unparse(kw.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "max"
        for kw in node.keywords
        if kw.arg == "key"
    ]
    assert ranking_keys, (
        "the bandit no longer ranks with max(..., key=...) — this assertion "
        "has stopped looking at the thing that chooses a model"
    )
    assert all("expected_value" in k for k in ranking_keys), (
        f"a ranking key does not use expected_value: {ranking_keys}"
    )
    # And the old unbounded ratio is gone from every value the code USES, not
    # merely from its prose.
    from _ast_assert import string_constants

    used = {ast.unparse(n) for n in ast.walk(tree)
            if isinstance(n, ast.Attribute)} | set(string_constants(tree))
    offenders = [u for u in used if "success_per_dollar" in u]
    assert not offenders, (
        f"the bandit is ranking on the old unbounded ratio: {offenders}"
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
    # R13: the SQL is a string the query runs, so assert on the string the
    # code uses. `"COUNT(judge_score)" in getsource(...)` was satisfied by the
    # docstring above, which names the column it is checking for.
    import sys
    from pathlib import Path

    from llm_router import telemetry

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _ast_assert import string_constants

    sql = string_constants(telemetry.aggregate_stats)
    for needed in ("COUNT(judge_score)", "AVG(judge_score)"):
        assert any(needed in q for q in sql), (
            f"{needed} is not in any SQL this function runs, so judged_samples "
            f"is always 0 and the upgrade never fires"
        )
