"""LLM_ROUTER_LOOP_GUARD_CAP_NO_PIN: a loop_guard CAP trip hands one step to Claude without pinning the
session; a REPEAT trip (and every other failure) still pins. Proxy-level, mocked upstream and backend."""

from __future__ import annotations

import json

import pytest

from llm_router.proxy import loop_guard
from tests import test_proxy_local_mode as T

Backend, _call, _first_call, _next_turn = T.Backend, T._call, T._first_call, T._next_turn
_ollama, _post, _served_blocks = T._ollama, T._post, T._served_blocks
# the harness fixtures, reused as-is (assignment, not import, so pytest finds them here)
env = T.env
_overrides_readable = T._overrides_readable

CAP = 2
SECRET = "SECRET-USER-TEXT-9f3a"


def _bash(i):
    return _ollama("", [_call("Bash", {"command": f"echo step{i}"})])


async def _drive(app, n, base=None):
    """n turns of one conversation; returns the responses."""
    base = base or _first_call()
    out, prev = [], None
    for _ in range(n):
        body = base if prev is None else _next_turn(base, prev)
        r = await _post(app, body)
        out.append(r)
        blocks = _served_blocks(r)
        prev = {"content": blocks} if blocks else prev
    return out


@pytest.fixture
def cap_app(env):
    def make(flag, monkeypatch):
        if flag:
            monkeypatch.setenv("LLM_ROUTER_LOOP_GUARD_CAP_NO_PIN", "1")
        else:
            monkeypatch.delenv("LLM_ROUTER_LOOP_GUARD_CAP_NO_PIN", raising=False)
        backend = Backend(*[_bash(i) for i in range(20)])
        return backend, env.app(backend, loop_max_consecutive=CAP)
    return make


async def test_flag_off_cap_trip_pins_claude(env, cap_app, monkeypatch):
    backend, app = cap_app(False, monkeypatch)
    await _drive(app, CAP + 2)
    rows = env.rows()
    assert [r["reason"] for r in rows[CAP:]] == ["loop_guard", "pinned_claude"]
    assert rows[CAP]["local_mode"]["pin_change"] == "local->claude"
    assert "pin_skipped" not in rows[CAP]
    assert len(backend.calls) == CAP  # never tried locally again


async def test_flag_on_cap_trip_falls_back_one_step_then_serves_local_again(env, cap_app, monkeypatch):
    backend, app = cap_app(True, monkeypatch)
    await _drive(app, 2 * CAP + 2)
    rows = env.rows()
    assert all(r["decision"] == "served" for r in rows[:CAP])
    assert (rows[CAP]["decision"], rows[CAP]["reason"]) == ("fallback", "loop_guard")
    assert rows[CAP]["pin_skipped"] == "loop_guard_cap" and rows[CAP]["loop_guard_trigger"] == "cap"
    assert "pin_change" not in rows[CAP]["local_mode"]
    # fresh budget: CAP more served steps, then the cap trips again (cycle)
    assert all(r["decision"] == "served" for r in rows[CAP + 1:2 * CAP + 1])
    assert rows[2 * CAP + 1]["reason"] == "loop_guard" and rows[2 * CAP + 1]["pin_skipped"] == "loop_guard_cap"
    assert len(backend.calls) == 2 * CAP


async def test_flag_on_repeat_trip_still_pins(env, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_LOOP_GUARD_CAP_NO_PIN", "1")
    same = _ollama("", [_call("Bash", {"command": SECRET})])
    backend = Backend(same, same, same)
    app = env.app(backend, loop_max_consecutive=50)
    await _drive(app, 3)
    rows = env.rows()
    assert rows[1]["reason"] == "loop_guard" and rows[1]["loop_guard_trigger"] == "repeat"
    assert rows[1]["local_mode"]["pin_change"] == "local->claude" and "pin_skipped" not in rows[1]
    assert rows[2]["reason"] == "pinned_claude"
    assert len(backend.calls) == 2


async def test_repeat_row_carries_hash_prefix_never_the_raw_input(env):
    same = _ollama("", [_call("Bash", {"command": SECRET})])
    app = env.app(Backend(same, same), loop_max_consecutive=50)
    await _drive(app, 2)
    row = env.rows()[1]
    assert row["detail"] == "repeat:Bash"
    sig = row["loop_guard_sig"]
    assert len(sig) == 10 and int(sig, 16) >= 0
    assert SECRET not in json.dumps(row)


def test_repeat_hit_hash_is_stable_and_none_when_new():
    g = loop_guard.LoopGuard(max_consecutive=100, repeat_window=3)
    msg = {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": SECRET}}]}
    assert g.repeat_hit("s", msg) is None
    g.record_served("s", msg)
    name, h = g.repeat_hit("s", msg)
    assert name == "Bash" and len(h) == 10 and SECRET not in h
    assert g.repeat_reason("s", msg) == "repeat:Bash"


async def test_flag_on_period4_cycle_is_pinned_at_the_second_cap_trip(env, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_LOOP_GUARD_CAP_NO_PIN", "1")
    cycle = [_ollama("", [_call("Bash", {"command": f"echo {c}"})]) for c in "abcd"]
    backend = Backend(*(cycle * 6))
    app = env.app(backend, loop_max_consecutive=4)
    await _drive(app, 4 + 1 + 4 + 1 + 1)
    rows = env.rows()
    assert rows[4]["loop_guard_trigger"] == "cap" and rows[4]["pin_skipped"] == "loop_guard_cap"
    assert rows[9]["loop_guard_trigger"] == "cap_cycle" and "pin_skipped" not in rows[9]
    assert rows[9]["local_mode"]["pin_change"] == "local->claude"
    assert rows[10]["reason"] == "pinned_claude"


async def test_flag_on_progressing_calls_are_never_pinned_across_three_cap_trips(env, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_LOOP_GUARD_CAP_NO_PIN", "1")
    backend = Backend(*[_bash(i) for i in range(30)])
    app = env.app(backend, loop_max_consecutive=2)
    await _drive(app, 3 * 3 + 1)
    rows = env.rows()
    trips = [r for r in rows if r["reason"] == "loop_guard"]
    assert len(trips) == 3 and all(r["loop_guard_trigger"] == "cap" and r["pin_skipped"] for r in trips)
    assert not any(r["reason"] == "pinned_claude" for r in rows)


async def test_flag_on_period12_cycle_with_cap8_is_pinned_within_three_trips(env, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_LOOP_GUARD_CAP_NO_PIN", "1")
    backend = Backend(*[_bash(i % 12) for i in range(60)])
    app = env.app(backend, loop_max_consecutive=8)
    await _drive(app, 3 * 9 + 1)
    rows = env.rows()
    trips = [r for r in rows if r["reason"] == "loop_guard"]
    assert 2 <= len(trips) <= 3 and trips[-1]["loop_guard_trigger"] == "cap_cycle"
    assert "pin_skipped" not in trips[-1] and rows[rows.index(trips[-1]) + 1]["reason"] == "pinned_claude"


async def test_flag_on_disjoint_ab_alternation_is_pinned(env, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_LOOP_GUARD_CAP_NO_PIN", "1")
    a = [_bash(f"a{i}") for i in range(4)]
    b = [_bash(f"b{i}") for i in range(4)]
    backend = Backend(*(a + b + a + b))
    app = env.app(backend, loop_max_consecutive=4)
    await _drive(app, 3 * 5 + 1)
    trips = [r for r in env.rows() if r["reason"] == "loop_guard"]
    assert [t["loop_guard_trigger"] for t in trips] == ["cap", "cap", "cap_cycle"]


async def test_flag_on_progressing_calls_are_never_pinned_across_four_cap_trips(env, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_LOOP_GUARD_CAP_NO_PIN", "1")
    app = env.app(Backend(*[_bash(i) for i in range(40)]), loop_max_consecutive=3)
    await _drive(app, 4 * 4 + 1)
    rows = env.rows()
    assert len([r for r in rows if r["reason"] == "loop_guard"]) == 4
    assert not any(r["reason"] == "pinned_claude" for r in rows)


def _call_msg(c):
    return {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": c}}]}


def _window(g, cmds):
    for c in cmds:
        g.record_served("s", _call_msg(c), track_window=True)
    return g.cap_trip("s")


def test_cap_trip_rules_overlap_min_sigs_and_text_only_keeps_history():
    g = loop_guard.LoopGuard(max_consecutive=4, repeat_window=0)
    assert _window(g, "abcd") == "cap"
    # text-only window: no verdict, history kept
    g.record_served("s", {"content": [{"type": "text", "text": "hi"}]}, track_window=True)
    assert g.cap_trip("s") == "cap"
    assert _window(g, "abcd") == "cap_cycle"  # cycle, text-only, cycle is still caught
    g2 = loop_guard.LoopGuard(max_consecutive=4, repeat_window=0)
    assert _window(g2, "abcd") == "cap"
    assert _window(g2, "ab") == "cap"  # fewer than CYCLE_MIN_SIGS: no verdict, even at 100% overlap
    assert _window(g2, "wxyz") == "cap"  # 0% overlap


def test_flag_off_window_sigs_is_not_populated():
    g = loop_guard.LoopGuard(max_consecutive=4, repeat_window=0)
    g.record_served("s", _call_msg("a"))
    assert not g._sessions["s"].window_sigs and not g._prev_window
    g.record_served("s", _call_msg("b"), track_window=True)
    assert g._sessions["s"].window_sigs.maxlen == 8
