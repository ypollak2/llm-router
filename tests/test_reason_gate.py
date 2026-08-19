# SPDX-License-Identifier: MIT
"""Tests for the calibrated reason-gate — including the regression guarantee
that it reproduces the old `_COMPLEXITY_DEEP` regex decision by default."""

from __future__ import annotations

import json

import pytest

from llm_router import reason_gate as rg
from llm_router.classify import _COMPLEXITY_DEEP, complexity_for, HOOK_POLICY
from llm_router.types import Complexity, Subject


@pytest.fixture(autouse=True)
def _clear_cache():
    rg._load_gate_params.cache_clear()
    yield
    rg._load_gate_params.cache_clear()


# ── default behaviour reproduces the regex (regression safety) ────────────────

_DEEP_PROMPTS = [
    "prove that the sum is even",
    "walk me through the reasoning step by step",
    "derive from first principles the equation",
    "root-cause analysis of the outage",
]

_NON_DEEP_PROMPTS = [
    "what time is it",
    "write a blog post about cats",
    "add a login button",
]


@pytest.mark.parametrize("p", _DEEP_PROMPTS)
def test_deep_keyword_triggers_reasoning(p):
    assert _COMPLEXITY_DEEP.search(p)          # precondition: regex would fire
    assert rg.needs_reasoning(p) is True       # gate agrees


@pytest.mark.parametrize("p", _NON_DEEP_PROMPTS)
def test_non_deep_does_not_trigger_by_default(p):
    assert not _COMPLEXITY_DEEP.search(p)
    assert rg.needs_reasoning(p) is False


def test_complexity_for_still_returns_deep_reasoning():
    # The wired-in path through classify._complexity must still surface DEEP.
    assert complexity_for(
        "prove that this holds by induction", task_type="query", policy=HOOK_POLICY
    ) is Complexity.DEEP_REASONING


# ── features / scoring ────────────────────────────────────────────────────────


def test_math_density_feature():
    dense = rg.gate("∑ x^2 = 3x + 4 = 2 ≤ 5 ≥ 1 = 0")
    prose = rg.gate("tell me a story about a dog")
    assert dense.features["math"] > prose.features["math"]


def test_code_fence_pushes_away_from_reasoning():
    # A deep keyword still wins, but the code feature lowers the score.
    with_fence = rg.gate("reason about this\n```\ncode\n```")
    without = rg.gate("reason about this")
    assert with_fence.score < without.score


def test_subject_boost_can_lift_score():
    base = rg.gate("compare the two options carefully")
    boosted = rg.gate("compare the two options carefully", subject=Subject.MATH.value)
    assert boosted.score >= base.score


# ── artifact-backed params ────────────────────────────────────────────────────


def test_artifact_overrides_weights(monkeypatch, tmp_path):
    # A permissive artifact makes math density alone sufficient.
    art = {
        "embedding_model": "x", "dim": 4, "task_type": {}, "subject": {},
        "reason_gate": {"weights": {"bias": -0.5, "math": 5.0}, "threshold": 0.5},
    }
    path = tmp_path / "semantic_centroids.json"
    path.write_text(json.dumps(art), encoding="utf-8")
    monkeypatch.setenv("LLM_ROUTER_SEMANTIC_CENTROIDS", str(path))
    rg._load_gate_params.cache_clear()

    weights, threshold = rg._params()
    assert weights["math"] == 5.0
    assert threshold == 0.5
    # A math-dense prompt with no deep keyword now trips the gate.
    assert rg.needs_reasoning("x^2 = 3 + 4 = 5 ≤ 9 ≥ 2 = 0 ∑ ∫") is True


def test_bad_artifact_falls_back_to_defaults(monkeypatch, tmp_path):
    path = tmp_path / "semantic_centroids.json"
    path.write_text("{ not json", encoding="utf-8")
    monkeypatch.setenv("LLM_ROUTER_SEMANTIC_CENTROIDS", str(path))
    rg._load_gate_params.cache_clear()
    weights, threshold = rg._params()
    assert weights == rg._DEFAULT_WEIGHTS
    assert threshold == rg._DEFAULT_THRESHOLD
