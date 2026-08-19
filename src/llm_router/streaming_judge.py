"""Streaming-aware quality judge — progressive scoring during a stream.

Background
----------
Both :mod:`llm_router.judge` (fire-and-forget, post-response) and
:mod:`llm_router.judge_cascade` (synchronous, post-response) score *complete*
responses. For streaming chat UX that's too late — the user has already
seen most of the bad answer by the time the score arrives. This module
adds the progressive case: score the partial response at configurable
token milestones, and let the caller intercept mid-stream when the score
falls below ``threshold``.

The intercept doesn't perform the model swap itself — that's a routing
concern. The streaming judge only emits a verdict; the caller (the
router's streaming handler) is responsible for cancelling the upstream
stream and starting a fresh one against the next chain entry.

Why this is hard
----------------
Partial responses have less context. Scoring a 60-token prefix of a
longer answer can mislead the judge — sometimes the answer turns out
fine. To mitigate:

* Only score after at least ``min_tokens`` have streamed (default 80).
* Each subsequent scoring point is at least ``stride_tokens`` apart
  (default 120) so we don't burn judge cost on a torrent of small
  windows.
* ``streak`` requirement: cascade only after ``streak`` consecutive
  below-threshold scores (default 2). Stops a single noisy score from
  triggering a retry the next chunk would have rescued.

All three knobs are env-var configurable so the feature can be flipped
on without a code release.

Usage shape
-----------

.. code-block:: python

    judge = StreamingJudge(prompt=user_prompt, task_type="query")
    async for chunk in upstream:
        verdict = await judge.observe(chunk)
        if verdict.intercept:
            await upstream.aclose()
            return await retry_with_next_model(chain)
        yield chunk
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Awaitable

from llm_router.judge_cascade import _cascade_threshold, judge_inline


__all__ = [
    "StreamingJudgeVerdict",
    "StreamingJudge",
]


def _env_int(name: str, default: int) -> int:
    """Read a non-negative int from env; fall back to ``default`` on any
    parse failure or negative value."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def _streaming_enabled() -> bool:
    """Master switch — read once per construction so per-call cost stays low."""
    return os.environ.get(
        "LLM_ROUTER_STREAMING_JUDGE", ""
    ).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class StreamingJudgeVerdict:
    """Outcome of a single observation step.

    * ``score`` is the latest score from the judge, or ``None`` when
      no score was computed at this step (either we haven't crossed
      ``min_tokens`` yet or we haven't hit a stride boundary).
    * ``intercept`` is the actionable bit — when ``True``, the caller
      should cancel the upstream stream and cascade to the next model.
      It's only ever ``True`` after a confirmed streak of below-
      threshold scores, so callers can act on it directly without
      additional voting logic.
    * ``reason`` is a short human-readable label suitable for log
      lines — ``"below_threshold_streak"`` / ``"disabled"`` / etc.
    """

    score: float | None
    intercept: bool
    reason: str


@dataclass
class StreamingJudge:
    """Progressive scorer for streaming responses.

    Stateful: instantiate once per stream, call ``observe(chunk)`` for
    every chunk the upstream emits, and inspect the returned verdict
    to decide whether to keep streaming, cascade, or accept.
    """

    prompt: str
    task_type: str = "query"
    # Defaults pull from env so operators can tune live; explicit
    # constructor args win (handy for tests + ad-hoc benchmarks).
    threshold: float | None = None
    min_tokens: int | None = None
    stride_tokens: int | None = None
    streak: int | None = None
    score_fn: Callable[[str, str, str], Awaitable[float | None]] | None = None

    # Mutable state — never read by callers, only by ``observe``.
    _buffer: list[str] = field(default_factory=list, init=False, repr=False)
    _approx_tokens: int = field(default=0, init=False, repr=False)
    _last_scored_at: int = field(default=0, init=False, repr=False)
    _below_streak: int = field(default=0, init=False, repr=False)
    _enabled: bool = field(default=True, init=False, repr=False)

    def __post_init__(self) -> None:
        self._enabled = _streaming_enabled()
        if self.threshold is None:
            t = _cascade_threshold()
            # Streaming feature is fully gated even when threshold is
            # configured — operators have to flip LLM_ROUTER_STREAMING_JUDGE
            # on explicitly. Avoids surprising users who turned on the
            # post-response cascade and got streaming side-effects.
            self.threshold = t
        if self.min_tokens is None:
            self.min_tokens = _env_int("LLM_ROUTER_STREAMING_JUDGE_MIN_TOKENS", 80)
        if self.stride_tokens is None:
            self.stride_tokens = _env_int("LLM_ROUTER_STREAMING_JUDGE_STRIDE_TOKENS", 120)
        if self.streak is None:
            self.streak = _env_int("LLM_ROUTER_STREAMING_JUDGE_STREAK", 2)

    async def observe(self, chunk: str) -> StreamingJudgeVerdict:
        """Process one streamed chunk and emit a verdict.

        Approximates tokens as ``len(text) // 4`` — the chars/4 rule is
        accurate enough for cadence decisions; we don't need
        tiktoken-grade counts here. The judge itself sees the real
        partial response.
        """
        if not chunk:
            return StreamingJudgeVerdict(score=None, intercept=False,
                                         reason="empty_chunk")
        self._buffer.append(chunk)
        self._approx_tokens += max(1, len(chunk) // 4)

        if not self._enabled:
            return StreamingJudgeVerdict(score=None, intercept=False,
                                         reason="disabled")
        if (self.threshold or 0.0) <= 0.0:
            return StreamingJudgeVerdict(score=None, intercept=False,
                                         reason="no_threshold")
        # Haven't streamed enough yet for a meaningful judgment.
        if self._approx_tokens < (self.min_tokens or 0):
            return StreamingJudgeVerdict(score=None, intercept=False,
                                         reason="below_min_tokens")
        # Throttle: don't re-score until we've grown by ``stride`` more
        # tokens since the last attempt. The very first scoring (when
        # ``_last_scored_at`` is still 0) is exempt — stride gates the
        # *gap between* scores, not the time before the first one.
        if (
            self._last_scored_at > 0
            and (self._approx_tokens - self._last_scored_at) < (self.stride_tokens or 0)
        ):
            return StreamingJudgeVerdict(score=None, intercept=False,
                                         reason="awaiting_stride")

        partial = "".join(self._buffer)
        self._last_scored_at = self._approx_tokens

        # ``score_fn`` lets callers (and tests) inject a deterministic
        # scorer; production falls through to the inline judge.
        if self.score_fn is not None:
            score = await self.score_fn(self.prompt, partial, self.task_type)
        else:
            score = await judge_inline(self.prompt, partial, self.task_type)

        # A None score (judge degraded) never triggers cascade — same
        # rule as the post-response cascade in judge_cascade.
        if score is None:
            return StreamingJudgeVerdict(score=None, intercept=False,
                                         reason="score_missing")
        if score >= (self.threshold or 0.0):
            self._below_streak = 0
            return StreamingJudgeVerdict(score=score, intercept=False,
                                         reason="above_threshold")
        # Below threshold: extend the streak; cascade only if we've
        # crossed ``streak`` consecutive observations.
        self._below_streak += 1
        if self._below_streak >= (self.streak or 1):
            return StreamingJudgeVerdict(
                score=score,
                intercept=True,
                reason=f"below_threshold_streak[{self._below_streak}]",
            )
        return StreamingJudgeVerdict(
            score=score,
            intercept=False,
            reason=f"below_threshold_streaking[{self._below_streak}]",
        )

    @property
    def buffered(self) -> str:
        """Return the concatenated chunks observed so far.

        Useful when the caller wants to replay the buffer into the
        retry stream or surface the partial response to the user.
        """
        return "".join(self._buffer)
