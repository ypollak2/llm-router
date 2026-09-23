"""S3 / U-03 — the two routing layers must not contradict each other.

Observed live on 2026-09-22, in one turn:

  * UserPromptSubmit injected: "CONTEXT-DEPENDENT PROMPT — this references your
    local files / repo / history / state, which a stateless routed model cannot
    see. No blind draft was generated (it would be fabrication)."
  * PreToolUse simultaneously held `Bash` under HARD enforcement, task
    `research/moderate`.

The request was to read this machine's git state and memory — something no
external model can perform. One layer knew that and said so; the other blocked
the only tool that could answer it.

THE CONTRACT ALREADY EXISTS IN THE CODE, and this file pins it. `auto-route.py`
sets `write_pending = False` for a context-dependent prompt, with a comment
naming the exact failure that was observed:

    "writing pending enforcement state here is what forced the
     throwaway-llm_query dance (enforce-route.py blocked Bash/Edit/Read until a
     llm_router tool was called, then Claude did the real work anyway — double
     cost). So SUPPRESS ENFORCEMENT."

Somebody found this and fixed it. Nothing asserted it, so nothing noticed when
a turn produced two verdicts anyway.

WHAT THIS FILE DELIBERATELY DOES NOT DO. It does not loosen
`_bash_exempt_from_hold`. `research` is a QA task type and is excluded on
purpose: a read-only command can ANSWER a Q&A question natively (`cat the
file`, then reply from it), which is the bypass enforcement exists to stop.
`_bash_exempt_from_hold`'s own docstring says the misclassification is "fixed
where it originates, in the classifier's `research` intent pattern, not by
loosening this gate" — and changing the classifier is a routing-behaviour
change that needs evaluating on the target distribution, not on the one prompt
that exposed it. Recorded as S3-deferred in remediation plan II.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "llm_router"
AUTO_ROUTE = SRC / "hooks" / "auto-route.py"


def _auto_route_tree() -> ast.Module:
    return ast.parse(AUTO_ROUTE.read_text(encoding="utf-8"))


def test_a_context_dependent_prompt_writes_no_pending():
    """The invariant, asserted on the AST of the branch that implements it.

    A source-text check would pass with the assignment deleted and the comment
    left behind — the A-10 evasion, in the file whose comment is the only
    record of why this matters.
    """
    tree = _auto_route_tree()

    ctx_branches = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and "_is_context_dependent" in ast.unparse(node.test)
        and "zero_claude" in ast.unparse(node.test)
    ]
    assert ctx_branches, (
        "no `if not zero_claude and _is_context_dependent(prompt):` branch "
        "found. The suppression that stops enforcement holding a tool no "
        "routed model can use is gone."
    )

    # More than one branch tests the same condition — the OKF-retrieval branch
    # is also gated on it and legitimately has nothing to do with enforcement.
    # The requirement is that AT LEAST ONE such branch suppresses the pending,
    # and that no such branch ever ENABLES it.
    suppressing = []
    for branch in ctx_branches:
        assignments = [
            ast.unparse(n)
            for n in ast.walk(branch)
            if isinstance(n, ast.Assign)
            and any(getattr(t, "id", "") == "write_pending" for t in n.targets)
        ]
        assert not [a for a in assignments if "True" in a], (
            f"a context-dependent branch sets write_pending TRUE: {assignments}. "
            "A prompt about local state cannot be answered by a routed model, "
            "and holding a tool for it forces the throwaway-query dance the "
            "comment above that branch describes."
        )
        if any("False" in a for a in assignments):
            suppressing.append(branch.lineno)

    assert suppressing, (
        "no context-dependent branch sets write_pending = False. Enforcement "
        "will hold a tool for a prompt only local state can answer — the "
        "behaviour observed on 2026-09-22, and the behaviour the comment in "
        "auto-route.py says was deliberately fixed."
    )


def test_the_suppression_is_not_reachable_only_under_zero_claude():
    """`not zero_claude` guards it. Under zero-Claude the route IS the answer,
    so enforcement is correct there — but the branch must still exist for the
    ordinary case, which is the one that was observed failing."""
    tree = _auto_route_tree()
    tests = [
        ast.unparse(n.test) for n in ast.walk(tree)
        if isinstance(n, ast.If) and "_is_context_dependent" in ast.unparse(n.test)
    ]
    assert any("not zero_claude" in t for t in tests), (
        f"the context-dependent branch is no longer scoped to non-zero-Claude "
        f"mode: {tests}"
    )


@pytest.mark.parametrize("prompt", [
    "check the git state of this repo and the machine's memory",
    "what does src/llm_router/cost.py line 42 do?",
    "why did the last test run fail?",
    "read the config file and tell me the enforce mode",
])
def test_local_state_prompts_are_detected_as_context_dependent(prompt):
    """The detector must actually fire on the shapes that provoked this.

    If it does not, the suppression above is correct and unreachable, which is
    the vacuous-mechanism failure this audit kept finding: a fix that is right
    and never runs.
    """
    from llm_router.context_signal import is_context_dependent

    assert is_context_dependent(prompt), (
        f"{prompt!r} is not detected as context-dependent, so the enforcement "
        "suppression never fires for it and PreToolUse will hold a tool that "
        "is the only thing capable of answering."
    )


@pytest.mark.parametrize("prompt", [
    # FIXED by S3b (2026-09-23). This carried an xfail(strict=True) recording
    # that the deictic 'that' over-fired; the marker XPASSed the moment
    # `_mask_relative_pronouns` landed, which failed the suite and forced this
    # file and the corpus to be updated together. That is the mechanism
    # working as designed.
    "write a regex that validates an email address",
    "what is the capital of Portugal?",
])
def test_genuinely_routable_prompts_are_not_over_detected(prompt):
    """The other half. A detector that fires on everything suppresses all
    enforcement, which is the same as turning routing off.

    FIXED by S3b. Retained because the reasoning is the record of why the
    detector is grammatical rather than lexical, and because this is the
    consumer that proves the fix reaches enforcement.

    It WAS a live defect. `enforce-route.py` deleted its
    own `is_context_dependent()` re-check because it "over-fired on incidental
    deictics ('Generate a regex THAT validates emails')". The CONSUMER was
    removed; the DETECTOR was not fixed.

    So the over-firing is still there for every other consumer — including the
    `write_pending = False` suppression this file pins two tests above. The
    consequence is the opposite polarity to the bug that motivated S3:
    enforcement is suppressed for prompts a routed model could answer perfectly
    well, which costs routing rather than correctness. Both are the same root
    cause: one detector, two consumers, no shared test.

    It was carried as `xfail(strict=True)` until a corpus existed to measure
    against. `tests/test_s3b_relative_that_is_not_a_deixis.py` is that corpus:
    26 labelled prompts, false positives 5/12 -> 0/12 with recall unchanged.
    """
    from llm_router.context_signal import is_context_dependent

    assert not is_context_dependent(prompt), (
        f"{prompt!r} is treated as context-dependent, so enforcement is "
        "suppressed for a prompt a routed model could answer perfectly well"
    )


def test_the_qa_bash_hold_is_deliberate_and_documented():
    """Pin the decision NOT taken, so it is not quietly reversed.

    `research` stays non-exempt on purpose. If someone loosens it, this fails
    and points at the reasoning rather than letting the change pass as a
    tidy-up.
    """
    import importlib.util

    path = SRC / "hooks" / "enforce-route.py"
    spec = importlib.util.spec_from_file_location("_er_probe", path)
    er = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(er)

    assert "research" in er._QA_TASK_TYPES, (
        "`research` was removed from the QA task types, which exempts "
        "read-only Bash from the hold. That is the documented bypass: a "
        "read-only command can answer a Q&A question natively. If this is "
        "intended, the fix belongs in the classifier — see "
        "`_bash_exempt_from_hold`'s docstring."
    )
    # And the exemption still refuses QA even for a local-only command.
    assert er._bash_exempt_from_hold("research", "git status", False) is False
    # ...while an operational task with a local command is exempt.
    assert er._bash_exempt_from_hold("coordination", "git status", False) is True
