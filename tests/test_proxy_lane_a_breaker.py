"""Lane A PR2 (D-285, D-292, D-293, D-297): breaker, failure feed, 5xx/429/timeout fallback,
cache-switch rule, consecutive cap, complexity_knn veto. Default OFF; a lane that is off has no side effect.

Anthropic is a mocked upstream and the clock is a fake: no test makes a network call or sleeps.
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import stat
import threading
from pathlib import Path

import httpx
import pytest

from llm_router import complexity_knn, engine, session_kind
from llm_router.proxy import haiku_guard, lane_a, lane_a_breaker as lb, ledger
from llm_router.proxy import tiers as pt
from llm_router.proxy.cache_cost import Stickiness
from tests.test_proxy_lane_a import (HAIKU, LANE_ON, OFF_LANE, OPUS, _app, _body, _policy, _post, _raw, _rules,
                                     _state_files)

BR = lane_a.BreakerConfig()


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv(lane_a.ENV_SWITCH, raising=False)


def _breaker(tmp_path, clock=None, **cfg):
    return lb.LaneABreaker(lane_a.BreakerConfig(**cfg), tmp_path / "lane_a_breaker.json", clock or Clock())


# ── config ───────────────────────────────────────────────────────────────────

def test_defaults_and_strict_parse():
    c = lane_a.parse_config({"enabled": True})
    assert (c.max_switch_usd, c.max_consecutive, c.breaker) == (0.05, 6, lane_a.BreakerConfig())
    assert c.breaker.trip_failures == 5 and c.breaker.redo_min_n == 30 and c.breaker.redo_max == 0.15
    c = lane_a.parse_config({"max_consecutive": 2, "max_switch_usd": 0.2, "breaker": {"trip_failures": 3, "cooldown_s": 60}})
    assert (c.max_consecutive, c.max_switch_usd, c.breaker.trip_failures, c.breaker.cooldown_s) == (2, 0.2, 3, 60.0)
    for bad in ({"max_consecutive": 0}, {"max_consecutive": True}, {"max_consecutive": 1.5}, {"max_switch_usd": 0},
                {"breaker": {"nope": 1}}, {"breaker": {"trip_failures": 0}}, {"breaker": {"trip_failures": 2.5}},
                {"breaker": {"redo_max": 2}}, {"breaker": []}):
        with pytest.raises(ValueError, match="lane_a"):
            lane_a.parse_config(bad)


# ── the breaker state machine ────────────────────────────────────────────────

async def test_trips_on_n_failures_in_the_window_and_not_on_spread_out_ones(tmp_path):
    clk = Clock()
    b = _breaker(tmp_path, clk)
    assert await b.admit() == (True, False)
    for _ in range(4):
        b.record_failure("http_503")
        clk.t += 1
    assert await b.admit() == (True, False)  # 4 of 5
    b.record_failure("timeout")
    assert b.state == lb.OPEN and (await b.admit()) == (False, False)
    # failures spread beyond the 600 s window never accumulate
    b2 = _breaker(tmp_path / "x", Clock())
    for _ in range(20):
        b2.record_failure("http_500")
        b2.clock.t += 301
    assert b2.state == lb.CLOSED


async def test_half_open_after_cooldown_admits_one_probe_then_closes_on_success(tmp_path):
    clk = Clock()
    b = _breaker(tmp_path, clk)
    for _ in range(5):
        b.record_failure("http_500")
    clk.t += BR.cooldown_s - 1
    assert await b.admit() == (False, False)  # still cooling down
    clk.t += 2
    assert await b.admit() == (True, True)  # the probe
    assert b.state == lb.HALF_OPEN
    assert await b.admit() == (False, False)  # one at a time
    b.record_failure("http_500")  # a non-probe straggler does not decide the probe
    assert b.state == lb.HALF_OPEN
    b.record_success(probe=True)
    assert b.state == lb.CLOSED and b.failures == [] and await b.admit() == (True, False)


async def test_probe_failure_reopens_and_restarts_the_cooldown(tmp_path):
    clk = Clock()
    b = _breaker(tmp_path, clk)
    for _ in range(5):
        b.record_failure("http_500")
    clk.t += BR.cooldown_s + 1
    assert (await b.admit()) == (True, True)
    clk.t += 10
    b.record_failure("empty", probe=True)
    assert b.state == lb.OPEN and b.opened_at == clk.t
    clk.t += BR.cooldown_s - 1
    assert await b.admit() == (False, False)  # the new cooldown, not the old one
    clk.t += 2
    assert await b.admit() == (True, True)


async def test_a_probe_with_no_outcome_times_out_and_the_next_step_probes(tmp_path):
    clk = Clock()
    b = _breaker(tmp_path, clk)
    for _ in range(5):
        b.record_failure("http_500")
    clk.t += BR.cooldown_s + 1
    assert (await b.admit()) == (True, True)
    clk.t += BR.probe_timeout_s - 1
    assert (await b.admit()) == (False, False)
    clk.t += 2
    assert (await b.admit()) == (True, True)


async def test_redo_rate_trips_with_the_quality_cooldown(tmp_path):
    b = _breaker(tmp_path)
    for i in range(29):
        b.record_unit(i < 5)  # 5 redo of 29: below min_n, never evaluated
    assert b.state == lb.CLOSED
    b.record_unit(False)  # n=30, 5/30 = 16.7% > 15%
    assert b.state == lb.OPEN and b.cooldown_s == BR.quality_cooldown_s and b.reason.startswith("redo_rate:5/30")
    ok = _breaker(tmp_path / "ok")
    for i in range(60):
        ok.record_unit(i % 20 >= 17)  # never above 15% (exactly 15.0% at n=40 and 60)
    assert ok.state == lb.CLOSED


async def test_state_survives_a_restart_and_is_private(tmp_path):
    clk = Clock()
    b = _breaker(tmp_path, clk)
    for _ in range(5):
        b.record_failure("http_503")
    await b.flush()
    f = tmp_path / "lane_a_breaker.json"
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    raw = json.loads(f.read_text())
    assert raw["state"] == "open" and raw["key"] == "proxy_haiku:lane_a" and not list(tmp_path.glob("*.tmp"))
    again = _breaker(tmp_path, clk)  # a new process
    assert await again.admit() == (False, False) and again.state == lb.OPEN
    clk.t += BR.cooldown_s + 1
    assert await again.admit() == (True, True)  # the cooldown ran on wall time across the restart
    await again.flush()
    third = _breaker(tmp_path, clk)
    assert await third.admit() == (True, True)  # half-open persisted; the probe in flight before the restart is gone


async def test_unreadable_state_opens_the_breaker_and_records_a_failopen_code(tmp_path, monkeypatch):
    """PR3 S2: ONE failure direction. A state file that exists but cannot be used (corrupt, wrong key, locked)
    fails safe like an internal admit() error: the lane does not serve. An ABSENT file is a fresh start."""
    codes = []
    real = lb.failopen.record
    monkeypatch.setattr(lb.failopen, "record", lambda code, exc, *a, **k: codes.append(code) or real(code, exc, *a, **k))
    f = tmp_path / "lane_a_breaker.json"
    for content in ("{not json", json.dumps({"key": "other", "state": "open"}), json.dumps([1, 2]), ""):
        codes.clear()
        f.write_text(content)
        b = _breaker(tmp_path)
        assert await b.admit() == (False, False), content
        assert b.state == lb.OPEN and b.reason == "state_unreadable"
        assert "LR-FO-LANE-A-BREAKER-READ" in codes
        await b.flush()
        assert json.loads(f.read_text())["state"] == "open"  # the bad file is replaced by a real open state
    # a locked / unreadable file is the same direction
    f.write_text(json.dumps({"key": lb.KEY, "state": "closed"}))
    os.chmod(f, 0)
    try:
        if not os.access(f, os.R_OK):  # (root can read it anyway)
            codes.clear()
            assert await _breaker(tmp_path).admit() == (False, False)
            assert "LR-FO-LANE-A-BREAKER-READ" in codes
    finally:
        os.chmod(f, 0o600)
    # absent file: a fresh start, closed, and nothing recorded
    f.unlink()
    codes.clear()
    assert await _breaker(tmp_path).admit() == (True, False) and codes == []


async def test_unreadable_state_probes_again_after_the_cooldown(tmp_path):
    clk = Clock()
    (tmp_path / "lane_a_breaker.json").write_text("{not json")
    b = _breaker(tmp_path, clk)
    assert await b.admit() == (False, False)
    clk.t += BR.cooldown_s + 1
    assert await b.admit() == (True, True)  # half-open: one probe, then the normal rules


async def test_no_blocking_io_on_the_event_loop(tmp_path):
    """Load and flush run in worker threads; the decision path (admit after load, record_*) does no I/O."""
    main = threading.get_ident()
    seen = {}
    b = _breaker(tmp_path)
    real_read, real_write = b._read, b._write
    b._read = lambda: seen.setdefault("read", threading.get_ident()) and real_read()
    b._write = lambda snap: seen.setdefault("write", threading.get_ident()) and real_write(snap)
    await b.admit()
    for _ in range(5):
        b.record_failure("http_500")
    await b.flush()
    assert seen["read"] != main and seen["write"] != main
    assert (tmp_path / "lane_a_breaker.json").exists()
    # record_* with a loop running writes nothing synchronously
    before = (tmp_path / "lane_a_breaker.json").stat().st_mtime_ns
    b2 = _breaker(tmp_path / "other")
    b2.record_unit(False)
    assert not (tmp_path / "other").exists()  # scheduled, not yet written, and not by this thread
    await b2.flush()
    assert (tmp_path / "other" / "lane_a_breaker.json").exists() and before


# ── reading a reply (the failure feed) ───────────────────────────────────────

def _sse(events):
    return "".join("event: x\ndata: %s\n\n" % json.dumps(e) for e in events).encode()


def _good_sse(stop="end_turn", tool=None, partial=None):
    ev = [{"type": "message_start", "message": {"id": "m", "content": []}}]
    if tool:
        ev += [{"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "t", "name": tool, "input": {}}}]
        for p in partial if partial is not None else ['{"file_path": "/a.py"}']:
            ev.append({"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": p}})
    else:
        ev += [{"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
               {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hello"}}]
    ev += [{"type": "message_delta", "delta": {"stop_reason": stop}}, {"type": "message_stop"}]
    return ev


SSE = "text/event-stream"


@pytest.mark.parametrize("status,buf,ctype,kind", [
    (500, b"x", "application/json", "http_500"),
    (429, b"x", "application/json", "http_429"),
    (400, b"x", "application/json", "http_400"),
    (200, _sse(_good_sse())[:-40], SSE, lb.F_TRUNCATED),
    (200, _sse(_good_sse("max_tokens")), SSE, lb.F_TRUNCATED),
    (200, _sse([{"type": "message_start"}, {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
                {"type": "message_stop"}]), SSE, lb.F_EMPTY),
    (200, _sse(_good_sse(stop="tool_use", tool="Read", partial=['{"file_path": ', '"/a'])), SSE, lb.F_TOOL_PARSE),
    (200, _sse(_good_sse(stop="tool_use")), SSE, lb.F_TOOL_PARSE),  # says tool_use, carries none
    (200, _sse([{"type": "message_start"}, {"type": "error", "error": {"type": "overloaded_error"}}]), SSE, lb.F_STREAM),
    (200, b"not json", "application/json", lb.F_UNPARSEABLE),
    (200, json.dumps({"content": [], "stop_reason": "end_turn"}).encode(), "application/json", lb.F_EMPTY),
    (200, json.dumps({"content": [{"type": "tool_use", "id": "t", "name": "Read", "input": "x"}],
                      "stop_reason": "tool_use"}).encode(), "application/json", lb.F_TOOL_PARSE),
])
def test_read_reply_names_each_hard_failure(status, buf, ctype, kind):
    assert lb.read_reply(status, buf, ctype) == (None, kind)


def test_read_reply_accepts_good_replies_and_returns_the_tool_calls():
    msg, kind = lb.read_reply(200, _sse(_good_sse(stop="tool_use", tool="Read", partial=['{"file_path": ', '"/a.py"}'])), SSE)
    assert kind is None and msg["content"][0]["input"] == {"file_path": "/a.py"}
    msg, kind = lb.read_reply(200, _sse(_good_sse()), SSE)
    assert kind is None and msg["content"][0]["text"] == "hello"
    body = json.dumps({"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"}).encode()
    assert lb.read_reply(200, body, "application/json")[1] is None


async def test_redo_feed_goes_to_the_breaker_in_memory_and_writes_no_file(tmp_path):
    """A tool call re-issued within two calls is a redo (local_gate.FailureFeed), kept in memory."""
    rt = lb.LaneARuntime(BR, tmp_path / "lane_a_breaker.json")
    served = {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/a.py"}}]}

    def req(*calls):
        msgs = [{"role": "user", "content": "q"}]
        for i, (n, inp) in enumerate(calls):
            msgs += [{"role": "assistant", "content": [{"type": "tool_use", "id": f"u{i}", "name": n, "input": inp}]},
                     {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"u{i}", "content": "x"}]}]
        return {"messages": msgs}

    rt.feed.note_served("s", "lane_a", "haiku", served)
    rt.observe(req(("Read", {"file_path": "/a.py"})), "s")  # the results of the served step itself
    rt.observe(req(("Read", {"file_path": "/a.py"}), ("Read", {"file_path": "/a.py"})), "s")  # re-issued: redo
    assert [r for _, r in rt.breaker.units] == [True]
    assert not list(tmp_path.glob("*")) and _state_files() == {}


# ── the policy: breaker, cap, cache rule ─────────────────────────────────────

CHEAP = lambda model, tokens: 0.0  # noqa: E731


async def _decide(policy, body, sid="sess-lane-a"):
    return await policy.decide(body, sid, Stickiness(), classify=_rules())


async def test_breaker_open_keeps_the_step_pinned_with_the_reserved_code(monkeypatch, tmp_path):
    monkeypatch.setenv(lane_a.ENV_SWITCH, "on")
    policy = _policy()
    policy.lane_runtime.breaker.path = tmp_path / "b.json"
    assert (await _decide(policy, _body())).lane_a_reason == lane_a.R_SERVED
    for _ in range(5):
        policy.lane_runtime.breaker.record_failure("http_503")
    d = await _decide(policy, _body())
    assert (d.lane_a_reason, d.served_model, d.reason) == (lane_a.R_BREAKER_OPEN, OPUS, pt.REASON_CONFIG_PINNED)
    assert lane_a.R_BREAKER_OPEN == "breaker_open_reserved" and d.lane_a_conf is not None


async def test_half_open_probe_is_marked_and_only_one_is_admitted(monkeypatch, tmp_path):
    monkeypatch.setenv(lane_a.ENV_SWITCH, "on")
    clk = Clock()
    policy = _policy()
    policy.lane_runtime.breaker = _breaker(tmp_path, clk)
    for _ in range(5):
        policy.lane_runtime.breaker.record_failure("http_503")
    clk.t += BR.cooldown_s + 1
    d = await _decide(policy, _body("what does this function do?", sid="s1"), "s1")
    assert (d.lane_a_reason, d.detail) == (lane_a.R_SERVED, lane_a.DETAIL_PROBE)
    d2 = await _decide(policy, _body("what does this function do?", sid="s2"), "s2")
    assert d2.lane_a_reason == lane_a.R_BREAKER_OPEN


async def test_breaker_error_forwards(monkeypatch):
    monkeypatch.setenv(lane_a.ENV_SWITCH, "on")
    policy = _policy()

    async def boom():
        raise RuntimeError("x")
    policy.lane_runtime.breaker._ensure_loaded = boom
    d = await _decide(policy, _body())
    assert (d.lane_a_reason, d.served_model) == (lane_a.R_BREAKER_OPEN, OPUS)  # admit() fails closed


async def test_consecutive_cap_holds_for_the_turn_and_resets_on_a_new_turn(monkeypatch):
    monkeypatch.setenv(lane_a.ENV_SWITCH, "on")
    policy = _policy(LANE_ON | {"max_consecutive": 2})
    same_turn = [await _decide(policy, _body()) for _ in range(4)]
    assert [d.lane_a_reason for d in same_turn] == [lane_a.R_SERVED, lane_a.R_SERVED, lane_a.R_CONSEC_CAP, lane_a.R_CONSEC_CAP]
    assert [d.served_model for d in same_turn] == [HAIKU, HAIKU, OPUS, OPUS]
    new_turn = await _decide(policy, _body("what is a mutex?"))
    assert new_turn.lane_a_reason == lane_a.R_SERVED  # a new human prompt starts a new count


async def test_a_pinned_step_ends_the_run(monkeypatch):
    monkeypatch.setenv(lane_a.ENV_SWITCH, "on")
    policy = _policy(LANE_ON | {"max_consecutive": 2})
    await _decide(policy, _body())
    await _decide(policy, _body(prev="Edit"))  # not allowlisted: stays pinned, entry kept (no key) ...
    assert (await _decide(policy, _body())).lane_a_reason == lane_a.R_SERVED


async def test_cache_rule_blocks_an_expensive_first_switch_and_allows_a_step_already_on_haiku(monkeypatch):
    monkeypatch.setenv(lane_a.ENV_SWITCH, "on")
    policy = _policy()
    costs = iter([0.049, 9.9, 9.9])
    monkeypatch.setattr(pt, "switch_cost_usd", lambda model, tokens: next(costs))
    first = await _decide(policy, _body())
    assert first.lane_a_reason == lane_a.R_SERVED  # 0.049 < 0.05
    second = await _decide(policy, _body())  # already on Haiku this turn: no switch, the price is not asked
    assert second.lane_a_reason == lane_a.R_SERVED
    fresh = await _decide(policy, _body("what is a mutex?"))  # a new turn would re-write the prefix
    assert (fresh.lane_a_reason, fresh.served_model) == (lane_a.R_SWITCH_COST, OPUS)


async def test_cache_rule_boundary_and_unknown_price(monkeypatch):
    monkeypatch.setenv(lane_a.ENV_SWITCH, "on")
    monkeypatch.setattr(pt, "switch_cost_usd", lambda model, tokens: 0.05)
    assert (await _decide(_policy(), _body())).lane_a_reason == lane_a.R_SWITCH_COST  # not below the ceiling
    monkeypatch.setattr(pt, "switch_cost_usd", lambda model, tokens: None)
    assert (await _decide(_policy(), _body())).lane_a_reason == lane_a.R_SWITCH_COST  # unknown = conservative
    assert (await _decide(_policy(LANE_ON | {"max_switch_usd": 5.0}), _body())).lane_a_reason == lane_a.R_SWITCH_COST
    monkeypatch.setattr(pt, "switch_cost_usd", lambda model, tokens: 4.0)
    assert (await _decide(_policy(LANE_ON | {"max_switch_usd": 5.0}), _body())).lane_a_reason == lane_a.R_SERVED


async def test_cap_and_cache_rule_are_measured_with_serving_off(monkeypatch):
    """The ceiling applies to the eligible share too (lane off still says what it would do)."""
    monkeypatch.setattr(pt, "switch_cost_usd", lambda model, tokens: 9.9)
    d = await _decide(_policy(OFF_LANE), _body())
    assert d.lane_a_reason == lane_a.R_SWITCH_COST
    monkeypatch.setattr(pt, "switch_cost_usd", CHEAP)
    assert (await _decide(_policy(OFF_LANE), _body())).lane_a_reason == lane_a.R_ELIGIBLE


async def test_unserved_forgets_the_haiku_claim(monkeypatch):
    monkeypatch.setenv(lane_a.ENV_SWITCH, "on")
    policy = _policy()
    body = _body()
    assert (await _decide(policy, body)).lane_a_reason == lane_a.R_SERVED
    policy.lane_a_unserved(body, "sess-lane-a")  # the proxy retried it on the pinned model
    monkeypatch.setattr(pt, "switch_cost_usd", lambda model, tokens: 9.9)
    assert (await _decide(policy, body)).lane_a_reason == lane_a.R_SWITCH_COST


# ── D-297: the complexity_knn veto ───────────────────────────────────────────

def _knn(monkeypatch, *, needs_frontier=None, calls=None):
    async def score(text):
        if calls is not None:
            calls.append(text)
        if needs_frontier is None:
            return None
        return complexity_knn.ComplexityScore(0.9 if needs_frontier else 0.1, needs_frontier, 1.0, 0.5)
    monkeypatch.setattr(complexity_knn, "complexity_score", score)


async def test_knn_enabled_and_voting_frontier_vetoes_and_stays_pinned(monkeypatch):
    monkeypatch.setenv(lane_a.ENV_SWITCH, "on")
    calls = []
    _knn(monkeypatch, needs_frontier=True, calls=calls)
    d = await _decide(_policy(complexity_knn=True), _body())
    assert (d.lane_a_reason, d.served_model, d.reason, d.body_rewrite) == (
        lane_a.R_KNN_VETO, OPUS, pt.REASON_CONFIG_PINNED, None)
    assert d.lane_a_conf == pytest.approx(0.666667) and len(calls) == 1
    assert lane_a.R_KNN_VETO == "knn_veto"


async def test_knn_enabled_voting_cheap_or_abstaining_serves(monkeypatch):
    monkeypatch.setenv(lane_a.ENV_SWITCH, "on")
    for vote in (False, None):
        _knn(monkeypatch, needs_frontier=vote)
        d = await _decide(_policy(complexity_knn=True), _body())
        assert (d.lane_a_reason, d.served_model) == (lane_a.R_SERVED, HAIKU), vote


async def test_knn_disabled_is_no_change_and_never_consulted(monkeypatch):
    monkeypatch.setenv(lane_a.ENV_SWITCH, "on")
    calls = []
    _knn(monkeypatch, needs_frontier=True, calls=calls)  # would veto, if it were asked
    d = await _decide(_policy(complexity_knn=False), _body())
    assert (d.lane_a_reason, d.served_model) == (lane_a.R_SERVED, HAIKU) and calls == []


@pytest.mark.parametrize("why", ["yaml_off", "env_off", "env_unset", "rewrite_off"])
async def test_knn_is_never_consulted_while_the_lane_is_off(monkeypatch, why):
    """No embed or model call for the veto unless all three switches are on (and the step is otherwise eligible)."""
    calls = []
    _knn(monkeypatch, needs_frontier=True, calls=calls)
    if why == "env_off":
        monkeypatch.setenv(lane_a.ENV_SWITCH, "off")
    elif why != "env_unset":
        monkeypatch.setenv(lane_a.ENV_SWITCH, "on")
    lane = OFF_LANE if why == "yaml_off" else LANE_ON
    d = await _decide(_policy(lane, complexity_knn=True, haiku_rewrite=why != "rewrite_off"), _body())
    assert d.lane_a_reason == lane_a.R_ELIGIBLE and calls == [] and d.served_model == OPUS


async def test_knn_error_stays_pinned(monkeypatch):
    monkeypatch.setenv(lane_a.ENV_SWITCH, "on")

    async def boom(text):
        raise RuntimeError("embedder down")
    monkeypatch.setattr(complexity_knn, "complexity_score", boom)
    d = await _decide(_policy(complexity_knn=True), _body())
    assert (d.lane_a_reason, d.served_model) == (lane_a.R_ERROR, OPUS)


# ── carry-in: the gates run off the event loop ───────────────────────────────

async def test_lane_gates_run_on_worker_threads_not_the_event_loop(monkeypatch):
    """Fails if a gate that PR1 moved to ``to_thread`` is called on the loop thread again."""
    main = threading.get_ident()
    seen = {}

    def spy(name, fn):
        def wrapped(*a, **k):
            seen.setdefault(name, set()).add(threading.get_ident())
            return fn(*a, **k)
        return wrapped
    monkeypatch.setattr(pt, "_pure_rules", spy("rules", pt._pure_rules))
    monkeypatch.setattr(pt, "_pure_engine", spy("engine", pt._pure_engine))
    monkeypatch.setattr(session_kind, "kind_of", spy("kind", session_kind.kind_of))
    d = await _policy(OFF_LANE).decide(_body(), "sess-lane-a", Stickiness())  # the real classifier seam
    assert d.lane_a_reason in (lane_a.R_ELIGIBLE, lane_a.R_LOW_CONF, lane_a.R_SWITCH_COST)
    assert set(seen) == {"rules", "engine", "kind"}, seen
    for name, threads in seen.items():
        assert main not in threads, f"{name} ran on the event loop thread"


# ── the proxy: fallback, failure feed, lane off has no side effects ──────────

class Upstream:
    """Haiku answers with ``haiku`` (a status, an exception or a good reply), Opus always answers well."""

    def __init__(self, haiku=200):
        self.requests: list[httpx.Request] = []
        self.haiku = haiku

    def __call__(self, request):
        self.requests.append(request)
        model = json.loads(request.content)["model"]
        if "haiku" in model:
            if isinstance(self.haiku, Exception):
                raise self.haiku
            if self.haiku != 200:
                return httpx.Response(self.haiku, json={"type": "error", "error": {"type": "api_error", "message": "x"}})
        return httpx.Response(200, stream=httpx.ByteStream(_sse(_good_sse())), headers={"content-type": SSE})


def _lane_rows(tmp_path):
    return ledger.read_rows(tmp_path / "proxy_calls.jsonl")


@pytest.mark.parametrize("fault,kind", [(500, "http_500"), (503, "http_503"), (529, "http_529"), (429, "http_429"),
                                         (httpx.ReadTimeout("slow"), "timeout")])
async def test_5xx_429_and_timeout_retry_the_pinned_model_once(tmp_path, monkeypatch, fault, kind):
    monkeypatch.setenv(lane_a.ENV_SWITCH, "on")
    fed = []
    real = lb.LaneABreaker.record_failure
    monkeypatch.setattr(lb.LaneABreaker, "record_failure", lambda self, k, probe=False, probe_id=None: fed.append(k) or real(self, k, probe, probe_id))
    up = Upstream(fault)
    resp = await _post(_app(tmp_path, _raw(), up), _body())
    assert resp.status_code == 200
    assert [json.loads(r.content)["model"] for r in up.requests] == [HAIKU, OPUS]  # once, on the pinned model
    (row,) = _lane_rows(tmp_path)
    assert (row["lane_a_reason"], row["served_model"], row["tier_switch"]) == (lane_a.R_RETRIED, OPUS, False)
    assert row["tier_retry"]["status"] == (fault if isinstance(fault, int) else None)
    assert fed == [kind]


async def test_the_pinned_model_failing_after_the_retry_is_relayed_and_not_retried_again(tmp_path, monkeypatch):
    monkeypatch.setenv(lane_a.ENV_SWITCH, "on")
    seen = []

    def both_fail(request):
        seen.append(request)
        return httpx.Response(503, stream=httpx.ByteStream(b'{"type": "error"}'),
                              headers={"content-type": "application/json"})
    resp = await _post(_app(tmp_path, _raw(), both_fail), _body())
    assert resp.status_code == 503 and len(seen) == 2


async def test_5xx_on_a_non_lane_a_call_is_not_retried(tmp_path, monkeypatch):
    """The arm and every non-lane call keep their behaviour: only a lane A serve gains the 5xx retry."""
    monkeypatch.setenv(lane_a.ENV_SWITCH, "off")
    up = Upstream(503)
    resp = await _post(_app(tmp_path, _raw(), up), _body(model="claude-sonnet-5"))
    assert len(up.requests) == 1 and resp.status_code == 200  # sonnet is not Haiku: the fault never fires


async def test_failures_through_the_proxy_trip_the_breaker_and_the_next_step_stays_pinned(tmp_path, monkeypatch):
    monkeypatch.setenv(lane_a.ENV_SWITCH, "on")
    up = Upstream(503)
    app = _app(tmp_path, _raw(), up)
    for i in range(5):
        assert (await _post(app, _body("what does this function do?", sid=f"s{i}"))).status_code == 200
    n = len(up.requests)
    assert (await _post(app, _body("what does this function do?", sid="s9"))).status_code == 200
    assert [json.loads(r.content)["model"] for r in up.requests[n:]] == [OPUS]  # no Haiku call once open
    rows = _lane_rows(tmp_path)
    assert [r["lane_a_reason"] for r in rows] == [lane_a.R_RETRIED] * 5 + [lane_a.R_BREAKER_OPEN]
    # positive control for the side-effect tests: a lane that is ON and trips does persist, privately
    f = Path(os.environ["LLM_ROUTER_HOME"]) / "lane_a_breaker.json"
    for _ in range(100):
        if f.exists():
            break
        await asyncio.sleep(0.02)
    assert json.loads(f.read_text())["state"] == "open" and stat.S_IMODE(f.stat().st_mode) == 0o600


@pytest.mark.parametrize("reply,kind", [
    (_sse(_good_sse())[:-40], lb.F_TRUNCATED), (_sse(_good_sse(stop="tool_use")), lb.F_TOOL_PARSE),
    (_sse([{"type": "message_start"}, {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
           {"type": "message_stop"}]), lb.F_EMPTY)])
async def test_bad_haiku_replies_are_fed_to_the_breaker(tmp_path, monkeypatch, reply, kind):
    monkeypatch.setenv(lane_a.ENV_SWITCH, "on")
    fed = []
    real = lb.LaneABreaker.record_failure
    monkeypatch.setattr(lb.LaneABreaker, "record_failure", lambda self, k, probe=False, probe_id=None: fed.append(k) or real(self, k, probe, probe_id))

    def up(request):
        return httpx.Response(200, stream=httpx.ByteStream(reply), headers={"content-type": SSE})
    assert (await _post(_app(tmp_path, _raw(), up), _body())).status_code == 200
    assert fed == [kind]


async def test_good_haiku_replies_feed_no_failure_and_a_probe_success_closes(tmp_path, monkeypatch):
    monkeypatch.setenv(lane_a.ENV_SWITCH, "on")
    up = Upstream()
    app = _app(tmp_path, _raw(), up)
    assert (await _post(app, _body())).status_code == 200
    (row,) = _lane_rows(tmp_path)
    assert row["lane_a_reason"] == lane_a.R_SERVED and row["served_model"] == HAIKU
    assert not (Path(os.environ["LLM_ROUTER_HOME"]) / "lane_a_breaker.json").exists()  # nothing changed: nothing written


async def test_all_new_reason_codes_are_reachable_and_in_the_closed_set():
    assert {lane_a.R_BREAKER_OPEN, lane_a.R_SWITCH_COST, lane_a.R_CONSEC_CAP, lane_a.R_KNN_VETO} <= set(lane_a.REASONS)


async def test_lane_off_has_no_side_effects(tmp_path, monkeypatch):
    """Lane off (every way): no breaker object, no file, no task, no knn call, and the same state dir
    as a policy with no ``lane_a`` key at all (the pre-lane proxy)."""
    calls = []
    _knn(monkeypatch, needs_frontier=True, calls=calls)
    monkeypatch.setattr(lb.LaneABreaker, "admit", lambda self: (_ for _ in ()).throw(AssertionError("breaker consulted")))
    root = Path(os.environ["LLM_ROUTER_HOME"])
    sample = [_body(), _body("what is a mutex?", prev="Grep"), _body(prev="Edit"), _body(continuation=False)]
    listings = {}
    for name, lane, env in (("absent", None, None), ("yaml_off", OFF_LANE, "on"), ("env_off", LANE_ON, "off"),
                            ("env_unset", LANE_ON, None)):
        if env is None:
            monkeypatch.delenv(lane_a.ENV_SWITCH, raising=False)
        else:
            monkeypatch.setenv(lane_a.ENV_SWITCH, env)
        for p in list(root.rglob("*")):
            if p.is_file():
                p.unlink()
        policy = _policy(lane, complexity_knn=True)
        assert policy.lane_runtime is None or name in ("env_off", "env_unset")
        d = tmp_path / name
        d.mkdir()
        up = Upstream()
        app = _app(d, _raw(lane, complexity_knn=True), up)
        for b in sample:
            assert (await _post(app, copy.deepcopy(b))).status_code == 200
        await asyncio.sleep(0.05)
        listings[name] = sorted(_state_files())
        assert len(up.requests) == len(sample), name  # the scenario really ran
        assert all(json.loads(r.content)["model"] == OPUS for r in up.requests), name
    assert calls == []
    assert listings["yaml_off"] == listings["absent"] == listings["env_off"] == listings["env_unset"], listings
    assert not any("lane_a_breaker" in f for f in sum(listings.values(), []))


def test_lane_off_creates_no_breaker_object_task_or_thread():
    before = {t.ident for t in threading.enumerate()}
    for lane in (None, OFF_LANE):
        p = _policy(lane)
        assert p.lane_runtime is None
    assert {t.ident for t in threading.enumerate()} == before


# ── haiku_units (the probe feed) sees lane A continuations ───────────────────

def test_haiku_units_includes_lane_a_continuations_and_only_served_ones():
    import time as _t
    now = _t.time()

    def row(msg, step, ts, **kw):
        return {"ts": ts, "session_id": "sx", "session_kind": "organic", "msg_id": msg, "step_class": step,
                "tier": "haiku", "requested_model": OPUS, "served_model": HAIKU, "decision": "forwarded",
                "tier_reason": "haiku_rewrite", **kw}
    rows = [row("m1", "turn_first", now - 300),
            row("m2", "continuation", now - 200, lane_a_reason="served", tier_task_type="query"),
            row("m3", "continuation", now - 100, lane_a_reason="eligible"),  # stayed pinned in spirit: not served
            row("m4", "continuation", now - 50, lane_a_reason="served", served_model=OPUS)]  # retried on Opus
    units = haiku_guard.haiku_units(rows, since=now - 1000, until=now, kinds=frozenset({"organic"}))
    kinds = [u.get("lane") for u in units]
    assert kinds.count("A") == 1 and len(units) == 2, units
    assert all(u["task_type"] == "haiku" and u["lever"] == "proxy_haiku" for u in units)
    # lane off / no lane rows: exactly the old population
    old = haiku_guard.haiku_units(rows[:1] + [dict(r, lane_a_reason=None) for r in rows[1:]],
                                  since=now - 1000, until=now, kinds=frozenset({"organic"}))
    assert [u.get("lane") for u in old] == [None]


# ── carry-in: byte identity against a golden frozen from main ────────────────

def _golden_module():
    import importlib.util
    path = Path(__file__).parent / "fixtures" / "proxy" / "gen_lane_off_golden.py"
    spec = importlib.util.spec_from_file_location("gen_lane_off_golden", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


GOLDEN = Path(__file__).parent / "fixtures" / "proxy" / "golden_lane_off_main.json"


@pytest.mark.parametrize("name,lane,env", [
    ("lane_key_absent", None, "on"), ("yaml_off_env_on", OFF_LANE, "on"), ("yaml_on_env_unset", LANE_ON, None),
    ("yaml_on_env_off", LANE_ON, "off")])
async def test_lane_off_is_byte_identical_to_the_frozen_main_golden(monkeypatch, name, lane, env):
    """The baseline is NOT this branch: ``golden_lane_off_main.json`` was produced from main 224584a3 (before
    any lane A code) by ``gen_lane_off_golden.py``. Lane A not fully on must send those exact bytes upstream
    and return those exact bytes, for streaming and non-streaming, 200, 4xx, 5xx and tool_use replies."""
    if env is None:
        monkeypatch.delenv(lane_a.ENV_SWITCH, raising=False)
    else:
        monkeypatch.setenv(lane_a.ENV_SWITCH, env)
    gen = _golden_module()
    golden = json.loads(GOLDEN.read_text())
    assert sum(len(v["upstream_requests"]) for v in golden.values()) == 10  # the golden is not empty
    got = await gen.run(gen.policy(lane))
    assert got == golden, name
    models = {r["model"] for v in golden.values() for r in v["upstream_requests"]}
    assert models == {OPUS, "claude-sonnet-5"}  # nothing in the baseline was ever on Haiku


async def test_the_golden_comparison_sees_a_lane_that_serves(monkeypatch):
    """Positive control: with the lane fully on, the same replay differs from the golden."""
    monkeypatch.setenv(lane_a.ENV_SWITCH, "on")
    gen = _golden_module()
    got = await gen.run(gen.policy(LANE_ON))
    assert got != json.loads(GOLDEN.read_text())
    assert any("haiku" in r["model"] for v in got.values() for r in v["upstream_requests"])


# ── carry-in: decide-mode coverage of the shadow-row mutant ──────────────────

@pytest.mark.parametrize("mode", ["shadow", "decide"])
async def test_no_engine_or_decision_ledger_row_from_lane_gates_in_either_mode(monkeypatch, mode):
    """The #727 review: M2 (``engine_door.shadow`` or ``engine_door.decision`` re-added to the lane's
    classification) was killed only in shadow mode. Here every writer a door can use is spied on, in both
    modes, so a regression to either shows up even when it creates no file."""
    from llm_router import decision_ledger, engine_door

    monkeypatch.setenv("LLM_ROUTER_ENGINE_MODE", mode)
    hits: list = []
    for obj, name in ((engine_door, "shadow"), (engine_door, "decision"), (decision_ledger, "record"),
                      (engine, "decide")):
        real = getattr(obj, name)
        monkeypatch.setattr(obj, name, lambda *a, _n=name, _r=real, **k: hits.append(_n) or _r(*a, **k))
    policy = _policy(OFF_LANE)
    for i in range(10):
        d = await policy.decide(_body("what does function h%d do, how does it fail, and what does it return?" % i),
                                "sess-lane-a", Stickiness())
        assert d.lane_a_reason in (lane_a.R_ELIGIBLE, lane_a.R_LOW_CONF, lane_a.R_SWITCH_COST)
    assert hits == [], hits
    assert not [f for f in _state_files() if "decision" in f or "shadow" in f]
