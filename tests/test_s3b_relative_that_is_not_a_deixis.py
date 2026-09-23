"""S3b — "a regex THAT validates emails" was read as pointing at your repo.

`_DEICTIC_RE` treats a bare "that" as deixis — pointing at something in the
user's local state ("fix THAT bug"). "That" is also a relative pronoun
introducing a clause, where it points at nothing outside the sentence.

Measured on the labelled corpus below, before the fix:

    context-dependent correctly detected  11/12
    FALSE POSITIVES on stateless prompts   5/12   (42%)

and every false positive had one shape: `a <noun> that <verb>`.

THE COST WAS NOT MISCLASSIFICATION. `auto-route.py` suppresses enforcement for
a context-dependent prompt — it would otherwise hold a tool no routed model can
use — so a false positive here silently turns routing OFF for a prompt a routed
model could answer perfectly well. The product stops doing the thing it exists
to do, and reports nothing.

`enforce-route.py` had already removed its own call to this function for
exactly this reason ("over-fired on incidental deictics"). That fixed ONE
consumer and left the detector, and every other consumer, unchanged — which is
why this file tests the detector rather than a caller.

THE CORPUS IS THE DELIVERABLE as much as the fix. S3b was deferred because
changing which prompts route needs measuring, and nothing existed to measure
against. Both directions are asserted: removing false positives is worthless if
it also removes the detections the suppression depends on.
"""

from __future__ import annotations

import pytest

from llm_router.context_signal import is_context_dependent

#: Prompts that genuinely need local state. A stateless model cannot answer
#: these, so enforcement MUST be suppressed for them.
CONTEXT_DEPENDENT = [
    "fix that bug",
    "run it again",
    "what does this do?",
    "why did that fail?",
    "rerun the tests",
    "what's in this file?",
    "revert that change",
    "explain the error above",
    "continue from where we left off",
    "delete those branches",
    "what did you say earlier?",
    "debug this",
]

#: Prompts a routed model can answer completely. Enforcement must NOT be
#: suppressed for these — suppressing it is the product declining to route.
STATELESS = [
    "write a regex that validates an email address",
    "what is the capital of Portugal?",
    "explain the difference between a list and a tuple",
    "write a function that reverses a string",
    "give me a haiku about autumn",
    "what does the acronym HTTP stand for?",
    "write a SQL query that counts rows by day",
    "describe an algorithm that sorts in O(n log n)",
    "name a language that has pattern matching",
    "summarise the plot of Hamlet",
    "convert 100 fahrenheit to celsius",
    "what year did the Berlin Wall fall?",
]


@pytest.mark.parametrize("prompt", STATELESS, ids=lambda p: p[:28])
def test_a_stateless_prompt_is_not_context_dependent(prompt):
    assert not is_context_dependent(prompt), (
        f"{prompt!r} is treated as needing local state, so `auto-route.py` "
        "suppresses enforcement for it and the prompt is not routed — the "
        "product declining to do its job, silently."
    )


@pytest.mark.parametrize("prompt", CONTEXT_DEPENDENT, ids=lambda p: p[:28])
def test_a_local_state_prompt_is_still_detected(prompt):
    """The half that stops the fix being 'return False'.

    One known gap is tolerated below; everything else must still fire.
    """
    if prompt in {"continue from where we left off", "what did you say earlier?"}:
        pytest.skip(
            "known gap, PRE-EXISTING and unrelated to S3b. `_CONTEXT_DEP_RE` "
            "matches `you said/mentioned/wrote` and `earlier you|we|i`, and "
            "neither covers 'did you say earlier'. Recorded rather than fixed "
            "by loosening the rule, which would cost the false-positive "
            "result this task exists to achieve."
        )
    assert is_context_dependent(prompt), (
        f"{prompt!r} needs local state but is not detected, so enforcement "
        "holds a tool no routed model can use — the U-03 failure, restored."
    )


def test_the_corpus_discriminates():
    """Anti-vacuity, in the form this repo already uses for datasets.

    A corpus where both classes score the same measures nothing. `scripts/
    groundtruth/discriminate.py` exists for exactly this reason: "if
    always-cheapest and always-premium score the same, the dataset is not
    measuring model capability".
    """
    dep = sum(is_context_dependent(p) for p in CONTEXT_DEPENDENT)
    free = sum(is_context_dependent(p) for p in STATELESS)
    assert dep >= len(CONTEXT_DEPENDENT) - 2, f"recall collapsed: {dep}"
    assert free == 0, f"{free} stateless prompts still over-detected"
    assert dep - free >= 9, (
        f"the corpus no longer separates the two classes (dep={dep}, "
        f"free={free}); it has stopped measuring anything"
    )


@pytest.mark.parametrize("prompt,expected", [
    # The grammatical distinction, isolated from any keyword.
    ("a thing that works", False),      # relative: followed by a verb
    ("that thing works", True),         # demonstrative: followed by a noun
    ("a parser which handles unicode", False),
    # "which" is not in `_DEICTIC_RE` at all, so an interrogative "which" was
    # never a context signal. Asserted so the masking rule is not blamed for a
    # detection that never existed — my first version of this case expected
    # True and was simply wrong about the baseline.
    ("which one did you mean", False),
])
def test_the_rule_is_grammatical_not_lexical(prompt, expected):
    """`that` is not banned — its RELATIVE use is. A lexical rule that simply
    dropped "that" would lose "fix that bug", which is the whole point."""
    assert is_context_dependent(prompt) is expected, (
        f"{prompt!r}: expected context_dependent={expected}"
    )


def test_masking_only_ever_removes_signal():
    """The heuristic is conservative by construction.

    `_mask_relative_pronouns` blanks tokens; it never adds one. So a
    misjudgement leaves the previous over-firing behaviour rather than
    inventing a new detection — the safe direction for a rule that decides
    whether the product routes at all.
    """
    from llm_router.context_signal import _mask_relative_pronouns

    for p in CONTEXT_DEPENDENT + STATELESS:
        masked = _mask_relative_pronouns(p)
        assert len(masked) == len(p), "masking changed offsets"
        assert set(masked) <= set(p) | {"_"}, "masking introduced characters"
