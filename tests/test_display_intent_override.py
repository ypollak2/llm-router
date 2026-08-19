"""Tests for the v6.12 display-intent override in auto-route.py.

Locks in the fix for the "LLM Router stuck" perception documented in
``STUCK_PATTERNS_ANALYSIS.md`` §2 mode 1. Before this guard, short prompts
like "show me the report" issued after code-heavy turns would inherit a
``code`` classification via ``_is_short_code_followup`` and route to
``llm_code`` — an external LLM that cannot read local files. The override
re-routes such prompts to ``llm_query`` regardless of inherited context.

These tests are pure regex tests against ``_DISPLAY_INTENT_RE`` (and the
companion length cap). They do not exercise the full hook main() because
the integration site is a 5-line branch that is structurally trivial; the
risk lives in regex precision (false positives on real code requests, false
negatives on legitimate display intents).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def _load_auto_route():
    cached = sys.modules.get("auto_route_under_test_display")
    if cached is not None:
        return cached
    path = (
        Path(__file__).resolve().parents[1]
        / "src" / "llm_router" / "hooks" / "auto-route.py"
    )
    spec = importlib.util.spec_from_file_location(
        "auto_route_under_test_display", path
    )
    assert spec and spec.loader, f"Could not load spec for {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules["auto_route_under_test_display"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def auto_route():
    return _load_auto_route()


# ── Constants are exposed ───────────────────────────────────────────────────


def test_display_intent_regex_is_exposed(auto_route):
    assert hasattr(auto_route, "_DISPLAY_INTENT_RE")
    assert hasattr(auto_route, "_DISPLAY_INTENT_MAX_CHARS")
    assert isinstance(auto_route._DISPLAY_INTENT_MAX_CHARS, int)
    assert auto_route._DISPLAY_INTENT_MAX_CHARS >= 50


# ── Positive cases — the override SHOULD fire ───────────────────────────────


@pytest.mark.parametrize("prompt", [
    # The exact prompt from the documented incident
    "show me the report",
    # Variations on common display targets
    "show me the file",
    "show me my notes",
    "show me the diff",
    "show me the output",
    "show me the log",
    "show the summary",
    "display the table",
    "display the chart",
    "view the changes",
    "view the readme",
    "read the spec",
    "read it",
    "see the data",
    "cat the file",
    "print the results",
    "list all files",
    "list the changes",
    "open the readme",
    # Explicit filenames with known extensions
    "show me CHANGELOG.md",
    "read CLAUDE.md",
    "cat config.yaml",
    "open settings.json",
    # Leading whitespace tolerated
    "   show me the report",
])
def test_display_intent_matches(auto_route, prompt):
    assert auto_route._DISPLAY_INTENT_RE.match(prompt), (
        f"Expected display-intent match for: {prompt!r}"
    )


# ── Negative cases — the override must NOT fire ─────────────────────────────


@pytest.mark.parametrize("prompt", [
    # Code-generation requests that happen to start with "show"
    "show me a function that parses dates",
    "show me how to implement a binary search",
    "show me an example of a Python decorator",
    # Genuine question prompts (these are handled by other paths, not display)
    "what does the report say",
    "why is this slow",
    "how do I run the tests",
    # Code work that should remain code-classified
    "refactor the auth module",
    "fix the bug in lineage_store.py",
    "add a test for the new function",
    # Display-adjacent verbs without targets that match our list
    "show me how to fix this",  # no display target after "show me"
    "view source code please",  # "view source" but no target keyword
    # Continuations / acks (handled by their own paths)
    "ok",
    "yes do it",
    "continue",
])
def test_display_intent_does_not_match(auto_route, prompt):
    assert not auto_route._DISPLAY_INTENT_RE.match(prompt), (
        f"Unexpected display-intent match for: {prompt!r}"
    )


# ── Length cap ──────────────────────────────────────────────────────────────


def test_long_prompt_with_display_verb_exceeds_cap(auto_route):
    """A long prompt is not a display request even if it starts with 'show me'."""
    prompt = "show me the report " + ("with detailed analysis of " * 20)
    assert len(prompt) > auto_route._DISPLAY_INTENT_MAX_CHARS
    # The regex itself may still match; the call site combines it with the
    # length check. Document the contract: callers must apply BOTH conditions.
    # This test asserts the cap is meaningful (not 1 million).
    assert auto_route._DISPLAY_INTENT_MAX_CHARS <= 200


# ── Integration: the override branch is wired in the main classify block ────


def test_override_branch_present_in_source(auto_route):
    """Smoke check: the call site references the regex by name.

    Catches a regression where someone removes the override branch but
    leaves the constants dangling.
    """
    source_path = (
        Path(__file__).resolve().parents[1]
        / "src" / "llm_router" / "hooks" / "auto-route.py"
    )
    source = source_path.read_text()
    assert "_DISPLAY_INTENT_RE.match" in source, (
        "Override branch missing — _DISPLAY_INTENT_RE.match(prompt) not "
        "found in call site. See STUCK_PATTERNS_ANALYSIS.md §5 fix A."
    )
    assert "intent-override-display" in source, (
        "Override method tag missing — telemetry will not distinguish "
        "this fix from other classifications."
    )
