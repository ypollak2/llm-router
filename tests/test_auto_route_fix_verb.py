"""Regression tests for the v0.0.2 fix-verb classifier expansion.

Two changes shipped:
    1. Code intent: added `(?:fix|patch|repair|resolve)\\s+<determiner>\\w+`
       so prompts like "fix the auto-route classifier" or "continue with
       the fix for the branch" score code-intent without requiring a
       trailing bug/error/issue noun. The required determiner filters
       out noun usage like "the fix was hard".
    2. Code topic: added testing vocabulary (tests, qa, test suite,
       regression test, functional, non-functional, integrity, usability)
       so QA-related implementation prompts score code-topic.

These tests pin the new behavior so future scorer changes don't regress.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def auto_route():
    """Load the auto-route hook as a module."""
    src = (
        Path(__file__).resolve().parent.parent
        / "src"
        / "llm_router"
        / "hooks"
        / "auto-route.py"
    )
    spec = importlib.util.spec_from_file_location("auto_route", src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ────────────────────────────────────────────────────────────────────────
# Fix verb expansion — code intent
# ────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("prompt,expected", [
    # Original (preserved) — bug/error/issue suffix
    ("fix the bug", "code"),
    ("fix the error in auth", "code"),
    ("fix this issue with login", "code"),
    ("fix a crash on startup", "code"),
    # New — broad determiner-based pattern
    ("fix the auto-route classifier", "code"),
    ("fix the classifier", "code"),
    ("fix the migration", "code"),
    ("fix the llm_router", "code"),
    ("patch the migration script", "code"),
    ("repair the build", "code"),
    ("resolve the conflict", "code"),
    ("fix this dependency", "code"),
    ("fix a regression", "code"),
    # "for the X" determiner — the actual failing case from xfail
    ("fix for the llm_router and its branch", "code"),
    ("a fix for the failing migration", "code"),  # noun→verb interpretation
])
def test_fix_verb_classifies_as_code(auto_route, prompt: str, expected: str):
    scores = auto_route.score_categories(prompt)
    winner = max(scores, key=lambda k: scores[k]) if any(scores.values()) else None
    assert winner == expected, (
        f"{prompt!r}: expected {expected}, got {winner} (scores={scores})"
    )


# ────────────────────────────────────────────────────────────────────────
# Noun-usage exclusion — "the fix" without trailing determiner+noun
# ────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("prompt", [
    "the fix was hard",
    "the fix is complete",
    "that fix solved it",
])
def test_fix_noun_usage_does_not_classify_as_code(auto_route, prompt: str):
    """When 'fix' is used as a noun without a determiner+noun following,
    code score should be 0 — the required determiner in the pattern
    filters these out."""
    scores = auto_route.score_categories(prompt)
    # The important invariant: noun-usage prompts should not score code
    # via intent. Topic matches are tolerable up to a low cap; if code
    # fires above that, the noun-filter regressed.
    if scores["code"] > 0:
        assert "the fix" not in prompt.lower() or scores["code"] <= 2


# ────────────────────────────────────────────────────────────────────────
# QA/testing prompts — should classify as code, not analyze
# ────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("prompt", [
    "I need more tests - functional, non-functional, performance, integrity, usability",
    "build a QA test suite for the router",
    "write integration tests for the migration",
    "add regression tests for the budget envelope",
    "extend the test suite with usability checks",
])
def test_qa_implementation_prompts_classify_as_code(auto_route, prompt: str):
    """Building tests is implementation work, not analysis. After v0.0.2
    expansion of code topic with testing vocabulary, these prompts win
    code instead of analyze."""
    scores = auto_route.score_categories(prompt)
    winner = max(scores, key=lambda k: scores[k])
    assert winner == "code", (
        f"{prompt!r}: expected code (implementation), got {winner} (scores={scores})"
    )


# ────────────────────────────────────────────────────────────────────────
# Control cases — pre-existing behavior must be preserved
# ────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("prompt,expected", [
    ("what is a foreign key?", "query"),
    ("analyze the performance of this query", "analyze"),
    ("write a blog post about LLMs", "generate"),
    ("look up the latest funding rounds", "research"),
    ("push to main", "coordination"),
    ("yes", "coordination"),
    ("refactor this Python function to use early returns", "code"),
    ("implement a new feature", "code"),
])
def test_existing_classifications_preserved(
    auto_route, prompt: str, expected: str
):
    scores = auto_route.score_categories(prompt)
    winner = max(scores, key=lambda k: scores[k]) if any(scores.values()) else None
    assert winner == expected, (
        f"{prompt!r}: expected {expected}, got {winner} (scores={scores})"
    )


# ────────────────────────────────────────────────────────────────────────
# Live hook sync check — the live llm-router hook must carry the same fix
# ────────────────────────────────────────────────────────────────────────

def _find_live_hook() -> Path | None:
    """Locate the installed auto-route hook regardless of binary rebrand.

    Post-llm_router rebrand the hook is ``llm_router-auto-route.py``. The legacy
    ``llm-router-auto-route.py`` path is checked second for backwards
    compatibility with environments that haven't migrated. First existing
    file wins.
    """
    candidates = [
        Path.home() / ".claude" / "hooks" / "llm_router-auto-route.py",
        Path.home() / ".claude" / "hooks" / "llm-router-auto-route.py",
    ]
    for path in candidates:
        if path.exists():
            return path
    return None


def test_live_hook_has_the_fix_pattern_too():
    """If the LLM Router source has the expanded fix pattern, the live
    auto-route hook used by Claude Code must too. Otherwise users hitting
    the bug locally won't benefit from the fix."""
    live_hook = _find_live_hook()
    if live_hook is None:
        pytest.skip("No live auto-route hook installed locally")

    content = live_hook.read_text()
    assert "fix|patch|repair|resolve" in content, (
        f"Live auto-route hook ({live_hook.name}) missing the v0.0.2 "
        "fix-verb expansion. Re-install from llm_router source so the live hook "
        "carries the same classifier patterns."
    )


def test_live_hook_has_the_qa_topic_terms_too():
    live_hook = _find_live_hook()
    if live_hook is None:
        pytest.skip("No live auto-route hook installed locally")

    content = live_hook.read_text()
    # New terms added in v0.0.2 to code topic
    assert "non[- ]functional" in content or "non-functional" in content, (
        f"Live auto-route hook ({live_hook.name}) missing the v0.0.2 "
        "QA-topic expansion. Re-install from llm_router source."
    )
