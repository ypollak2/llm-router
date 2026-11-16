"""STALL-2: budget_exceeded with zero stream lines feeds the breaker; with lines it does not.

Incident 2026-11-16: every local step timed out at the 120 s budget while the runner was stalled.
Because budget_exceeded was always NEUTRAL, the breaker never opened and every step paid the full 120 s.
The fix: a budget_exceeded step that received zero stream lines counts as FAIL (feeds the streak);
one that received any line stays NEUTRAL. hedge_timeout stays NEUTRAL regardless.
"""
from __future__ import annotations

import json

import httpx
import pytest

from llm_router.proxy import backend_health as bh
from llm_router.proxy import backends as pb
from tests.test_proxy_backend_health import Clock


# ── classify ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("err,reason,elapsed,_box,expected", [
    # budget_exceeded: zero lines → FAIL
    ("exceeded step budget 120.0s", "budget_exceeded", 120.0, [0], bh.OUTCOME_FAIL),
    # budget_exceeded: one or more lines → NEUTRAL
    ("exceeded step budget 120.0s", "budget_exceeded", 120.0, [1], bh.OUTCOME_NEUTRAL),
    ("exceeded step budget 120.0s", "budget_exceeded", 120.0, [5], bh.OUTCOME_NEUTRAL),
    # budget_exceeded: no box at all (budget spent before backend was called) → NEUTRAL
    ("exceeded step budget 120.0s", "budget_exceeded", 120.0, None, bh.OUTCOME_NEUTRAL),
    # hedge_timeout: always NEUTRAL, regardless of _box
    ("no first token within 8.0s", "hedge_timeout", 8.0, [0], bh.OUTCOME_NEUTRAL),
    ("no first token within 8.0s", "hedge_timeout", 8.0, [1], bh.OUTCOME_NEUTRAL),
    ("no first token within 8.0s", "hedge_timeout", 8.0, None, bh.OUTCOME_NEUTRAL),
    # existing: empty response and validation still FAIL regardless of _box
    ("empty response", "validation", 0.1, [0], bh.OUTCOME_FAIL),
    ("empty response", "validation", 0.1, [1], bh.OUTCOME_FAIL),
    # existing: ok stays ok
    (None, None, 3.0, None, bh.OUTCOME_OK),
])
def test_classify_budget_exceeded_and_hedge_timeout(err, reason, elapsed, _box, expected):
    assert bh.classify(err, reason, elapsed, _box=_box) == expected


# ── the breaker ───────────────────────────────────────────────────────────────


async def test_n_consecutive_zero_byte_budget_exceeded_opens_breaker():
    """N zero-byte budget_exceeded outcomes open the breaker."""
    clock = Clock()
    h = bh.BackendHealth(fail_n=3, cooldown_s=60, clock=clock, warn=lambda s: None)
    h.record("m", "exceeded step budget 120.0s", "budget_exceeded", 120.0, _box=[0])
    h.record("m", "exceeded step budget 120.0s", "budget_exceeded", 120.0, _box=[0])
    tripped = h.record("m", "exceeded step budget 120.0s", "budget_exceeded", 120.0, _box=[0])
    assert tripped and tripped["state"] == "tripped" and tripped["trigger"] == "consecutive_invalid"


async def test_budget_exceeded_with_lines_does_not_count():
    """A budget_exceeded that received stream lines is NEUTRAL: it never opens the breaker on its own."""
    h = bh.BackendHealth(fail_n=2, cooldown_s=60, clock=Clock(), warn=lambda s: None)
    for _ in range(6):
        assert h.record("m", "exceeded step budget 120.0s", "budget_exceeded", 120.0, _box=[1]) is None
    assert h.record("m", "exceeded step budget 120.0s", "budget_exceeded", 120.0) is None   # no box: neutral too


async def test_hedge_timeout_stays_neutral():
    """hedge_timeout does not feed or reset the streak."""
    clock = Clock()
    h = bh.BackendHealth(fail_n=2, cooldown_s=60, clock=clock, warn=lambda s: None)
    h.record("m", "exceeded step budget 120.0s", "budget_exceeded", 120.0, _box=[0])
    h.record("m", "no first token within 8.0s", "hedge_timeout", 8.0, _box=[0])
    assert h.record("m", "exceeded step budget 120.0s", "budget_exceeded", 120.0, _box=[0])["state"] == "tripped"


async def test_mixed_zero_byte_then_with_lines_then_zero_byte():
    """Zero, with-lines, zero at fail_n=2 DOES open on the second zero-line outcome, and not before.

    The existing NEUTRAL semantics (test_timeouts_neither_count_nor_reset): a neutral outcome neither feeds
    nor resets the streak. So with-lines in the middle is invisible: the streak is 1, 1, 2 and opens on the
    third call. (The brief's "does not open at fail_n=2" holds only if the with-lines outcome reset the
    streak, which NEUTRAL does not; this test pins the actual semantics.)
    """
    h = bh.BackendHealth(fail_n=2, cooldown_s=60, clock=Clock(), warn=lambda s: None)
    assert h.record("m", "exceeded step budget 120.0s", "budget_exceeded", 120.0, _box=[0]) is None   # streak 1
    assert h.record("m", "exceeded step budget 120.0s", "budget_exceeded", 120.0, _box=[3]) is None   # neutral
    tripped = h.record("m", "exceeded step budget 120.0s", "budget_exceeded", 120.0, _box=[0])        # streak 2
    assert tripped and tripped["trigger"] == "consecutive_invalid"


async def test_a_working_reply_between_zero_line_timeouts_resets_the_streak():
    h = bh.BackendHealth(fail_n=2, cooldown_s=60, clock=Clock(), warn=lambda s: None)
    h.record("m", "exceeded step budget 120.0s", "budget_exceeded", 120.0, _box=[0])
    h.record("m", None, None, 2.0)                                                                    # ok: reset
    assert h.record("m", "exceeded step budget 120.0s", "budget_exceeded", 120.0, _box=[0]) is None


# ── the bytes signal: the real OllamaBackend under asyncio.wait_for, as the proxy calls it ──────────────────


class _SlowStream(httpx.AsyncByteStream):
    """Yields ``chunks`` then hangs, so the caller's wait_for expires mid-stream."""

    def __init__(self, chunks: list[bytes], delay_first_s: float = 0.0) -> None:
        self.chunks, self.delay_first_s = chunks, delay_first_s

    async def __aiter__(self):
        import asyncio

        await asyncio.sleep(self.delay_first_s)
        for c in self.chunks:
            yield c
        await asyncio.sleep(30)


async def _run_under_budget(chunks: list[bytes], delay_first_s: float = 0.0) -> list[int]:
    import asyncio

    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, stream=_SlowStream(chunks, delay_first_s))))
    backend = pb.OllamaBackend("ollama/x", client, base_url="http://127.0.0.1:1", num_ctx=32768, hedge_s=None)
    box = bh.begin_stream_progress()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(backend.complete({"model": "x", "messages": [{"role": "user", "content": "hi"}]}, 0.5),
                               timeout=0.5)
    return box


async def test_backend_that_streamed_a_line_before_the_budget_leaves_a_nonzero_box():
    line = json.dumps({"message": {"role": "assistant", "content": "o"}, "done": False}).encode() + b"\n"
    box = await _run_under_budget([line])
    assert box[0] >= 1
    assert bh.classify("exceeded step budget 0.5s", "budget_exceeded", 0.5, _box=box) == bh.OUTCOME_NEUTRAL


async def test_backend_that_sent_nothing_before_the_budget_leaves_a_zero_box():
    box = await _run_under_budget([])
    assert box[0] == 0
    assert bh.classify("exceeded step budget 0.5s", "budget_exceeded", 0.5, _box=box) == bh.OUTCOME_FAIL
