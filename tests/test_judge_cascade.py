"""Tests for the synchronous judge-cascade primitives.

The cascade-driving judge is gated behind two env vars:
``LLM_ROUTER_JUDGE_CASCADE_THRESHOLD`` and ``LLM_ROUTER_JUDGE_CASCADE_SAMPLE_RATE``.
Both default to 0 (off) — flipping them on in production must be a
conscious act, since a misbehaving judge that scores everything low
would force every routed call into a retry, doubling spend.

These tests pin three guarantees:

1. Disabled-by-default — ``should_cascade`` returns False when no env
   is set, even on a 0.0 input score.
2. ``None`` scores never cascade — a degraded judge cannot amplify
   failures by triggering retries when it has no opinion.
3. Score parsing tolerates prose-wrapped JSON because cheap judge
   models sometimes ignore the "no prose" instruction.
"""

from __future__ import annotations

import random


from llm_router.judge_cascade import (
    JUDGE_DISABLED,
    _cascade_sample_rate,
    _cascade_threshold,
    _parse_score,
    should_cascade,
    should_judge_inline,
)


# ── Defaults ────────────────────────────────────────────────────────────


def test_cascade_disabled_by_default(monkeypatch):
    """No env → threshold is the disable sentinel."""
    monkeypatch.delenv("LLM_ROUTER_JUDGE_CASCADE_THRESHOLD", raising=False)
    assert _cascade_threshold() == JUDGE_DISABLED


def test_sample_rate_disabled_by_default(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_JUDGE_CASCADE_SAMPLE_RATE", raising=False)
    assert _cascade_sample_rate() == 0.0


# ── Threshold parsing robustness ────────────────────────────────────────


def test_threshold_out_of_range_treated_as_disabled(monkeypatch):
    """A misconfigured threshold (>1 or <0) must NOT silently activate.

    The product contract is "disabled unless explicitly enabled with a
    valid value". Silently clamping to 1.0 would accidentally cascade
    every call when someone typos a decimal point.
    """
    monkeypatch.setenv("LLM_ROUTER_JUDGE_CASCADE_THRESHOLD", "5.0")
    assert _cascade_threshold() == JUDGE_DISABLED
    monkeypatch.setenv("LLM_ROUTER_JUDGE_CASCADE_THRESHOLD", "-0.1")
    assert _cascade_threshold() == JUDGE_DISABLED


def test_threshold_garbage_treated_as_disabled(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_JUDGE_CASCADE_THRESHOLD", "not-a-number")
    assert _cascade_threshold() == JUDGE_DISABLED


def test_threshold_in_range_is_honored(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_JUDGE_CASCADE_THRESHOLD", "0.6")
    assert _cascade_threshold() == 0.6


# ── should_cascade contract ─────────────────────────────────────────────


def test_should_cascade_false_when_disabled(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_JUDGE_CASCADE_THRESHOLD", raising=False)
    # Even a clearly-bad score must not cascade when the feature is off.
    assert should_cascade(score=0.0) is False
    assert should_cascade(score=0.1) is False


def test_should_cascade_false_when_score_is_none(monkeypatch):
    """Degraded judge must never amplify failures by triggering retries."""
    monkeypatch.setenv("LLM_ROUTER_JUDGE_CASCADE_THRESHOLD", "0.6")
    assert should_cascade(score=None) is False


def test_should_cascade_true_when_score_below_threshold(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_JUDGE_CASCADE_THRESHOLD", "0.6")
    assert should_cascade(score=0.59) is True
    assert should_cascade(score=0.0) is True


def test_should_cascade_false_at_or_above_threshold(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_JUDGE_CASCADE_THRESHOLD", "0.6")
    # Boundary: 0.6 means "score >= 0.6 is good enough" — equal is not
    # below the threshold, so don't cascade. Avoids the most common
    # off-by-one debate by being explicit.
    assert should_cascade(score=0.6) is False
    assert should_cascade(score=0.61) is False
    assert should_cascade(score=0.99) is False


def test_explicit_threshold_overrides_env(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_JUDGE_CASCADE_THRESHOLD", "0.9")
    # Explicit kwarg wins — useful for one-off calls / tests.
    assert should_cascade(score=0.85, threshold=0.5) is False
    assert should_cascade(score=0.85, threshold=0.9) is True


# ── Sampling ────────────────────────────────────────────────────────────


def test_should_judge_inline_respects_zero_rate(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_JUDGE_CASCADE_SAMPLE_RATE", "0.0")
    rng = random.Random(0)
    assert should_judge_inline(rng) is False


def test_should_judge_inline_with_full_rate_always_fires(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_JUDGE_CASCADE_SAMPLE_RATE", "1.0")
    rng = random.Random(0)
    # With rate 1.0, every roll is "below 1.0" — always fires.
    for _ in range(20):
        assert should_judge_inline(rng) is True


def test_should_judge_inline_sampling_is_approximately_correct(monkeypatch):
    """A deterministic RNG and 1000 trials should hit ~25% with rate 0.25."""
    monkeypatch.setenv("LLM_ROUTER_JUDGE_CASCADE_SAMPLE_RATE", "0.25")
    rng = random.Random(42)
    fires = sum(should_judge_inline(rng) for _ in range(1000))
    # 25% of 1000 = 250 expected; allow ±5pp of noise.
    assert 200 <= fires <= 300, f"expected ~250 fires, got {fires}"


# ── Score parsing ──────────────────────────────────────────────────────


def test_parse_score_pure_json():
    assert _parse_score('{"score": 0.85}') == 0.85


def test_parse_score_prose_wrapped():
    """Cheap judges sometimes ignore the 'no prose' instruction."""
    text = 'Here is my evaluation:\n{"score": 0.3}\nThe response was weak.'
    assert _parse_score(text) == 0.3


def test_parse_score_clamps_to_unit_interval():
    """A misbehaving judge that returns >1 or <0 must be clamped."""
    assert _parse_score('{"score": 1.5}') == 1.0
    assert _parse_score('{"score": -0.5}') == 0.0


def test_parse_score_missing_returns_none():
    assert _parse_score("nothing scored here") is None
    assert _parse_score("") is None
    assert _parse_score('{"verdict": "bad"}') is None
