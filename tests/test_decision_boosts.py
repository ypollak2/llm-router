"""Tests for DecisionEngine boost support — agent profiles bias decisions."""
from __future__ import annotations

import pytest

from llm_router.decisions.engine import Decision, DecisionEngine, _apply_boosts
from llm_router.signals.base import SignalScore


def _score(name, score, threshold=0.5):
    return SignalScore(name=name, score=score, threshold=threshold)


# ─────────────────────────────────────────────────────────────────────────
# _apply_boosts (pure function)
# ─────────────────────────────────────────────────────────────────────────

def test_no_boosts_returns_input():
    scores = {"a": _score("a", 0.3)}
    out = _apply_boosts(scores, {})
    assert out == scores


def test_unity_boost_is_noop():
    scores = {"a": _score("a", 0.3)}
    out = _apply_boosts(scores, {"a": 1.0})
    assert out["a"].score == 0.3


def test_positive_boost_multiplies():
    scores = {"code": _score("code", 0.3)}
    out = _apply_boosts(scores, {"code": 2.0})
    assert out["code"].score == pytest.approx(0.6)


def test_boost_clamps_to_one():
    scores = {"code": _score("code", 0.8)}
    out = _apply_boosts(scores, {"code": 2.0})  # 1.6 → clamped to 1.0
    assert out["code"].score == 1.0


def test_negative_boost_clamps_to_zero():
    """A 0 multiplier zeros the score — used to suppress a signal."""
    scores = {"code": _score("code", 0.8)}
    out = _apply_boosts(scores, {"code": 0.0})
    assert out["code"].score == 0.0


def test_boost_evidence_annotation():
    scores = {"code": _score("code", 0.3)}
    out = _apply_boosts(scores, {"code": 1.5})
    assert "boost×1.5" in out["code"].evidence


def test_unboosted_signals_pass_through():
    scores = {"a": _score("a", 0.3), "b": _score("b", 0.4)}
    out = _apply_boosts(scores, {"a": 2.0})  # only a boosted
    assert out["a"].score == pytest.approx(0.6)
    assert out["b"].score == 0.4
    assert out["b"] is scores["b"]  # untouched, same object


# ─────────────────────────────────────────────────────────────────────────
# DecisionEngine integration
# ─────────────────────────────────────────────────────────────────────────

def test_choose_without_boosts_unchanged_behavior():
    engine = DecisionEngine(
        decisions=[
            Decision(
                name="route_code", operator="SINGLE",
                signal_refs=("code",), action="code_chain", priority=50,
            )
        ]
    )
    scores = {"code": _score("code", 0.6, threshold=0.5)}
    result = engine.choose(scores)
    assert result.action == "code_chain"


def test_choose_with_boost_promotes_below_threshold_signal():
    """The whole point of boosts: turn a near-miss into a fire."""
    engine = DecisionEngine(
        decisions=[
            Decision(
                name="route_code", operator="SINGLE",
                signal_refs=("code",), action="code_chain", priority=50,
            )
        ]
    )
    scores = {"code": _score("code", 0.4, threshold=0.5)}  # below threshold
    # Without boost: default chain
    no_boost = engine.choose(scores)
    assert no_boost.action == "default_chain"
    # With 1.5× boost: 0.4 * 1.5 = 0.6 → fires
    boosted = engine.choose(scores, boosts={"code": 1.5})
    assert boosted.action == "code_chain"


def test_choose_with_zero_boost_suppresses_signal():
    """A 0 boost can disable a signal that would otherwise fire."""
    engine = DecisionEngine(
        decisions=[
            Decision(
                name="route_code", operator="SINGLE",
                signal_refs=("code",), action="code_chain", priority=50,
            )
        ]
    )
    scores = {"code": _score("code", 0.9, threshold=0.5)}  # would fire
    suppressed = engine.choose(scores, boosts={"code": 0.0})
    assert suppressed.action == "default_chain"


def test_choose_priority_still_respected_with_boosts():
    """Boosts shouldn't break priority ordering."""
    engine = DecisionEngine(
        decisions=[
            Decision(
                name="high_pri", operator="SINGLE",
                signal_refs=("a",), action="action_a", priority=10,
            ),
            Decision(
                name="low_pri", operator="SINGLE",
                signal_refs=("b",), action="action_b", priority=20,
            ),
        ]
    )
    scores = {
        "a": _score("a", 0.6, threshold=0.5),  # fires
        "b": _score("b", 0.6, threshold=0.5),  # also fires
    }
    result = engine.choose(scores, boosts={"a": 1.5, "b": 1.5})
    assert result.action == "action_a"  # higher priority wins
