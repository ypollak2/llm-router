"""The three invariants that survived the entire suite (verdict 19, G-F).

A mutation sample chosen independently of the remediation found three
behaviour-changing defects that the whole test suite passed over. Each was
confirmed by its own dedicated full-suite run — not merely by the narrow subset
its mutation named, because `B8` proved that distinction matters (it looked
uncovered, and was in fact covered by a test filed where nobody would look).

| | area | mutation that survived |
|---|---|---|
| **B1** | money | `_host_opus_rates()` input and output rates **swapped** |
| **B4** | routing | the registration guard made to always answer `True` |
| **B9** | verification | the budget pressure cap cut from 0.5 to 0.05 |

**B1 is the most consequential.** Those two numbers are the baseline every
savings figure in the system is computed against, and output tokens cost five
times input — so swapping them makes every counterfactual wrong by a large
factor while leaving it plausibly shaped.

**B4 is the most pointed.** A guard that cannot answer "no" is blind spot
Q3(c)'s exact shape — the one the original audit recorded as CLOSED on the
strength of `unregistered()` checking tier constants against `_TIERS`.

WHAT THESE TESTS DO AND DO NOT PROVE
------------------------------------
They close three named holes. They are **not** evidence that coverage improved
in general, and re-scoring the same mutation sample after adding them would not
be either — that measures against exactly what was just fixed, which is the
error the verdict exists to record. Only a sample drawn independently of this
file can say whether coverage is better, and only a **pre-registered** one can
restore G-F's design intent.
"""

from __future__ import annotations

import pytest


# ── B1 (money): the baseline rate pair must not be transposable ──────────────

def test_host_opus_rates_returns_input_then_output():
    """Order is load-bearing: callers unpack `(input_rate, output_rate)`.

    Swapping them survived the entire suite. The assertion is on ORDER against
    the canonical constants, not on the literal numbers — pinning 5.0/25.0 here
    would turn an ordinary price update into a test failure and teach the next
    maintainer to edit the assertion rather than read it.
    """
    from llm_router.cost import _HOST_INPUT_PER_M, _HOST_OUTPUT_PER_M
    from llm_router.execution_ledger import _host_opus_rates

    assert _host_opus_rates() == (float(_HOST_INPUT_PER_M), float(_HOST_OUTPUT_PER_M)), (
        "the baseline rate pair is transposed: every savings figure in the "
        "system is computed against these two numbers, in this order"
    )


def test_output_tokens_cost_more_than_input_tokens():
    """An independent check on the same defect, resolved against a fact about
    the world rather than against the constants themselves.

    Every frontier model prices output above input. If the pair is ever
    transposed at the source, the assertion above compares two equally-swapped
    values and passes; this one does not.
    """
    from llm_router.execution_ledger import _host_opus_rates

    in_rate, out_rate = _host_opus_rates()
    assert out_rate > in_rate, (
        f"input {in_rate} >= output {out_rate}: output tokens are priced above "
        "input by every frontier provider, so this pair is almost certainly "
        "transposed"
    )


# ── B4 (routing): the registration guard must be able to answer "no" ─────────

def test_is_registered_rejects_an_unregistered_name():
    """The guard made to always return True survived the whole suite.

    This is Q3(c): a bogus canonical tool name passed the lint and 106 tests,
    and the blind spot was recorded CLOSED. A guard that cannot say no is not a
    guard.
    """
    from llm_router.tool_surface import is_registered

    assert is_registered("llm_bogus_zzz", "core") is False, (
        "the registration guard accepts a name that is not registered — it "
        "cannot answer 'no', which is the only answer that matters"
    )


def test_is_registered_still_accepts_a_real_name():
    """The complement. A guard hardened into always answering "no" would pass
    the test above and break every route."""
    from llm_router.tool_surface import is_registered

    assert is_registered("llm_query", "core") is True


@pytest.mark.parametrize("tier", ["core", "routing", "consolidated"])
def test_the_guard_discriminates_in_every_restrictive_tier(tier):
    """`off` is excluded deliberately: `_TIERS["off"] is None` means *every*
    tool is registered, so `True` there is correct rather than blind. Asserting
    over it would make this test pass for the wrong reason in one case."""
    from llm_router.tool_surface import is_registered

    assert is_registered("llm_bogus_zzz", tier) is False


# ── B9 (verification): the budget pressure cap ───────────────────────────────

def test_pending_pressure_is_capped_at_fifty_percent():
    """Cutting the cap tenfold (0.5 -> 0.05) survived the entire suite.

    The cap decides how much in-flight spend can push the router toward cheaper
    models. At 0.05 the pressure signal effectively stops mattering, and the
    inline comment still reads "Cap at 50%" — which is how a silent tenfold
    change survives a code review as well as a test run.
    """
    from llm_router import budget

    budget._pending_tokens.update({"cap-probe": 10**9})
    try:
        assert budget._get_pending_pressure_offset("cap-probe") == pytest.approx(0.5), (
            "the pending-pressure cap is no longer 0.5; budget pressure can no "
            "longer reach a level that changes routing"
        )
    finally:
        budget._pending_tokens.pop("cap-probe", None)


def test_pending_pressure_scales_below_the_cap():
    """Pins the *slope*, not just the ceiling. A function returning a constant
    0.5 would satisfy the cap test above while carrying no information."""
    from llm_router import budget

    budget._pending_tokens.update({"slope-probe": 5000})
    try:
        offset = budget._get_pending_pressure_offset("slope-probe")
        assert 0.0 < offset < 0.5, f"expected a proportional value, got {offset}"
    finally:
        budget._pending_tokens.pop("slope-probe", None)


def test_no_pending_spend_means_no_pressure():
    """The zero case must stay zero: an idle provider must not be nudged toward
    cheaper models by a pressure signal that has nothing to measure."""
    from llm_router import budget

    budget._pending_tokens.pop("idle-probe", None)
    assert budget._get_pending_pressure_offset("idle-probe") == 0.0
