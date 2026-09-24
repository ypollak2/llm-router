"""audit/28 D-1 and D-2 — the context gate, measured and then changed.

D-1 · `_is_context_dependent` missed bare continuations
    Measured over n=1606 real prompts before the change:

        names a file .................... 0/221  missed
        SHORT CONTINUATION ............. 19/45   missed  = 42.2%

    "go on more", "yes, repoint both", "yes, file both" — no deictic pronoun
    for `_DEICTIC_RE`, no definite anaphora for `_ANAPHORA_RE`, and meaningless
    without the previous turn.

    After: 0/45 missed, file-naming unchanged at 0/221, total flagged over the
    whole corpus 836 → 892 (+3.5pp). That increase is the cost, and the
    detector's own docstring accepts the asymmetry: "a false positive only
    costs a skipped draft ... a false negative is exactly the failure the user
    hit".

D-2 · The OKF rescue asked the wrong question
    It fired whenever `find_relevant()` returned ANY doc, and `find_relevant`
    matches by keyword overlap — "is there related material?" rather than "is
    there enough to answer THIS?".

    Measured lifetime: rescues overrode the gate 746 times (674 session, 67
    OKF, 5 tool-loop) for 0 accepted drafts out of 1,132 audited.

    The rescue now requires COVERAGE: if the prompt names something distinctive
    and none of the retrieved material mentions it, retrieval did not reach the
    subject. Fail-closed — a prompt with nothing distinctive cannot be shown to
    be covered, so the gate stays shut.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

HOOK = (
    pathlib.Path(__file__).resolve().parents[1]
    / "src" / "llm_router" / "hooks" / "auto-route.py"
)


@pytest.fixture(scope="module")
def hook():
    spec = importlib.util.spec_from_file_location("auto_route_d1d2", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Doc:
    def __init__(self, title: str, body: str) -> None:
        self.title, self.body = title, body


# ── D-1 ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("prompt", [
    # Every one of these is a REAL prompt from the measured corpus.
    "go on more",
    "yes, repoint both",
    "yes, file both",
    # Shape taken from a real corpus prompt; the project name it carried is
    # removed — check_identity.py blocks private project names in this repo,
    # and the test asserts the SHAPE ("continue, remember to ...") not the name.
    "continue, remember to use the local model all along",
    # And the shapes the same class takes.
    "do them both",
    "push it",
    "fix it too",
    "again",
    "ok, go for it",
])
def test_a_bare_continuation_is_context_dependent(hook, prompt: str) -> None:
    assert hook._is_context_dependent(prompt) is True, prompt


@pytest.mark.parametrize("prompt", [
    "what is a monad?",
    "explain the difference between a list and a tuple",
    # NOT "write a regex that validates an email address" — the HOOK flags it,
    # even though S3b fixed exactly that case in `context_signal` this week.
    # The hook keeps its own copy of the detector ("Option B, keep in sync
    # manually"), so it never received the fix. Verified pre-existing: True
    # before this change and True after. That drift is its own finding and is
    # not silently repaired here.
])
def test_a_self_contained_prompt_is_not(hook, prompt: str) -> None:
    """Anti-vacuity. A detector that flags everything skips every draft, which
    is a different way of turning the feature off."""
    assert hook._is_context_dependent(prompt) is False, prompt


def test_the_word_mid_sentence_does_not_trigger_it(hook) -> None:
    """Anchored at the start on purpose.

    "continue" inside a sentence is ordinary English; opening with it is a
    reply to something. Without the anchor this class would swallow ordinary
    instructions.
    """
    assert hook._CONTINUATION_RE.match("the script should continue on error") is None
    # Asserted on the REGEX, not on `_is_context_dependent`. The full predicate
    # flags "the loop should continue until the tests pass" anyway — verified
    # pre-existing (True before this change and after), via the anaphora branch
    # on "the tests". Asserting False there would have failed for a reason
    # unrelated to what this test is about.
    assert hook._CONTINUATION_RE.match(
        "the loop should continue until the tests pass"
    ) is None


def test_the_length_gate_still_applies(hook) -> None:
    """A long prompt that merely opens with a continuation word carries its own
    subject and does not need the previous turn."""
    long_prompt = (
        "continue the migration by first draining the queue, then swapping the "
        "primary, then verifying replica lag stays under one second throughout"
    )
    assert len(long_prompt.split()) > 12
    # Again on the gate this change owns. The full predicate flags this prompt
    # via `_CONTEXT_DEP_RE` ("migration"/"queue"/"replica"), pre-existing and
    # unrelated — but the D-1 branch must not be what fires.
    assert not (len(long_prompt.split()) <= 12
                and hook._CONTINUATION_RE.match(long_prompt))


# ── D-2 ───────────────────────────────────────────────────────────────────


def test_retrieved_but_uncovered_does_not_rescue(hook) -> None:
    """The measured failure: related material, no answer.

    The prompt MUST carry a distinctive token, or this exercises the
    `if not tokens` guard instead of the coverage comparison and passes for the
    wrong reason. The first version of this test used "does agenticgraphs
    accept an injected runner" — one lowercase word, no token — so mutating the
    coverage line to `return True` left it GREEN. Assert the reason, not the
    boolean.
    """
    prompt = "is TokenManager.refresh_token thread safe"
    assert hook._DISTINCTIVE_RE.findall(prompt), (
        "premise: the prompt must have something distinctive to match on, or "
        "this test does not reach the coverage comparison"
    )
    docs = [_Doc("AGR overview", "graphs, nodes and edges — nothing about auth")]
    assert hook._okf_covers_prompt(prompt, docs) is False


def test_the_coverage_comparison_is_what_decides(hook) -> None:
    """Same prompt, same shape of doc — only the CONTENT differs.

    Isolates the comparison: both calls pass the `if not tokens` guard, so the
    only thing that can change the answer is whether the material mentions the
    subject.
    """
    prompt = "is TokenManager.refresh_token thread safe"
    assert hook._okf_covers_prompt(
        prompt, [_Doc("auth", "TokenManager refreshes tokens")]
    ) is True
    assert hook._okf_covers_prompt(
        prompt, [_Doc("auth", "queues and workers")]
    ) is False


@pytest.mark.parametrize("prompt, doc", [
    ("what does _write_source_concept do",
     _Doc("okf.py", "_write_source_concept writes a doc")),
    ("fix src/llm_router/router.py",
     _Doc("router", "src/llm_router/router.py is the core")),
    ("is TokenManager thread safe",
     _Doc("auth", "TokenManager refreshes tokens")),
])
def test_covered_material_does_rescue(hook, prompt: str, doc) -> None:
    """Anti-vacuity: a coverage check that never passes disables the rescue
    entirely, which is not what was asked for."""
    assert hook._okf_covers_prompt(prompt, [doc]) is True


def test_nothing_distinctive_fails_closed(hook) -> None:
    """A prompt with no identifier to match on cannot be SHOWN to be covered.

    The gate stays shut. At 0 accepted drafts in 1,132, the draft being skipped
    has no demonstrated value to lose.
    """
    assert hook._okf_covers_prompt("what is a monad", [_Doc("fp", "monads")]) is False


def test_no_docs_is_not_coverage(hook) -> None:
    assert hook._okf_covers_prompt("fix router.py", []) is False


def test_a_malformed_doc_does_not_break_the_hook(hook) -> None:
    """Fail-open on shape, fail-closed on the decision: a retrieval object
    without title/body returns False rather than raising inside the hook."""
    class _Bad:
        @property
        def title(self):
            raise RuntimeError("boom")

    assert hook._okf_covers_prompt("fix router.py", [_Bad()]) is False


def test_the_coverage_check_is_wired_at_the_rescue(hook) -> None:
    """The predicate being correct is not the same as it being CALLED.

    Dropping the call site left every other test in this file green — the same
    weakness that let an AST-only assertion pass for F-1. AST, so a comment
    carrying the name cannot satisfy it.
    """
    import ast

    tree = ast.parse(HOOK.read_text(encoding="utf-8"))
    called = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "_okf_covers_prompt"
    ]
    assert called, (
        "_okf_covers_prompt has no call site — the OKF rescue fires on mere "
        "retrieval again, which is D-2 unfixed"
    )

    # And it must GUARD the rescue, not merely be mentioned: the call has to sit
    # inside an `if` whose body clears _okf_docs.
    guarded = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        if not any(
            isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
            and c.func.id == "_okf_covers_prompt"
            for c in ast.walk(node.test)
        ):
            continue
        for stmt in ast.walk(node):
            if (isinstance(stmt, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "_okf_docs"
                            for t in stmt.targets)):
                guarded = True
    assert guarded, (
        "the coverage check is called but does not clear _okf_docs, so the "
        "rescue proceeds regardless"
    )
