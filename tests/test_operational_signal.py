"""B2 — the operational-intent signal that makes `llm_delegate` classifier-selectable.

High precision by design: this drives an ENFORCED hard-route, so a false positive
hijacks a normal prompt into a heavy multi-step delegation. The gate is therefore
CHANGE_VERB *and* VERIFY_CUE, minus anything explanatory/interrogative.
"""
from __future__ import annotations

import pytest

from llm_router.operational_signal import OperationalSignal, detect_operational, is_operational

# Prompts that genuinely need tool-execution + objective verification → delegate.
_OPERATIONAL = [
    "Fix the failing test in parser.py and make it pass.",
    "Implement add(a, b) in add.py and add a unit test that asserts add(2,3)==5.",
    "Refactor the auth module and ensure the existing tests still pass.",
    "Migrate the config loader and verify the build is green.",
    "Add a /health endpoint and a test that checks it returns 200.",
    "Debug the crash and confirm the suite passes afterwards.",
]

# Prompts that must NOT fire: explanatory, interrogative, or codegen without any
# verification demand (a one-shot completion tool handles those).
_NON_OPERATIONAL = [
    "Explain how the parser works.",
    "Why does this test fail?",
    "What is the difference between asyncio and threading?",
    "Summarize the auth module for me.",
    "Implement a REST endpoint for users.",          # change verb, but no verify cue
    "Write a poem about spring.",                     # 'write' but not code + no verify
    "Should I use pytest or unittest?",
    "Describe the migration strategy.",
    "How do I run the tests?",                        # interrogative
    # False positives the audit found with the old bare-word cues — must stay dead:
    "Please write a concise explanation and ensure it has examples.",  # ensure/explanation
    "Generate a personality test for onboarding.",                     # 'test' = HR quiz
    "Make the button green and add a short label.",                    # 'green' = CSS
    "Create a summary of the insurance coverage options.",             # coverage = insurance
    "Write a short example showing how to pass a value by reference.",  # 'pass' = arg passing
    "Write a rubric for a passing grade in this course.",              # passing = grade
    "Generate a checklist for replacing a lost boarding pass.",        # pass = travel
    "Write an explanation of what this unit test is checking.",        # content-object short-circuit
    # P2-S1 residual false positives the audit found — content deliverables that
    # happen to contain software-verification words. Must stay dead.
    "Generate a regression test plan.",                                # 'test plan' deliverable
    "Build a training exercise so the assertions pass in this lesson.",  # exercise / lesson
    "Generate a quiz that tests CI concepts.",                         # quiz
    "Create a worksheet with exam questions on unit testing.",         # worksheet / exam
    "Write a curriculum about test coverage.",                         # curriculum
]


@pytest.mark.parametrize("prompt", _OPERATIONAL)
def test_operational_prompts_fire(prompt):
    assert is_operational(prompt) is True, prompt


@pytest.mark.parametrize("prompt", _NON_OPERATIONAL)
def test_non_operational_prompts_do_not_fire(prompt):
    assert is_operational(prompt) is False, prompt


def test_detect_returns_reason_for_transparency():
    sig = detect_operational("Fix the failing test in parser.py and make it pass.")
    assert isinstance(sig, OperationalSignal)
    assert sig.fires is True
    assert sig.verb and sig.cue          # both signals captured for logging
    assert "fix" in sig.reason.lower()


def test_detect_non_operational_has_no_reason():
    sig = detect_operational("Explain how the parser works.")
    assert sig.fires is False
    assert sig.verb is None or sig.cue is None   # at least one axis missing


def test_reason_and_axes_recorded_on_every_non_firing_branch():
    """Transparency contract: each non-firing branch must name WHY it declined,
    and a lone matched verb/cue must still be surfaced for the enforced-route audit
    log. (Also kills mutants that blank/mangle the reason or drop verb/cue here.)"""
    # Explanatory lead.
    s = detect_operational("Explain how the parser works.")
    assert s.fires is False and "explanatory" in s.reason
    # Prose/content deliverable (short-circuits before the verb/cue check).
    s = detect_operational("Write a poem about spring.")
    assert s.fires is False and "prose" in s.reason
    # Change verb but NO verify cue → declines, records the matched verb.
    s = detect_operational("Implement a REST endpoint for users.")
    assert s.fires is False
    assert s.verb == "Implement" and s.cue is None
    assert "missing change verb or verification cue" in s.reason
    # Verify cue but NO change verb → declines, records the matched cue.
    s = detect_operational("The unit test is comprehensive and well organized.")
    assert s.fires is False
    assert s.verb is None and s.cue is not None
    assert "missing change verb or verification cue" in s.reason


def test_empty_and_none_are_safe():
    assert is_operational("") is False
    assert is_operational(None) is False          # type: ignore[arg-type]
    assert detect_operational("").fires is False


def test_explanatory_prefix_overrides_even_with_verb_and_cue():
    # "explain how to fix the failing test" describes, not requests execution.
    assert is_operational("Explain how to fix the failing test so it passes.") is False
