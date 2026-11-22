"""EKIND-1: a failing local backend is recorded by a fixed-vocabulary kind, never by runner text.

Evidence: 2026-11-21 22:08:25-22:11:04Z the runner hit a Metal OOM and answered ~115 requests with
'Compute error'; the proxy logged only 'empty response' and probed every 60 s. All runners are fakes here.
"""
import json

import pytest

from llm_router.proxy import backend_health as bh
from llm_router.proxy import backend_recycle as br
from tests.test_proxy import _post, _req, _rows
from tests.test_proxy_backend_health import Clock, _app, policy  # noqa: F401
from tests.test_proxy_selfheal import command  # noqa: F401

SECRET = "SECRET-sk-9f8e7d6c5b4a-do-not-log"


@pytest.mark.parametrize("err,kind", [
    ("RuntimeError: HTTP 500: ggml_metal_synchronize: kIOGPUCommandBufferCallbackErrorOutOfMemory", bh.KIND_RUNNER_OOM),
    ("ollama error: Insufficient Memory (00000008:kIOGPU)", bh.KIND_RUNNER_OOM),
    ("RuntimeError: HTTP 500: decode() failed: Compute error.", bh.KIND_RUNNER_COMPUTE),
    ("ollama error: llama runner process has terminated: signal: killed", bh.KIND_RUNNER_COMPUTE),
    ("RuntimeError: HTTP 500: XML syntax error on line 3: unexpected EOF", bh.KIND_XML_PARSE),
    ("RuntimeError: HTTP 500: internal server error", bh.KIND_HTTP_500),
    ("empty response", bh.KIND_EMPTY_BODY),
    ("zero-line stream", bh.KIND_EMPTY_BODY),
    ("unknown tool 'llm'", bh.KIND_OTHER),
    ("HTTP 404: model not found", bh.KIND_OTHER),
    (None, None),
    ("", None),
])
def test_error_kind_mapping(err, kind):
    assert bh.error_kind(err) == kind


def test_kind_is_from_a_closed_vocabulary_whatever_the_body_says():
    allowed = {bh.KIND_RUNNER_OOM, bh.KIND_RUNNER_COMPUTE, bh.KIND_XML_PARSE, bh.KIND_HTTP_500,
               bh.KIND_EMPTY_BODY, bh.KIND_OTHER}
    for err in (f"HTTP 500: Compute error {SECRET}", f"ollama error: {SECRET}", SECRET):
        assert bh.error_kind(err) in allowed


class FailingBackend:
    def __init__(self, err):
        self.err, self.calls, self.probes = err, 0, 0

    async def complete(self, body, timeout_s):
        self.calls += 1
        return None, self.err, {"first_token_s": 0.1}

    async def probe(self, timeout_s):
        self.probes += 1
        return False, "Compute error"


def test_runner_trip_is_unhealthy_longer_and_warns_with_the_kind_only():
    clock, lines = Clock(), []
    h = bh.BackendHealth(clock=clock, warn=lines.append, runner_cooldown_s=300.0)
    trip = h.record("m", f"ollama error: decode() failed: Compute error {SECRET}", "validation", 0.1)
    assert trip == {"state": "tripped", "trigger": "crash_signature"}
    assert h.error_kind_for("m") == bh.KIND_RUNNER_COMPUTE
    assert len(lines) == 1 and "kind=runner_compute_error" in lines[0] and "300s" in lines[0]
    assert SECRET not in lines[0] and "decode()" not in lines[0]
    clock.t += 299
    assert h.is_open("m")
    clock.t += 1
    assert not h.is_open("m")  # the probe is due only now


def test_non_runner_error_keeps_the_plain_cooldown():
    clock, lines = Clock(), []
    h = bh.BackendHealth(clock=clock, warn=lines.append, runner_cooldown_s=300.0)
    for _ in range(3):
        trip = h.record("m", "empty response", "validation", 5.0)
    assert trip == {"state": "tripped", "trigger": "consecutive_invalid"}
    assert h.error_kind_for("m") == bh.KIND_EMPTY_BODY and "60s" in lines[0]
    clock.t += 60
    assert not h.is_open("m")


def test_runner_cooldown_never_shorter_than_the_plain_one():
    clock = Clock()
    h = bh.BackendHealth(cooldown_s=900, clock=clock, warn=lambda _: None, runner_cooldown_s=300.0)
    h.record("m", "ollama error: Compute error", "validation", 0.1)
    clock.t += 600
    assert h.is_open("m")


@pytest.mark.usefixtures("policy")
async def test_proxy_row_carries_the_kind_not_the_runner_text(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_BACKEND_RECYCLE_CMD", raising=False)
    clock = Clock()
    backend = FailingBackend(f"RuntimeError: HTTP 500: decode() failed: Compute error {SECRET}")
    app = _app(tmp_path, backend, clock)
    await _post(app, _req())
    row = _rows(tmp_path)[-1]
    assert row["backend_error_kind"] == "runner_compute_error"
    assert row["backend_health"]["error_kind"] == "runner_compute_error"
    blob = json.dumps({k: v for k, v in row.items() if k != "detail"})
    assert SECRET not in blob and "Compute error" not in blob
    # No recycle command: no reload invented; the runner stays out for 300 s, not 60 s.
    assert app.state.recycler is None
    clock.t += 61
    await _post(app, _req())
    assert backend.calls == 1 and backend.probes == 0
    skipped = _rows(tmp_path)[-1]
    assert skipped["reason"] == "backend_unhealthy" and skipped["backend_health"]["error_kind"] == "runner_compute_error"
    clock.t += 240
    await _post(app, _req())
    assert backend.probes == 1


@pytest.mark.usefixtures("policy")
async def test_reload_requested_once_then_rate_limited_for_ten_minutes(tmp_path, monkeypatch, command):  # noqa: F811
    cmd, marker = command
    monkeypatch.setenv("LLM_ROUTER_BACKEND_RECYCLE_CMD", cmd)
    clock = Clock()
    backend = FailingBackend("ollama error: decode() failed: Compute error")
    app = _app(tmp_path, backend, clock, backend_cooldown_s=60)
    await _post(app, _req())
    assert _rows(tmp_path)[-1]["backend_health"]["recycle"] == "started"
    await app.state.recycler.task
    assert marker.read_text().splitlines() == ["recycled"]
    for _ in range(3):  # the breaker re-opens after each failed probe; the reload must not repeat
        clock.t += 61
        await _post(app, _req())
    assert marker.read_text().splitlines() == ["recycled"]
    assert br.RATE_LIMIT_S == 600.0
    clock.t += 600
    await _post(app, _req())
    if app.state.recycler.task is not None:
        await app.state.recycler.task
    assert marker.read_text().splitlines() == ["recycled", "recycled"]


@pytest.mark.usefixtures("policy")
async def test_non_runner_error_does_not_request_a_reload(tmp_path, monkeypatch, command):  # noqa: F811
    cmd, marker = command
    monkeypatch.setenv("LLM_ROUTER_BACKEND_RECYCLE_CMD", cmd)
    app = _app(tmp_path, FailingBackend("unknown tool 'llm'"), Clock())
    await _post(app, _req())
    row = _rows(tmp_path)[-1]
    assert "backend_error_kind" not in row and "recycle" not in (row.get("backend_health") or {})
    assert not marker.exists()


def _empties(h, clock, n, gap, elapsed=0.1):
    trip = None
    for _ in range(n):
        trip = h.record("m", "empty response", "validation", elapsed)
        clock.t += gap
    return trip


def test_empty_burst_trips_as_a_runner_failure():
    clock, lines = Clock(), []
    h = bh.BackendHealth(clock=clock, warn=lines.append, runner_cooldown_s=300.0, fail_n=10)
    assert _empties(h, clock, 2, 1) is None
    trip = h.record("m", "empty response", "validation", 0.1)
    assert trip == {"state": "tripped", "trigger": "empty_burst"}
    assert h.error_kind_for("m") == bh.KIND_RUNNER_EMPTY_BURST
    assert "kind=runner_empty_burst" in lines[0] and "300s" in lines[0]


def test_two_instant_empties_do_not_trip():
    clock = Clock()
    h = bh.BackendHealth(clock=clock, warn=lambda _: None, runner_cooldown_s=300.0)
    assert _empties(h, clock, 2, 1) is None and not h.is_open("m")


def test_three_slow_empties_keep_todays_behaviour():
    clock = Clock()
    h = bh.BackendHealth(clock=clock, warn=lambda _: None, runner_cooldown_s=300.0)
    trip = _empties(h, clock, 3, 1, elapsed=5.0)   # opens via the old streak, not the burst rule
    assert trip == {"state": "tripped", "trigger": "consecutive_invalid"}
    assert h.error_kind_for("m") == bh.KIND_EMPTY_BODY
    clock.t += 60
    assert not h.is_open("m")


def test_burst_window_expires_and_a_good_reply_resets():
    clock = Clock()
    h = bh.BackendHealth(clock=clock, warn=lambda _: None, runner_cooldown_s=300.0, fail_n=10)
    assert _empties(h, clock, 2, 11) is None          # gaps longer than the window
    assert h.record("m", "empty response", "validation", 0.1) is None
    clock.t += 1
    assert h.record("m", None, None, 3.0) is None     # a working reply clears the run
    assert _empties(h, clock, 2, 1) is None


def test_200_empty_stream_alone_is_empty_body_with_the_plain_cooldown():
    """Pins the limit: a lone empty reply carries no runner text, so it is not a runner kind."""
    clock = Clock()
    h = bh.BackendHealth(clock=clock, warn=lambda _: None, runner_cooldown_s=300.0)
    for _ in range(3):
        h.record("m", "empty response", "validation", 6.0)
    assert h.error_kind_for("m") == bh.KIND_EMPTY_BODY
    clock.t += 60
    assert not h.is_open("m")


async def test_failed_probe_updates_kind_but_runner_kind_is_sticky():
    clock = Clock()
    h = bh.BackendHealth(clock=clock, warn=lambda _: None, runner_cooldown_s=300.0)
    h.record("m", "ollama error: Compute error", "validation", 0.1)
    clock.t += 300
    ok, info = await h.admit("m", FailingBackend("x"))
    assert not ok and info["error_kind"] == bh.KIND_RUNNER_COMPUTE
    assert h.is_open("m") and (clock.t - h._states["m"].opened_at) == 0
    clock.t += 299
    assert h.is_open("m")   # still the 300 s cooldown after the probe's bare 'Compute error'
