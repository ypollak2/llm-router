"""P6 — the canonical context-dependent signal shared by advisory + enforcement.

A context-dependent prompt is exempted from hard-blocking (enforce-route.py) and
gets the advisory note (auto-route.py) — both now read this one function, so a
prompt flagged by one is guaranteed handled by the other.
"""
from __future__ import annotations

import pytest

from llm_router.context_signal import is_context_dependent

CONTEXT_DEPENDENT = [
    "run the tests",
    "fix the bug in the parser",
    "why does the dashboard not update",
    "restart the server",
    "stop the rest",
    "run it",
    "what does this do",
    "as we discussed earlier",
    "in the previous session",
    "check /Users/me/proj/app.py",
    "look at src/llm_router/server.py",
    "delete the merged branch",
]

ROUTABLE = [
    "what is the capital of France",
    "explain how SQLite WAL mode works",
    "write a regex for email addresses",
    "translate 'good morning' into Spanish",
    "summarize the theory of relativity in two sentences",
    "what are the five love languages",
]


@pytest.mark.parametrize("prompt", CONTEXT_DEPENDENT)
def test_context_dependent_prompts_detected(prompt):
    assert is_context_dependent(prompt) is True


@pytest.mark.parametrize("prompt", ROUTABLE)
def test_routable_prompts_not_flagged(prompt):
    # these are self-contained knowledge/generation tasks a stateless model CAN do
    assert is_context_dependent(prompt) is False


def test_empty_and_whitespace_safe():
    assert is_context_dependent("") is False
    assert is_context_dependent("   ") is False


def test_deictic_word_count_cutoff_is_exactly_twelve():
    """The short-deictic fallback fires for prompts of ≤12 words only. Pin the
    boundary exactly (neither <12 nor ≤13): a 12-word deictic prompt IS
    context-dependent, a 13-word one is NOT. Both are decided by the word-count
    branch (they don't match the strong context regex)."""
    twelve = "make it a little bit shorter and cleaner for the final version"
    thirteen = "make it a little bit shorter and cleaner for the final polished version"
    assert len(twelve.split()) == 12 and len(thirteen.split()) == 13
    assert is_context_dependent(twelve) is True     # 12 ≤ 12
    assert is_context_dependent(thirteen) is False   # 13 > 12


def test_signal_does_not_over_exempt_routable_generative_prompts():
    """Regression guard for the reverted CTX_DEP_EXEMPT: the signal errs toward
    True on prompts that are genuinely routable, which is fine for the advisory
    but must NOT be used as an enforcement exemption — hard enforcement must
    still cover them. So this signal is advisory-only, never an enforce-route
    exemption.

    The example moved in S3b (2026-09-23). It used to be 'Generate a regex that
    validates emails', flagged because the relative pronoun in 'that validates'
    was read as a deixis; S3b masks a relative `that`/`which` before the deixis
    check and that prompt now correctly reads False.

    The over-breadth it guarded against is NOT gone — it just has a different
    cause. 'Build a parser that handles nested quotes' is still flagged, and
    `_CONTEXT_DEP_RE` matches it on the bare word **'Build'**. That is a
    stronger example of the same defect than the one it replaces: no pronoun,
    no deixis, nothing pointing at the user's state — one verb in a wordlist.
    If this ever starts reading False, do not delete the assertion; find the
    next routable prompt the signal over-flags, because the claim being
    defended is about the signal's suitability as an exemption, not about any
    one prompt.
    """
    # Premise: the signal still over-flags a genuinely routable task.
    assert is_context_dependent("Build a parser that handles nested quotes") is True
    # And S3b's fix is real — the relative-pronoun false positive is gone.
    assert is_context_dependent("Generate a regex that validates emails") is False
    src = (
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "src" / "llm_router" / "hooks" / "enforce-route.py"
    ).read_text()
    assert "CTX_DEP_EXEMPT" not in src, "enforce-route must not exempt on this over-broad signal"
