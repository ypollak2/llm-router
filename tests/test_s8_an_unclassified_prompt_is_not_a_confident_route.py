"""S8 — half of real traffic is routed by a default, and nothing said so.

The finding entered the remediation plan as "word order changes the route":

    what is the capital of Portugal?            -> query
    tell me what the capital of Portugal is     -> analyze   (gateway)

Word order is the symptom. Measured, both prompts score ZERO in every category,
so `classify_signals` falls through to `policy.low_signal_default` — "query" for
the hook and the router, "analyze" for the gateway. The hook gets the better
answer by luck, not by measurement, and the two doors therefore return different
task types for the same prompt.

Measured 2026-09-23 over n=1571 real prompts (`scripts/groundtruth/sources.py`
drop rules applied first, removing 1389 system-noise / synthetic-session /
benchmark-sandbox records):

    score == 0 ....................................... 651/1571 = 41.4%
    weak (0 < score < _CONFIDENCE_THRESHOLD) .........  132/1571 =  8.4%
    gateway and hook return a different task_type .... 783/1571 = 49.8%

These tests do NOT assert which task type those prompts should get. There is no
labelled set to answer that on, and per CLAUDE.md a one-point calibration is a
guess. They assert the two things that were actually wrong: that the
fall-through is recorded, and that a default is never reported as a measurement.
"""

from __future__ import annotations

import pytest

import llm_router.classify as C
from llm_router import counter_registry


# Prompts that score nothing. Kept short and boring on purpose: if one of them
# ever starts scoring, the premise assertion below fails loudly rather than the
# rest of the file quietly testing nothing.
UNSCORED = [
    "tell me what the capital of Portugal is",
    "tell me what a closure is",
    "tell me what the default port is",
    "analyse why the build is failing",
]

SCORED = [
    "what is the capital of Portugal?",
    "what is a closure?",
]


@pytest.fixture(autouse=True)
def _fresh_counters():
    C.reset_low_signal_counters()
    yield
    C.reset_low_signal_counters()


# ── Premise ───────────────────────────────────────────────────────────────
# Assert the premise, not only the conclusion (CLAUDE.md, A-10 corollary). If
# these prompts were scoring after all, every test below would pass for the
# wrong reason.


@pytest.mark.parametrize("prompt", UNSCORED)
def test_the_prompt_really_does_score_nothing(prompt: str) -> None:
    scores = C._score_categories(prompt)
    assert max(scores.values()) == 0, (
        f"{prompt!r} now scores {dict(scores)} — the premise of S8 has changed; "
        "re-measure before trusting anything else in this file"
    )


@pytest.mark.parametrize("prompt", SCORED)
def test_the_control_prompt_really_does_score(prompt: str) -> None:
    assert max(C._score_categories(prompt).values()) >= C._CONFIDENCE_THRESHOLD


# ── The defect ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("prompt", UNSCORED)
def test_an_unscored_prompt_is_reported_as_not_confident(prompt: str) -> None:
    """The signal must not present a default as a classification."""
    assert C.classify_signals(prompt, C.GATEWAY_POLICY).confident is False
    assert C.classify_signals(prompt, C.HOOK_POLICY).confident is False


def test_the_two_doors_disagree_and_it_is_the_default_that_differs() -> None:
    """Pins the split so a one-word edit to `low_signal_default` cannot pass.

    Changing either policy's default silently re-routes ~half of real traffic.
    That is a routing change and it needs a measurement, not a preference; this
    test makes it impossible to land as an incidental edit.
    """
    prompt = "tell me what the capital of Portugal is"
    gw = C.classify_signals(prompt, C.GATEWAY_POLICY)
    hk = C.classify_signals(prompt, C.HOOK_POLICY)

    assert gw.task_type.value != hk.task_type.value
    assert gw.task_type.value == C.GATEWAY_POLICY.low_signal_default == "analyze"
    assert hk.task_type.value == C.HOOK_POLICY.low_signal_default == "query"
    # and neither of them measured anything
    assert gw.score == hk.score == 0


# ── The fix: the fall-through is counted, and the count is readable ───────


def test_the_fall_through_is_counted_with_its_denominator() -> None:
    for p in UNSCORED:
        C.classify_signals(p)
    for p in SCORED:
        C.classify_signals(p)

    low, total = C.low_signal_classifications()
    assert total == len(UNSCORED) + len(SCORED)
    assert low == len(UNSCORED), (
        "a confident classification must not be counted as a fall-through"
    )


def test_reading_the_counter_does_not_change_it() -> None:
    """S1. A counter consumed by being read lies to its second reader."""
    C.classify_signals(UNSCORED[0])
    first = C.low_signal_classifications()
    for _ in range(3):
        assert C.low_signal_classifications() == first


def test_a_process_that_classified_nothing_reads_UNKNOWN_not_a_clean_zero() -> None:
    """Denominator disappearance: 0 of 0 must never render as 0%."""
    reading = counter_registry.read_one("low_signal_classifications")
    assert reading.value is None
    assert "no prompt classified" in reading.unknown_reason


def test_the_counter_reaches_an_operator_surface() -> None:
    """R12: a counter with no reader is the CLASS-A defect, not instrumentation.

    `ClassifySignal.confident` recorded this fall-through from the day it was
    added and had zero readers in src/. That is exactly why S8 had to be found
    by an audit instead of by looking at `doctor`.
    """
    for p in UNSCORED:
        C.classify_signals(p)
    lines = counter_registry.render_lines()
    body = "\n".join(lines)
    assert "low_signal_classifications" in body
    assert "decided by the default" in body


def test_the_counter_alarms_only_on_the_share() -> None:
    """One fall-through in a thousand is normal; half of everything is not."""
    for _ in range(20):
        C.classify_signals(SCORED[0])
    assert counter_registry.read_one("low_signal_classifications").alarming is False

    for _ in range(20):
        C.classify_signals(UNSCORED[0])
    assert counter_registry.read_one("low_signal_classifications").alarming is True
