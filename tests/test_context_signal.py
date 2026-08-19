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
    True (deictic 'that'/'it' in short prompts), which is fine for the advisory
    but must NOT be used as an enforcement exemption — 'Generate a regex that
    validates emails' is context-flagged yet is a genuinely routable task that
    hard enforcement must still cover. So this signal is advisory-only, never an
    enforce-route exemption."""
    assert is_context_dependent("Generate a regex that validates emails") is True  # errs toward True
    src = (
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "src" / "llm_router" / "hooks" / "enforce-route.py"
    ).read_text()
    assert "CTX_DEP_EXEMPT" not in src, "enforce-route must not exempt on this over-broad signal"
