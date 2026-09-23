"""S4 / U-04 — a local model's cost is memory and latency, not dollars.

Observed on 2026-09-22. The enforcement hook's documented escape valve is
"call ANY llm_* tool (even a trivial `llm(task="query")`) — clears the lock for
this turn". Taking it verbatim with the prompt "Reply with the single word:
acknowledged" routed to **qwen3-coder:30b**, which then held **42.6% of system
memory** and OOM-killed three background jobs, including the pre-release
verification.

That is not a misconfiguration. It is the reward function doing its job:

    expected_value = success_rate * ANSWER_VALUE_USD - avg_cost

`avg_cost` is DOLLARS. Every local model costs zero dollars, so the cost term
vanishes for all of them and the comparison collapses to success rate alone —
and a 30B model has a higher success rate than a 6.6B one. **"Pick the largest
free model" is precisely what this formula maximises.**

The machine had a 6.6 GB model available, and the routing hook's own drafting
path uses it. The escape valve took the 18.6 GB one.

WHAT THIS FILE DOES NOT DO. It does not change the reward. Adding a
memory or latency term changes which model every route picks, which is a
routing-behaviour change that needs evaluating on the target distribution —
not on the one anecdote that exposed it. CLAUDE.md: a proxy split has already
misled by 4.25 points, and a one-point calibration is a guess.

What it does is pin the MECHANISM, so the finding cannot be misremembered as
"the escape valve is badly configured" when it is the reward being
dollar-only.
"""

from __future__ import annotations

import pytest

from llm_router.telemetry import ModelStats


def _stats(model: str, success_rate: float, avg_cost: float,
           avg_latency_ms: float = 1000.0) -> ModelStats:
    """Build a stats row.

    Note `avg_latency_ms` is a REQUIRED field that ModelStats already carries —
    and `expected_value` does not read it. The data needed for a latency term
    is collected and unused, which is the same CLASS-A shape (a mechanism
    built, nothing reading it) that R12 was about, in the reward function.
    """
    return ModelStats(
        model=model,
        n_samples=100,
        success_rate=success_rate,
        avg_cost=avg_cost,
        avg_latency_ms=avg_latency_ms,
    )


def test_latency_is_already_collected_and_the_reward_ignores_it():
    """The cheapest half of S4's fix is already measured.

    `avg_latency_ms` is a required field on every stats row. `expected_value`
    reads `success_rate` and `avg_cost` only. Whatever a full fix does about
    memory, the latency term needs no new plumbing — which is worth knowing
    before anyone scopes it as large.
    """
    slow = _stats("ollama/big", 0.85, 0.0, avg_latency_ms=45_000.0)
    fast = _stats("ollama/small", 0.85, 0.0, avg_latency_ms=800.0)
    assert slow.expected_value == fast.expected_value, (
        "the reward now distinguishes a 45-second call from an 0.8-second one; "
        "S4 is partly fixed and this test plus the plan need updating together"
    )


def test_the_reward_is_blind_to_everything_but_dollars():
    """Two free models differing only in size rank purely on success rate."""
    small = _stats("ollama/qwen3.5:latest", success_rate=0.80, avg_cost=0.0)
    large = _stats("ollama/qwen3-coder:30b", success_rate=0.85, avg_cost=0.0)

    assert large.expected_value > small.expected_value, (
        "the premise has changed: the larger free model no longer wins on "
        "expected value, so this finding needs re-deriving"
    )
    # And the margin is ENTIRELY the success-rate difference — the cost term
    # contributed nothing, because both are zero dollars.
    from llm_router.telemetry import ANSWER_VALUE_USD

    assert large.expected_value - small.expected_value == pytest.approx(
        (0.85 - 0.80) * ANSWER_VALUE_USD
    ), (
        "the gap between two free models is not purely their success-rate "
        "difference, so something other than dollars is already in the reward"
    )


def test_an_18gb_model_and_a_300mb_model_are_priced_identically():
    """The finding, stated as a property rather than an anecdote.

    `nomic-embed-text` (0.3 GB) and `qwen3-coder:30b` (18.6 GB) were both
    present on the machine where this was measured. The reward cannot tell
    them apart on cost.
    """
    tiny = _stats("ollama/nomic-embed-text:latest", success_rate=0.70, avg_cost=0.0)
    huge = _stats("ollama/qwen3-coder:30b", success_rate=0.70, avg_cost=0.0)

    assert tiny.expected_value == huge.expected_value, (
        "a memory or latency term has entered the reward. That is the fix S4 "
        "describes — update this test and the plan together, and evaluate the "
        "change on the target distribution rather than on this pair."
    )


def test_a_paid_model_still_loses_when_overpriced():
    """The other half: the dollar term is not broken, it is incomplete.

    The reward correctly prefers a cheap-and-good model over an
    expensive-and-good one. S4 is that it has no term for a resource that is
    not billed, not that the billing term is wrong.
    """
    from llm_router.telemetry import ANSWER_VALUE_USD

    free_ok = _stats("ollama/x", success_rate=0.80, avg_cost=0.0)
    paid_great = _stats("anthropic/y", success_rate=0.99, avg_cost=0.02)
    paid_gouging = _stats("anthropic/z", success_rate=0.99, avg_cost=0.10)

    assert paid_gouging.expected_value < free_ok.expected_value, (
        "an overpriced model beats a free one; the dollar term is broken, "
        "which is a different and worse finding than S4"
    )
    # The documented example from the reward's own docstring.
    assert paid_great.expected_value == pytest.approx(
        0.99 * ANSWER_VALUE_USD - 0.02
    )


def test_the_escape_valve_is_documented_as_cheap():
    """If the hook promises 'trivial', the promise should be checkable.

    The wording is currently "even a trivial `llm(task="query")`", which reads
    as a cost claim. It is not enforced anywhere — the tier the CALLER passes
    is advisory, and the bandit picks regardless. Pinned so the wording and
    the behaviour are compared rather than assumed to match.
    """
    import pathlib

    hook = (
        pathlib.Path(__file__).resolve().parents[1]
        / "src" / "llm_router" / "hooks" / "enforce-route.py"
    )
    text = hook.read_text(encoding="utf-8")
    if "even a trivial" not in text:
        pytest.skip("the escape-valve wording has changed; re-check S4")
    assert "trivial" in text
    # No assertion that it IS cheap — it is not, and claiming otherwise here
    # would be the documentation defect this audit exists to remove. The
    # finding is recorded in audit/25_REMEDIATION_PLAN_2.md, S4.
