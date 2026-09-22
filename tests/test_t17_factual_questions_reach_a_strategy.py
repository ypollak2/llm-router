"""`propose` and `eligibility` must agree about what is verifiable — T-17.

A FACTUAL checkable question — "What is the capital of Portugal?" — is the shape
`eligibility` is proudest of admitting: it sets `verifier_class = V_MECHANICAL`
and `needs_reference_answer = True`, and the audit's corpus work found these to
be the most gradable population in real traffic.

`propose.select_strategy` then fell through every branch and returned
`no_reliable_verifier`. So a candidate the gate admitted could never be proposed
for, and the two halves of the subsystem disagreed about the same task.

`S_REFERENCE` ("frozen_reference") already WAS the strategy for this; it was
simply unreachable except via `external_evidence`. A missing reference answer is
a blocker — something to capture — not a different strategy, and losing that
distinction is what turned "we need an answer key" into "this is ungradable".
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

from groundtruth import contract as ct         # noqa: E402
from groundtruth import dataset as ds          # noqa: E402
from groundtruth import eligibility as el      # noqa: E402
from groundtruth import propose as pr          # noqa: E402

CHECKABLE = "What is the capital of Portugal?"
SPECULATIVE = "What do you think the best web framework will be in 2030?"
PERSONAL = "What should I name my cat?"


def _task(prompt, **kw):
    return pr.TaskInput(task_id="gtc-t17", task=prompt, task_type="query", **kw)


def _strategy(prompt, **kw):
    task = _task(prompt, **kw)
    contract = ct.derive(task.task_id, task.task, existing_tests=[])
    return pr.select_strategy(task, contract, existing=[])


# ── the disagreement ─────────────────────────────────────────────────────────

def test_eligibility_admits_a_checkable_factual_question():
    """The premise. If this ever stops being true, the finding is moot."""
    e = el.assess(CHECKABLE)
    assert e.verification_candidate, e.ineligibility_reasons
    assert e.verifier_class == ds.V_MECHANICAL
    assert e.needs_reference_answer is True


def test_propose_no_longer_drops_it_on_the_floor():
    """The gate: an admitted checkable question must reach a real strategy."""
    strategy, blockers = _strategy(CHECKABLE)
    assert strategy != pr.S_NONE, (
        f"eligibility admits this shape but propose returns {strategy!r} "
        f"(blockers: {blockers})"
    )
    assert strategy == pr.S_REFERENCE


def test_the_strategy_is_a_mechanical_one():
    """Otherwise it can never become an ACTIVE verifier."""
    strategy, _ = _strategy(CHECKABLE)
    assert strategy in pr.MECHANICAL_STRATEGIES


def test_the_missing_answer_key_is_a_blocker_not_a_dead_end():
    """The distinction the old code lost.

    "We need an answer key" is work to do; "this is ungradable" closes the task.
    """
    _strategy_, blockers = _strategy(CHECKABLE)
    assert any("reference answer" in b for b in blockers), blockers
    assert not any("nothing in the envelope" in b for b in blockers), blockers


# ── anti-vacuity: it must not admit everything ───────────────────────────────

@pytest.mark.parametrize("prompt", [SPECULATIVE, PERSONAL])
def test_an_unanswerable_question_still_gets_no_strategy(prompt):
    """A speculative or first-person question has no reference answer, however
    confidently it is phrased. If this passes, `select_strategy` has become a
    rubber stamp and every test above is meaningless."""
    strategy, blockers = _strategy(prompt)
    assert strategy == pr.S_NONE, (
        f"{prompt!r} was given strategy {strategy!r} — propose now admits "
        "questions eligibility itself rejects"
    )
    assert blockers


def test_eligibility_agrees_those_are_not_checkable():
    """Both sides of the disagreement, pinned together."""
    for prompt in (SPECULATIVE, PERSONAL):
        e = el.assess(prompt)
        assert e.verifier_class != ds.V_MECHANICAL or not e.needs_reference_answer


# ── the rule lives in one place ──────────────────────────────────────────────

def test_propose_delegates_the_question_shape_to_eligibility():
    """T-17 was two modules disagreeing. A second copy of the rule guarantees
    they drift apart again — which is precisely how the scrubbers drifted four
    secret classes apart earlier in this audit.

    R13/A-10: `"_is_checkable_question" in inspect.getsource(...)` is
    satisfied by the name appearing in a comment (or the docstring, which
    already mentions it) with the real delegating call deleted. `assert_calls`
    matches an actual Call node via `ast.unparse`, so a comment cannot pass
    it. Likewise `"re.compile" not in src` would be silently satisfied by
    deleting a comment that happened to mention it while a real regex crept
    in some other way (e.g. `re.match`/`re.search`); `assert_not_calls`
    checks for any call whose unparsed form contains `re.compile` specifically
    — the actual shape-rule duplication this test forbids — over the real
    call graph.
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _ast_assert import assert_calls, assert_not_calls

    assert_calls(pr._is_reference_checkable, "_is_checkable_question")
    assert_not_calls(
        pr._is_reference_checkable, "re.compile",
        msg="propose has grown its own copy of the shape rule",
    )


def test_an_explicit_non_mechanical_class_is_not_overruled():
    """Eligibility's judgement on the full envelope beats a prompt-shape guess."""
    strategy, _ = _strategy(CHECKABLE, expected_verification_class=ds.V_HUMAN)
    assert strategy == pr.S_NONE


def test_code_work_is_unaffected():
    """The new branch sits after every code path; it must not shadow them."""
    strategy, _ = _strategy(
        "Add a retry to src/client.py",
        named_files=["src/client.py"], repo_commit="abc123",
        repo_reconstructable=True, test_command="pytest",
    )
    assert strategy == pr.S_TASK_TEST
