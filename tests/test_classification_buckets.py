"""Audit §2.6: classifier bucket accuracy for the three cited misroutes.

  * "Coordinate three agents to build, test, and deploy" → coordination
  * "Design a distributed rate limiter"                  → code (or analyze)
  * "Generate a haiku about autumn"                      → generate

These are scored by the heuristic pattern layer (score_categories). The test
asserts the *winning* bucket, tolerating the code/analyze ambiguity for design.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "src" / "llm_router" / "hooks" / "auto-route.py"


@pytest.fixture(scope="module")
def ar():
    cached = sys.modules.get("auto_route_buckets")
    if cached:
        return cached
    spec = importlib.util.spec_from_file_location("auto_route_buckets", HOOK)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["auto_route_buckets"] = mod
    spec.loader.exec_module(mod)
    return mod


def _top(ar, text: str) -> str:
    scores = ar.score_categories(text)
    return max(scores.items(), key=lambda kv: kv[1])[0]


def test_coordination_wins_for_multi_agent(ar):
    assert _top(ar, "Coordinate three agents to build, test, and deploy") == "coordination"


def test_design_task_is_code_or_analyze_not_research(ar):
    top = _top(ar, "Design a distributed rate limiter")
    assert top in ("code", "analyze"), f"design task misrouted to {top!r}"


def test_generate_haiku_is_generate(ar):
    assert _top(ar, "Generate a haiku about autumn") == "generate"


def test_generate_poem_still_generate(ar):
    # guard: don't regress the existing creative-writing detection
    assert _top(ar, "Write a poem about the sea") == "generate"


def test_plain_query_unaffected(ar):
    # guard: a simple factual question must not get pulled into a work bucket
    scores = ar.score_categories("What is the capital of France?")
    assert scores.get("code", 0) == 0 and scores.get("generate", 0) == 0
