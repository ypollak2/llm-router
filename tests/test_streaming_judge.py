"""Tests for the streaming-aware quality judge.

Five contracts:

1. Disabled by default — operators must opt in explicitly via
   ``LLM_ROUTER_STREAMING_JUDGE=1`` AND a positive cascade threshold.
2. No scoring before ``min_tokens`` of partial response has streamed.
3. Subsequent scores throttled by ``stride_tokens``.
4. Below-threshold scores only intercept after a ``streak`` of
   consecutive low scores — one noisy score doesn't cascade.
5. ``None`` scores never trigger intercept (degraded judge can't
   amplify failures).
"""

from __future__ import annotations

import pytest

from llm_router.streaming_judge import StreamingJudge


# ── Helpers ─────────────────────────────────────────────────────────────


class _StubScorer:
    """Async callable that returns a queued sequence of scores."""

    def __init__(self, scores):
        self._scores = list(scores)
        self.calls = 0

    async def __call__(self, prompt, partial, task_type):
        self.calls += 1
        if not self._scores:
            return None
        return self._scores.pop(0)


@pytest.fixture
def enabled(monkeypatch):
    """Flip the master switch + a non-zero threshold so the feature is live."""
    monkeypatch.setenv("LLM_ROUTER_STREAMING_JUDGE", "1")
    monkeypatch.setenv("LLM_ROUTER_JUDGE_CASCADE_THRESHOLD", "0.6")


# ── Disabled paths ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_disabled_by_default(monkeypatch):
    """Without LLM_ROUTER_STREAMING_JUDGE=1, observe returns ``disabled``
    no matter what the threshold or streak knobs say."""
    monkeypatch.delenv("LLM_ROUTER_STREAMING_JUDGE", raising=False)
    monkeypatch.setenv("LLM_ROUTER_JUDGE_CASCADE_THRESHOLD", "0.9")

    scorer = _StubScorer([0.0])
    judge = StreamingJudge(prompt="p", task_type="query",
                           min_tokens=4, stride_tokens=4, streak=1,
                           score_fn=scorer)
    verdict = await judge.observe("x" * 200)
    assert verdict.intercept is False
    assert verdict.reason == "disabled"
    # Scorer never invoked when the feature is off.
    assert scorer.calls == 0


@pytest.mark.asyncio
async def test_zero_threshold_treated_as_disabled(enabled, monkeypatch):
    """A threshold of 0 (or below) means cascading is off even when the
    streaming switch is on — same convention as judge_cascade."""
    monkeypatch.setenv("LLM_ROUTER_JUDGE_CASCADE_THRESHOLD", "0.0")

    scorer = _StubScorer([0.0])
    judge = StreamingJudge(prompt="p", min_tokens=4, stride_tokens=4,
                           streak=1, score_fn=scorer)
    verdict = await judge.observe("x" * 200)
    assert verdict.intercept is False
    assert verdict.reason == "no_threshold"


# ── Throttling ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_score_before_min_tokens(enabled):
    scorer = _StubScorer([0.0])
    judge = StreamingJudge(prompt="p", min_tokens=200, stride_tokens=10,
                           streak=1, score_fn=scorer)
    # 40 chars ≈ 10 approx tokens, well below 200.
    verdict = await judge.observe("a" * 40)
    assert verdict.score is None
    assert verdict.reason == "below_min_tokens"
    assert scorer.calls == 0


@pytest.mark.asyncio
async def test_stride_throttles_subsequent_scores(enabled):
    scorer = _StubScorer([0.9, 0.9])
    judge = StreamingJudge(prompt="p", min_tokens=4, stride_tokens=100,
                           streak=1, score_fn=scorer)
    # First chunk crosses min_tokens and triggers a score.
    v1 = await judge.observe("x" * 40)  # ~10 tokens; above min
    assert v1.score == 0.9
    # Second chunk is small — within stride threshold, no new score.
    v2 = await judge.observe("y" * 12)
    assert v2.score is None
    assert v2.reason == "awaiting_stride"
    # Only one call.
    assert scorer.calls == 1


# ── Streak gating ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_one_bad_score_does_not_intercept(enabled):
    """A single below-threshold score must not cascade — streak guard."""
    scorer = _StubScorer([0.3])
    judge = StreamingJudge(prompt="p", min_tokens=4, stride_tokens=4,
                           streak=2, score_fn=scorer)
    verdict = await judge.observe("x" * 40)
    assert verdict.score == 0.3
    assert verdict.intercept is False
    assert "streaking" in verdict.reason


@pytest.mark.asyncio
async def test_streak_of_below_threshold_scores_intercepts(enabled):
    scorer = _StubScorer([0.3, 0.3])
    judge = StreamingJudge(prompt="p", min_tokens=4, stride_tokens=4,
                           streak=2, score_fn=scorer)
    v1 = await judge.observe("x" * 40)
    assert v1.intercept is False
    v2 = await judge.observe("y" * 40)
    assert v2.intercept is True
    assert "below_threshold_streak" in v2.reason


@pytest.mark.asyncio
async def test_recovery_resets_the_streak(enabled):
    """A passing score in the middle of a bad run resets the counter,
    so we don't accidentally intercept after a transient dip."""
    scorer = _StubScorer([0.3, 0.9, 0.3])
    judge = StreamingJudge(prompt="p", min_tokens=4, stride_tokens=4,
                           streak=2, score_fn=scorer)
    v1 = await judge.observe("x" * 40)
    assert v1.intercept is False  # streak=1
    v2 = await judge.observe("y" * 40)
    assert v2.score == 0.9
    assert v2.intercept is False  # streak reset
    v3 = await judge.observe("z" * 40)
    assert v3.score == 0.3
    assert v3.intercept is False  # streak=1 again, not 2


# ── None-score safety ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_none_score_never_intercepts(enabled):
    """A degraded judge (returns None) must not amplify failures by
    triggering retries."""
    scorer = _StubScorer([None, None])
    judge = StreamingJudge(prompt="p", min_tokens=4, stride_tokens=4,
                           streak=1, score_fn=scorer)
    v1 = await judge.observe("x" * 40)
    assert v1.intercept is False
    assert v1.reason == "score_missing"


# ── Buffered state ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_buffered_chunks_accessible(enabled):
    """Caller needs the assembled partial response to replay into the
    retry stream."""
    scorer = _StubScorer([0.9])
    judge = StreamingJudge(prompt="p", min_tokens=4, stride_tokens=4,
                           streak=1, score_fn=scorer)
    await judge.observe("hello ")
    await judge.observe("world")
    assert judge.buffered == "hello world"
