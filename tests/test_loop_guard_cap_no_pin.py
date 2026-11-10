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
