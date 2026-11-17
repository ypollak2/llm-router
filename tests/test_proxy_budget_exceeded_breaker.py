"""STALL-2: a budget_exceeded step in which the request was sent and nothing came back feeds the breaker.

Incident 2026-11-16: the local runner stalled, the request never received response headers, so the hedge
could not fire and the whole 120 s budget passed inside ``client.stream``. Because budget_exceeded was always
NEUTRAL, the breaker never opened and every step paid the full budget before falling back to Claude.

Progress box (``backend_health.begin_stream_progress``): -1 not sent, 0 sent and silent, >0 headers or lines came
back. Only 0 is a FAIL. A step that never reached the send, one that got headers or lines, ``hedge_timeout``, and a
backend that never reports stay NEUTRAL.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from llm_router.proxy import backend_health as bh
from llm_router.proxy import backends as pb
from tests.test_proxy_backend_health import Clock, _app, policy  # noqa: F401
from tests.test_proxy import _post, _req, _rows

BUDGET = "exceeded step budget 120.0s"


# ── classify ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("err,reason,elapsed,_box,expected", [
    (BUDGET, "budget_exceeded", 120.0, [0], bh.OUTCOME_FAIL),       # sent, no headers, no line
    (BUDGET, "budget_exceeded", 120.0, [-1], bh.OUTCOME_NEUTRAL),   # budget spent before the request was sent
    (BUDGET, "budget_exceeded", 120.0, [1], bh.OUTCOME_NEUTRAL),    # headers arrived, no line
    (BUDGET, "budget_exceeded", 120.0, [5], bh.OUTCOME_NEUTRAL),    # lines arrived
    (BUDGET, "budget_exceeded", 120.0, None, bh.OUTCOME_NEUTRAL),   # no box at all
    ("no first token within 8.0s", "hedge_timeout", 8.0, [0], bh.OUTCOME_NEUTRAL),
    ("no first token within 8.0s", "hedge_timeout", 8.0, [1], bh.OUTCOME_NEUTRAL),
    ("no first token within 8.0s", "hedge_timeout", 8.0, None, bh.OUTCOME_NEUTRAL),
    ("empty response", "validation", 0.1, [0], bh.OUTCOME_FAIL),
    ("empty response", "validation", 0.1, [1], bh.OUTCOME_FAIL),
    (None, None, 3.0, None, bh.OUTCOME_OK),
])
def test_classify_budget_exceeded_and_hedge_timeout(err, reason, elapsed, _box, expected):
    assert bh.classify(err, reason, elapsed, _box=_box) == expected


# ── the breaker ───────────────────────────────────────────────────────────────


def _rec(h, box):
    return h.record("m", BUDGET, "budget_exceeded", 120.0, _box=box)


async def test_fail_n_sent_and_silent_budget_timeouts_open_the_breaker():
    h = bh.BackendHealth(fail_n=3, cooldown_s=60, clock=Clock(), warn=lambda s: None)
    assert _rec(h, [0]) is None and _rec(h, [0]) is None
    tripped = _rec(h, [0])
    assert tripped and tripped["state"] == "tripped" and tripped["trigger"] == "consecutive_invalid"


@pytest.mark.parametrize("box", [[-1], [1], [5], None])
async def test_budget_timeouts_that_are_not_sent_and_silent_never_open_it(box):
    h = bh.BackendHealth(fail_n=2, cooldown_s=60, clock=Clock(), warn=lambda s: None)
    for _ in range(6):
        assert _rec(h, box) is None


async def test_hedge_timeout_stays_neutral():
    h = bh.BackendHealth(fail_n=2, cooldown_s=60, clock=Clock(), warn=lambda s: None)
    _rec(h, [0])
    h.record("m", "no first token within 8.0s", "hedge_timeout", 8.0, _box=[0])
    assert _rec(h, [0])["state"] == "tripped"       # the hedge neither fed nor reset the streak


async def test_mixed_silent_then_answered_then_silent():
    """Existing NEUTRAL semantics (test_timeouts_neither_count_nor_reset): neutral neither feeds nor resets.

    So at fail_n=2 the sequence silent, headers-only, silent opens on the third call (streak 1, 1, 2)."""
    h = bh.BackendHealth(fail_n=2, cooldown_s=60, clock=Clock(), warn=lambda s: None)
    assert _rec(h, [0]) is None
    assert _rec(h, [3]) is None
    assert _rec(h, [0])["trigger"] == "consecutive_invalid"


async def test_a_working_reply_between_silent_timeouts_resets_the_streak():
    h = bh.BackendHealth(fail_n=2, cooldown_s=60, clock=Clock(), warn=lambda s: None)
    _rec(h, [0])
    h.record("m", None, None, 2.0)
    assert _rec(h, [0]) is None


# ── the markers: the real OllamaBackend under asyncio.wait_for, as the proxy calls it ───────────────────────


class _Stalled(httpx.AsyncByteStream):
    """Response headers arrive at once; the body then yields ``chunks`` and hangs."""

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def __aiter__(self):
        for c in self.chunks:
            yield c
        await asyncio.sleep(30)


async def _backend_box(handler, *, guard_stalls: bool = False) -> list[int]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    backend = pb.OllamaBackend("ollama/x", client, base_url="http://127.0.0.1:1", num_ctx=32768, hedge_s=None)
    if guard_stalls:
        async def _slow_guard(purpose):
            await asyncio.sleep(30)
        backend._guard = _slow_guard
    box = bh.begin_stream_progress()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(backend.complete({"model": "x", "messages": [{"role": "user", "content": "hi"}]}, 0.5),
                               timeout=0.5)
    return box


async def test_a_line_before_the_budget_leaves_a_positive_box():
    line = json.dumps({"message": {"role": "assistant", "content": "o"}, "done": False}).encode() + b"\n"
    box = await _backend_box(lambda request: httpx.Response(200, stream=_Stalled([line])))
    assert box[0] > 1
    assert bh.classify(BUDGET, "budget_exceeded", 0.5, _box=box) == bh.OUTCOME_NEUTRAL


async def test_headers_but_no_line_before_the_budget_is_neutral():
    box = await _backend_box(lambda request: httpx.Response(200, stream=_Stalled([])))
    assert box[0] == 1
    assert bh.classify(BUDGET, "budget_exceeded", 0.5, _box=box) == bh.OUTCOME_NEUTRAL


async def test_request_sent_but_no_headers_before_the_budget_is_a_failure():
    async def handler(request):
        await asyncio.sleep(30)        # the stalled runner: no response headers at all

    box = await _backend_box(handler)
    assert box[0] == 0
    assert bh.classify(BUDGET, "budget_exceeded", 0.5, _box=box) == bh.OUTCOME_FAIL


async def test_budget_spent_before_the_request_was_sent_is_neutral():
    box = await _backend_box(lambda request: httpx.Response(200), guard_stalls=True)
    assert box[0] == bh.PROGRESS_NOT_SENT
    assert bh.classify(BUDGET, "budget_exceeded", 0.5, _box=box) == bh.OUTCOME_NEUTRAL


# ── the proxy: build_app wires the box into the breaker ─────────────────────────────────────────────────────


class SilentBackend:
    """A stalled runner: the request goes out, nothing comes back, the step budget runs out."""

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, body, timeout_s):
        self.calls += 1
        bh.mark_request_sent()
        await asyncio.sleep(30)


async def test_proxy_opens_the_breaker_after_fail_n_silent_budget_timeouts(tmp_path, policy):  # noqa: F811
    backend = SilentBackend()
    app = _app(tmp_path, backend, Clock(), backend_fail_n=2, backend_cooldown_s=60, step_budget_s=0.2)
    for _ in range(3):
        assert (await _post(app, _req())).status_code == 200      # Claude answers every step
    assert backend.calls == 2                                       # the third step never reached the backend
    rows = _rows(tmp_path)
    assert [r["reason"] for r in rows] == ["budget_exceeded"] * 2 + ["backend_unhealthy"]
    assert rows[1]["backend_health"]["state"] == "tripped"
