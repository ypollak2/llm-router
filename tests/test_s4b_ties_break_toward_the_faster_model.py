"""S4b — the reward ties, and the tie landed on the slowest model.

`expected_value = success_rate * ANSWER_VALUE_USD - avg_cost`. For a free model
the cost term is zero, so two free models with the same success rate score
IDENTICALLY, and `max()` returns whichever iteration order reaches first.

Measured on the development ledger, restricted to rows with recorded
provenance (S4a — before that filter, 87% of the rows were placeholders):

    ollama/qwen3.8:latest    n=8    54.3s   EV=+0.05000   <- max() picked this
    ollama/lfm2.5:8b         n=99   10.1s   EV=+0.05000
    codex/gpt-5.5            n=8    49.2s   EV=+0.05000
    ollama/qwen3-coder:30b   n=96   13.6s   EV=+0.04583

A three-way tie resolved arbitrarily onto a 54-second model over a 10-second
one, on identical evidence.

WHY A TIE-BREAK RATHER THAN A COST TERM. Charging latency as dollars needs a
$/second rate, and none can be justified here: every trusted row is free or
subscription, so there is no paid/free trade-off to calibrate against. Picking
a rate anyway embeds a guess in the routing policy, and this repo's own rule is
that a one-point calibration is a guess.

A tie-break needs no rate, and cannot reorder any pair whose expected values
differ. It changes nothing the reward already decides — it replaces "arbitrary"
with "faster" exactly where the reward is silent.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from llm_router.bandit import _rank
from llm_router.telemetry import ModelStats

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "llm_router"


def _stats(model: str, sr: float, cost: float, ms: float) -> ModelStats:
    return ModelStats(
        model=model, n_samples=50, success_rate=sr,
        avg_cost=cost, avg_latency_ms=ms,
    )


def test_a_tie_breaks_toward_the_faster_model():
    """The exact case measured on the ledger."""
    slow = _stats("ollama/qwen3.8:latest", 1.0, 0.0, 54_300.0)
    fast = _stats("ollama/lfm2.5:8b", 1.0, 0.0, 10_100.0)

    assert slow.expected_value == fast.expected_value, (
        "the premise has changed — these no longer tie on expected value, so "
        "this test no longer exercises the defect"
    )
    assert max([slow, fast], key=_rank).model == fast.model
    # Order-independent: `max` returns the first maximum, so a tie-break that
    # did nothing would pass one ordering and fail the other.
    assert max([fast, slow], key=_rank).model == fast.model


def test_the_three_way_tie_from_the_ledger_resolves_to_the_fastest():
    models = [
        _stats("ollama/qwen3.8:latest", 1.0, 0.0, 54_300.0),
        _stats("ollama/lfm2.5:8b", 1.0, 0.0, 10_100.0),
        _stats("codex/gpt-5.5", 1.0, 0.0, 49_200.0),
        _stats("ollama/qwen3-coder:30b", 0.926, 0.0, 13_674.0),
    ]
    assert max(models, key=_rank).model == "ollama/lfm2.5:8b"


def test_the_tie_break_never_overrides_a_real_difference():
    """The property that makes this safe.

    A model that is genuinely better on expected value must win even if it is
    far slower. Otherwise this is not a tie-break, it is a latency policy
    wearing one.
    """
    better_but_slow = _stats("good/slow", 1.00, 0.0, 60_000.0)
    worse_but_fast = _stats("bad/fast", 0.50, 0.0, 100.0)

    assert better_but_slow.expected_value > worse_but_fast.expected_value
    assert max([better_but_slow, worse_but_fast], key=_rank).model == "good/slow"
    assert max([worse_but_fast, better_but_slow], key=_rank).model == "good/slow"


def test_a_cheaper_paid_model_still_beats_a_free_one_when_it_should():
    """The dollar term is untouched. S4b adds nothing to the money comparison."""
    from llm_router.telemetry import ANSWER_VALUE_USD

    free_mediocre = _stats("ollama/x", 0.50, 0.0, 1_000.0)
    paid_excellent = _stats("anthropic/y", 0.99, 0.02, 2_000.0)

    assert paid_excellent.expected_value > free_mediocre.expected_value
    assert max([free_mediocre, paid_excellent], key=_rank).model == "anthropic/y"
    assert paid_excellent.expected_value == pytest.approx(
        0.99 * ANSWER_VALUE_USD - 0.02
    )


def test_missing_latency_does_not_crash_or_win():
    """A stats row with no latency must not sort to the front by accident."""
    known = _stats("has/latency", 1.0, 0.0, 5_000.0)

    class _NoLatency:
        model = "no/latency"
        expected_value = known.expected_value

    winner = max([_NoLatency(), known], key=_rank).model
    assert winner == "has/latency", (
        "a row with no recorded latency (treated as 0ms) outranked a measured "
        "one. Absent is not fast."
    )


def test_both_ranking_call_sites_use_the_same_key():
    """`reorder()` picks a best model twice — explore and exploit. A tie-break
    applied to one and not the other would make the two paths disagree."""
    tree = ast.parse((SRC / "bandit.py").read_text(encoding="utf-8"))
    keys = [
        ast.unparse(kw.value)
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and ast.unparse(n.func) == "max"
        for kw in n.keywords if kw.arg == "key"
    ]
    assert keys, "the bandit no longer ranks with max(..., key=...)"
    assert set(keys) == {"_rank"}, (
        f"the ranking call sites disagree on their key: {keys}"
    )
