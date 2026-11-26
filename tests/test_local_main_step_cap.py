"""LLM_ROUTER_LOCAL_MAIN_MAX_STEPS (D-640): a MAIN-thread local run is handed back to Claude after N consecutive
local steps, with reason main_step_cap and no pin; sub-agent threads keep the loop guard cap. Proxy-level, mocked
upstream and backend. Flag off: decisions identical to before (tests/test_proxy_off_golden.py)."""

from __future__ import annotations

import pytest

from llm_router.proxy import loop_guard
from tests import test_proxy_local_mode as T

Backend, _call, _first_call, _next_turn = T.Backend, T._call, T._first_call, T._next_turn
_ollama, _post, _served_blocks = T._ollama, T._post, T._served_blocks
env = T.env
_overrides_readable = T._overrides_readable


def _step(i):
    return _ollama(f"step {i}", [_call("Bash", {"command": f"echo step{i}"})])


async def _drive(app, n, base):
    out, prev = [], None
    for _ in range(n):
        r = await _post(app, base if prev is None else _next_turn(base, prev))
        out.append(r)
        blocks = _served_blocks(r)
        prev = {"content": blocks} if blocks else prev
    return out


def _app(env, monkeypatch, cap, guard=100):
    if cap is None:
        monkeypatch.delenv("LLM_ROUTER_LOCAL_MAIN_MAX_STEPS", raising=False)
    else:
        monkeypatch.setenv("LLM_ROUTER_LOCAL_MAIN_MAX_STEPS", str(cap))
    backend = Backend(*[_step(i) for i in range(30)])
    return backend, env.app(backend, loop_max_consecutive=guard)


async def test_flag_off_never_hands_off(env, monkeypatch):
    backend, app = _app(env, monkeypatch, None)
    await _drive(app, 9, _first_call())
    rows = env.rows()
    assert all(r["decision"] == "served" for r in rows) and len(backend.calls) == 9
    assert all("main_step_cap" not in r for r in rows)


async def test_main_thread_hands_back_after_5_local_steps_without_a_pin(env, monkeypatch):
    backend, app = _app(env, monkeypatch, 5)
    await _drive(app, 8, _first_call())
    rows = env.rows()
    assert all(r["is_main_thread"] is True for r in rows)
    assert [r["decision"] for r in rows[:5]] == ["served"] * 5
    assert (rows[5]["decision"], rows[5]["reason"]) == ("fallback", "main_step_cap")
    assert rows[5]["detail"] == "main_step_cap:5" and rows[5]["main_step_cap"] == 5
    assert rows[5]["pin_skipped"] == "main_step_cap" and "pin_change" not in rows[5]["local_mode"]
    assert len(backend.calls) == 7  # 5 served, none at the hand-back, 2 after: the next step re-decides locally
    assert rows[6]["decision"] == "served"


async def test_a_lower_loop_guard_cap_still_wins(env, monkeypatch):
    backend, app = _app(env, monkeypatch, 5, guard=3)
    await _drive(app, 5, _first_call())
    rows = env.rows()
    assert (rows[3]["decision"], rows[3]["reason"]) == ("fallback", "loop_guard")


async def test_subagent_thread_is_unaffected(env, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_EXEMPT", "none")
    base = _first_call()
    base["tools"] = [t for t in base["tools"] if t["name"] != "Agent"]
    backend, app = _app(env, monkeypatch, 2)
    await _drive(app, 6, base)
    rows = env.rows()
    assert all(r["is_main_thread"] is False for r in rows)
    assert all(r.get("reason") != "main_step_cap" for r in rows)
    assert sum(r["decision"] == "served" for r in rows) >= 4  # the 2-step main cap did not cut the run


@pytest.mark.parametrize("raw,want", [(None, 0), ("", 0), ("0", 0), ("junk", 0), ("-3", 0), ("5", 5)])
def test_parse(monkeypatch, raw, want):
    if raw is None:
        monkeypatch.delenv("LLM_ROUTER_LOCAL_MAIN_MAX_STEPS", raising=False)
    else:
        monkeypatch.setenv("LLM_ROUTER_LOCAL_MAIN_MAX_STEPS", raw)
    assert loop_guard.local_main_max_steps() == want
