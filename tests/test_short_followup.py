"""Short follow-up detection — extends the existing CONTINUATION_RE
to catch multi-word conversational continuations like
"ok do that" / "yes, continue with 3" / "now do the next one".

False-positive guard: a genuine new task with NO conversational prefix
must still route, regardless of length. The bypass only triggers on
the *acknowledgment-prefix + short* pair.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def auto_route():
    spec = importlib.util.spec_from_file_location(
        "_auto_route_short_followup",
        Path(__file__).resolve().parents[1]
        / "src" / "llm_router" / "hooks" / "auto-route.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ── True positives — conversational follow-ups bypass routing ─────────


@pytest.mark.parametrize("prompt", [
    "ok do it",
    "yes, continue with 3",
    "yeah continue",
    "now do the next one",
    "next step please",
    "ok then add the last one",
    "let's tackle 3",
    "go for it",
    "continue with the rest",
    "more like this",
    "keep going",
    "alright, run it",
    "cool, now make a test",
    "and then push it",
])
def test_short_followups_bypass(auto_route, prompt):
    assert auto_route._is_short_followup(prompt), (
        f"prompt should be flagged short-followup: {prompt!r}"
    )
    assert auto_route._is_continuation(prompt), (
        f"_is_continuation should return True for {prompt!r}"
    )


# ── False-positive guards — genuine new tasks still route ─────────────


@pytest.mark.parametrize("prompt", [
    "Implement a new MCP tool that wraps litellm streaming responses",
    "What is the time complexity of merge sort?",
    "Show me a Python example of decorators with arguments",
    "Refactor the cost.py module to extract _safe_migrate into its own file",
    # Sub-80 char prompt but no acknowledgment prefix — still a new task.
    "Write a quick regex for email validation",
])
def test_genuine_tasks_still_route(auto_route, prompt):
    assert not auto_route._is_short_followup(prompt), (
        f"prompt mis-flagged as short-followup: {prompt!r}"
    )


def test_long_acknowledgment_doesnt_bypass(auto_route):
    """A prompt that starts with 'ok' but contains a 200-char detailed
    request is not a follow-up — the length guard is doing real work."""
    long = (
        "ok, let me explain what I actually need: a full audit of the "
        "router's classifier behaviour with edge cases for every task "
        "type, including a markdown report I can paste into a PR with "
        "specific lines for follow-up engineers."
    )
    assert not auto_route._is_short_followup(long)


def test_empty_string_does_not_match(auto_route):
    assert not auto_route._is_short_followup("")
    assert not auto_route._is_short_followup("   ")
