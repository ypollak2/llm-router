"""#28 (Gate 7) — summary.py baseline uses REAL recorded tokens, not a latency guess.

lineage rows carry input_tokens/output_tokens (the routing_decisions store has had
these columns); summary.collect() must price the baseline counterfactual from those
actual counts when present, and only fall back to the latency proxy for token-less
rows — counting each fallback in baseline_estimated_rows so the figure can be labelled
honestly instead of an estimate masquerading as a measured total.
"""
from __future__ import annotations

import time

from llm_router.observability.summary import (
    _BASELINE_PER_1K_INPUT,
    _BASELINE_PER_1K_OUTPUT,
    collect,
)


class _FakeLineage:
    def __init__(self, rows):
        self._rows = rows

    def recent(self, limit=5000):
        return self._rows


class _FakeSessions:
    def rollup(self, sid):  # never reached — rows carry no session_id
        raise AssertionError("rollup should not be called")


def _row(**kw):
    base = {"timestamp": time.time(), "cost_usd": 0.0, "latency_ms": 0.0,
            "model_chosen": "ollama/x", "input_tokens": 0, "output_tokens": 0}
    base.update(kw)
    return base


def test_baseline_uses_recorded_tokens_not_latency():
    # A token-bearing row with a HUGE latency: if the baseline used latency it would
    # be enormous; using the recorded 1000/500 tokens pins it to the exact token price.
    row = _row(input_tokens=1000, output_tokens=500, latency_ms=999_999.0)
    data = collect(_FakeLineage([row]), _FakeSessions(), since_seconds=None)
    expected = (1000 / 1000) * _BASELINE_PER_1K_INPUT + (500 / 1000) * _BASELINE_PER_1K_OUTPUT
    assert data.baseline_cost_usd == expected
    assert data.baseline_estimated_rows == 0  # fully measured, nothing estimated


def test_tokenless_row_falls_back_to_estimate_and_is_counted():
    row = _row(input_tokens=0, output_tokens=0, latency_ms=8000.0)
    data = collect(_FakeLineage([row]), _FakeSessions(), since_seconds=None)
    est_out = max(20, 8000 // 4)
    est_in = max(50, est_out * 2)
    expected = (est_in / 1000) * _BASELINE_PER_1K_INPUT + (est_out / 1000) * _BASELINE_PER_1K_OUTPUT
    assert data.baseline_cost_usd == expected
    assert data.baseline_estimated_rows == 1  # honestly flagged as estimated


def test_mixed_rows_only_count_estimated_ones():
    rows = [
        _row(input_tokens=800, output_tokens=200, latency_ms=1.0),   # measured
        _row(input_tokens=0, output_tokens=0, latency_ms=4000.0),    # estimated
        _row(input_tokens=100, output_tokens=100, latency_ms=1.0),   # measured
    ]
    data = collect(_FakeLineage(rows), _FakeSessions(), since_seconds=None)
    assert data.baseline_estimated_rows == 1
