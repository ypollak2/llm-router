"""Regression tests for the heuristic classifier's coordination signals.

These tests lock in the fix for a class of false positives where
substantive prompts (multi-sentence, > 150 chars) containing common
English words like "continue", "test", "verify" were being scored as
``coordination`` and routed to a tiny Ollama model that hallucinated
unrelated answers.

The fix has two parts:

1. ``COORDINATION_MAX_LEN`` — coordination score is zeroed when the
   prompt is longer than the threshold. Long prompts cannot be
   coordination regardless of which short coordination words appear in
   them.

2. The ``coordination/intent`` regex was trimmed to remove ambiguous
   English (continue, proceed, verify, check, test, update, is, are,
   does, please, thanks, execute, run, build, compile). What remains is
   git/deploy verbs (push, pull, deploy, release, publish, commit,
   merge, sync, fetch, rebase) plus short acks (yes, ok, y, n,
   go ahead).

Loading auto-route.py is awkward because of the hyphen in the filename;
we use importlib.util.spec_from_file_location to import it as a module.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def _load_auto_route():
    cached = sys.modules.get("auto_route_under_test_signals")
    if cached is not None:
        return cached
    path = (
        Path(__file__).resolve().parents[1]
        / "src" / "llm_router" / "hooks" / "auto-route.py"
    )
    spec = importlib.util.spec_from_file_location(
        "auto_route_under_test_signals", path
    )
    assert spec and spec.loader, f"Could not load spec for {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules["auto_route_under_test_signals"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def auto_route():
    return _load_auto_route()


# ── Length gate ──────────────────────────────────────────────────────────────


class TestCoordinationLengthGate:
    """``score_categories`` zeroes coordination for prompts above the threshold."""

    def test_threshold_constant_is_exposed(self, auto_route):
        # The threshold is referenced by tests + tooling — must stay public.
        assert isinstance(auto_route.COORDINATION_MAX_LEN, int)
        assert auto_route.COORDINATION_MAX_LEN >= 100

    def test_long_prompt_with_coordination_words_scores_zero(self, auto_route):
        # Use only coordination-adjacent words but make the prompt long.
        text = "yes " + ("push pull merge commit " * 20)
        assert len(text) > auto_route.COORDINATION_MAX_LEN
        scores = auto_route.score_categories(text)
        assert scores["coordination"] == 0

    def test_short_prompt_with_coordination_words_scores_nonzero(self, auto_route):
        text = "yes push to main and merge"
        assert len(text) <= auto_route.COORDINATION_MAX_LEN
        scores = auto_route.score_categories(text)
        assert scores["coordination"] > 0


# ── Real-world misfire fixtures (these were the bugs) ────────────────────────


class TestPreviouslyMisfiredPrompts:
    """Each of these used to win coordination by ≥ 2x; they must not anymore."""

    def test_routerarena_continuation_prompt_does_not_score_coordination(
        self, auto_route
    ):
        text = (
            "Continue RouterArena optimization for PR #132. Branch reset to "
            "baseline 065cca5 after Lever #3 was rejected as test-set leakage. "
            "Read docs/ROUTERARENA_IMPROVEMENT_PLAN.md and start with Tier 1 "
            "(free wins). Before any submission, run uv run python "
            "scripts/check_submission_integrity.py."
        )
        scores = auto_route.score_categories(text)
        assert scores["coordination"] == 0, (
            f"Long substantive prompt still scoring coordination: {scores}"
        )

    def test_llm_router_fix_request_does_not_win_coordination(self, auto_route):
        # Short prompt — length gate does NOT apply, but the trimmed
        # intent regex no longer matches "continue", so coordination is
        # at most tied with substantive categories rather than dominating.
        text = "I want to continue with the fix for the llm-router and its branch"
        scores = auto_route.score_categories(text)
        winner = max(scores, key=lambda k: scores[k])
        assert winner != "coordination", (
            f"Substantive fix request still winning coordination: {scores}"
        )

    def test_long_prompt_with_continue_and_test_does_not_score_coordination(
        self, auto_route
    ):
        text = (
            "I want to continue working on the test suite and verify "
            "the integration tests pass before we push the branch. "
            "Please check whether the build is green on CI and update "
            "the docs if anything changed in the public API."
        )
        scores = auto_route.score_categories(text)
        assert scores["coordination"] == 0


# ── Legitimate coordination must still win ───────────────────────────────────


class TestLegitimateCoordinationStillWins:
    """Short git/deploy prompts must still be classified as coordination."""

    @pytest.mark.parametrize(
        "text",
        [
            "push to main",
            "merge after CI passes",
            "rebase onto origin/main",
            "deploy to staging",
            "yes go ahead",
            "ok proceed",  # "proceed" no longer in intent but "ok" is
            "publish the release",
            "fetch and pull",
        ],
    )
    def test_short_git_prompt_wins_coordination(self, auto_route, text):
        scores = auto_route.score_categories(text)
        winner = max(scores, key=lambda k: scores[k])
        assert winner == "coordination", (
            f"Expected coordination, got {winner} for {text!r}: {scores}"
        )


# ── Substantive prompts must still classify correctly ────────────────────────


class TestSubstantiveStillClassifiesCorrectly:
    """Code/analyze/generate prompts must not regress after the fix."""

    def test_implement_classifies_as_code(self, auto_route):
        scores = auto_route.score_categories(
            "Implement a function that parses JSON safely."
        )
        winner = max(scores, key=lambda k: scores[k])
        assert winner == "code", scores

    def test_refactor_classifies_as_code(self, auto_route):
        scores = auto_route.score_categories(
            "Refactor the cache module to use SHA256 keys."
        )
        winner = max(scores, key=lambda k: scores[k])
        assert winner == "code", scores

    def test_explain_classifies_substantive(self, auto_route):
        # Should be query or analyze, but definitely not coordination.
        scores = auto_route.score_categories(
            "Explain how the cache eviction policy works."
        )
        winner = max(scores, key=lambda k: scores[k])
        assert winner != "coordination", scores


# ── End-to-end classify_prompt sanity (no Ollama / API calls) ────────────────


class TestClassifyPromptIntegration:
    """Exercise the full chain on the previously-misfired prompt.

    ``classify_prompt`` may fall through to Ollama or the cheap API
    layer. We set the env var that disables those so the test runs
    offline and is deterministic.
    """

    @pytest.fixture(autouse=True)
    def disable_llm_classifiers(self, auto_route, monkeypatch):
        monkeypatch.setattr(auto_route, "DISABLE_LLM_CLASSIFIERS", True)

    def test_routerarena_prompt_routes_substantive_not_coordination(self, auto_route):
        text = (
            "Continue RouterArena optimization for PR #132. Branch reset to "
            "baseline 065cca5 after Lever #3 was rejected as test-set leakage. "
            "Read docs/ROUTERARENA_IMPROVEMENT_PLAN.md and start with Tier 1 "
            "(free wins). Before any submission, run uv run python "
            "scripts/check_submission_integrity.py."
        )
        result = auto_route.classify_prompt(text)
        assert result is not None
        assert result["task_type"] != "coordination", result

    def test_short_continue_no_longer_returns_coordination(self, auto_route):
        # Bare "continue" used to return coordination/moderate. After the
        # trim it is too short to classify; classify_prompt returns None
        # (filtered by the < 8 chars guard) and the downstream skip logic
        # handles it. This is the correct behavior — a bare ack does not
        # need a route hint.
        result = auto_route.classify_prompt("continue")
        # 8 chars exactly — at the boundary. Either None or non-coordination is OK.
        if result is not None:
            assert result["task_type"] != "coordination", result
