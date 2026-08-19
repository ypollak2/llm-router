"""Regression test for the no-hint complexity heuristic in router.py.

A session on 2026-06-06 showed every single one of the day's 31
routed prompts classifying as ``moderate`` even though 80% of the
user-facing prompts were under 150 chars. Root cause: when callers
don't pass ``complexity_hint``, the router auto-detects from the
wrapped prompt length using a 300-char ``simple/moderate`` boundary —
and wrappers (``llm_query`` callers, Claude Code) almost always cross
that line. So Haiku/Flash routing never fired and Sonnet/GPT-4o ate
the spend.

The fix raises the simple boundary to 600 chars and pulls the complex
boundary down to 2000. This test pins those numbers so a future
refactor can't silently regress to the old ratio.
"""

from __future__ import annotations

import pytest

from unittest.mock import MagicMock

from llm_router.router import _resolve_profile
from llm_router.types import Complexity, RoutingProfile


@pytest.mark.parametrize("length,expected", [
    (1, Complexity.SIMPLE),
    (100, Complexity.SIMPLE),
    (200, Complexity.SIMPLE),
    (500, Complexity.SIMPLE),
    (599, Complexity.SIMPLE),
    (600, Complexity.MODERATE),
    (1000, Complexity.MODERATE),
    (1999, Complexity.MODERATE),
    (2000, Complexity.MODERATE),
    (2001, Complexity.COMPLEX),
    (5000, Complexity.COMPLEX),
])
def test_no_hint_heuristic_boundaries(length, expected):
    """Verify the < 600 / [600,2000] / > 2000 partition of complexity."""
    prompt = "x" * length
    config = MagicMock(llm_router_profile=RoutingProfile.BALANCED)
    _profile, complexity, _thinking = _resolve_profile(
        profile=None,
        complexity_hint=None,
        classification_data=None,
        prompt=prompt,
        model_override=None,
        config=config,
    )
    assert complexity == expected, (
        f"len={length}: expected {expected}, got {complexity}"
    )


def test_complexity_hint_wins_over_heuristic():
    """When the caller supplies a hint, the auto-heuristic must NOT
    override it — that's the whole point of the hint."""
    config = MagicMock(llm_router_profile=RoutingProfile.BALANCED)
    long_prompt = "x" * 5000  # would be COMPLEX by length alone
    _profile, complexity, _thinking = _resolve_profile(
        profile=None,
        complexity_hint="simple",  # caller explicitly says simple
        classification_data=None,
        prompt=long_prompt,
        model_override=None,
        config=config,
    )
    assert complexity == Complexity.SIMPLE


def test_classification_data_complexity_used_when_no_hint():
    """When classification_data carries a complexity (e.g. from the
    UserPromptSubmit hook), the router must honor it rather than
    falling through to the length heuristic."""
    config = MagicMock(llm_router_profile=RoutingProfile.BALANCED)
    _profile, complexity, _thinking = _resolve_profile(
        profile=None,
        complexity_hint=None,
        classification_data={"complexity": "simple"},
        prompt="x" * 5000,
        model_override=None,
        config=config,
    )
    assert complexity == Complexity.SIMPLE
