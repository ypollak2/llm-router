"""LLM_ROUTER_LOCAL_MAX_BLOCKS: hand a step to Claude before the content blocks added since the last Claude step
reach the cap, so the next Claude call stays inside the prompt cache's ~20-block lookback. Proxy-level, mocked
upstream and backend. Flag off: decisions identical to before (tests/test_loop_guard_cap_no_pin.py and
tests/test_proxy.py::test_loop_guard_env_defaults, which pin DEFAULT_MAX_CONSECUTIVE == 8, are unchanged)."""

from __future__ import annotations

import pytest

from llm_router.proxy import loop_guard
from llm_router.proxy.loop_guard import BlockCap
from tests import test_proxy_local_mode as T

Backend, _call, _first_call, _next_turn = T.Backend, T._call, T._first_call, T._next_turn
_ollama, _post, _served_blocks = T._ollama, T._post, T._served_blocks
env = T.env
_overrides_readable = T._overrides_readable


def _step(i):  # a text block plus a tool_use block: 2 assistant blocks, and 1 tool_result comes back = 3 per step
    return _ollama(f"step {i}", [_call("Bash", {"command": f"echo step{i}"})])


async def _drive(app, n):
    base, out, prev = _first_call(), [], None
    for _ in range(n):
        r = await _post(app, base if prev is None else _next_turn(base, prev))
        out.append(r)
        blocks = _served_blocks(r)
        prev = {"content": blocks} if blocks else prev
        if not blocks:  # Claude's reply: the stub upstream returns none; keep the last local message as history
            continue
    return out


def _app(env, monkeypatch, cap):
    if cap is None:
        monkeypatch.delenv("LLM_ROUTER_LOCAL_MAX_BLOCKS", raising=False)
    else:
        monkeypatch.setenv("LLM_ROUTER_LOCAL_MAX_BLOCKS", str(cap))
    backend = Backend(*[_step(i) for i in range(30)])
    return backend, env.app(backend, loop_max_consecutive=100)


async def test_flag_off_never_hands_off_and_writes_no_field(env, monkeypatch):
    backend, app = _app(env, monkeypatch, None)
    await _drive(app, 9)
    rows = env.rows()
    assert all(r["decision"] == "served" for r in rows) and len(backend.calls) == 9
    assert all("blocks_since_claude" not in r and "_blocks_in" not in r for r in rows)


async def test_cap_14_hands_off_at_the_right_step_with_its_reason_and_resets(env, monkeypatch):
    backend, app = _app(env, monkeypatch, 14)
    await _drive(app, 7)
    rows = env.rows()
    # 2-block replies (text + tool_use): blocks_since_claude 2, 5, 8, 11; step 5 has pending 12 and 12 + the
    # 2 reserved reply blocks reaches 14 -> hand-off, so the Claude request carries 12, never more than the cap
    assert [r["decision"] for r in rows[:4]] == ["served"] * 4
    assert [r["blocks_since_claude"] for r in rows[:4]] == [2, 5, 8, 11]
    assert (rows[4]["decision"], rows[4]["reason"]) == ("fallback", "cache_block_cap")
    assert rows[4]["detail"] == "cache_block_cap:12" and rows[4]["blocks_since_claude"] == 12
    assert rows[4]["pin_skipped"] == "cache_block_cap" and "pin_change" not in rows[4]["local_mode"]
    assert all(r["blocks_since_claude"] <= 14 for r in rows)
    assert len(backend.calls) == 6  # 4 served before the hand-off, none at it, 2 after
    # the counter reset after the Claude step: the next step is served locally again, counting from 0 + its own
    assert rows[5]["decision"] == "served" and rows[5]["blocks_since_claude"] == 3


async def test_side_call_between_main_steps_does_not_reset_the_main_counter(env, monkeypatch):
    backend, app = _app(env, monkeypatch, 14)
    base, prev = _first_call(), None
    side = {**_first_call(), "tools": []}
    side["messages"] = [{"role": "user", "content": "write a title for this chat"}]
    for i in range(5):
        r = await _post(app, base if prev is None else _next_turn(base, prev))
        blocks = _served_blocks(r)
        prev = {"content": blocks} if blocks else prev
        if i == 1:
            await _post(app, side)  # same session id, other conversation, goes to Claude
    rows = env.rows()
    main = [r for r in rows if r.get("step_class") != "side_call"]
    side_rows = [r for r in rows if r.get("step_class") == "side_call"]
    assert len(side_rows) == 1 and side_rows[0]["decision"] != "served"
    assert [r["blocks_since_claude"] for r in main[:4]] == [2, 5, 8, 11]  # not reset by the side call
    assert (main[4]["decision"], main[4]["reason"]) == ("fallback", "cache_block_cap")


def test_blockcap_counter_arithmetic():
    bc = BlockCap()
    assert bc.would_reach("s", 0, 14) is None
    assert bc.settle("s", 1, 2) == 3 and bc.settle("s", 1, 2) == 6
    assert bc.would_reach("s", 1, 9) == 7  # pending 6 + 1 = 7; 7 + 2 reserved >= 9
    assert bc.would_reach("s", 1, 10) is None
    assert bc.settle("s", 1, 2) == 9 and bc.settle("k2", 1, 2) == 3 and bc.pending("s", 0) == 9  # keys are independent
    assert bc.settle("s", 1, None) == 10  # a Claude step records what it faced (6 + 1 + 2 + 1) ...
    assert bc.pending("s", 0) == 0  # ... and resets
    assert bc.would_reach(None, 5, 3) is None and bc.would_reach("s", 5, 0) is None


def test_incoming_blocks_counts_only_tool_results_of_the_last_user_message():
    body = {"messages": [{"role": "user", "content": [{"type": "tool_result"}, {"type": "text"}]},
                         {"role": "assistant", "content": [{"type": "text"}]},
                         {"role": "user", "content": [{"type": "tool_result"}, {"type": "tool_result"}]}]}
    assert loop_guard.incoming_blocks(body) == 2
    assert loop_guard.incoming_blocks({"messages": [{"role": "user", "content": "hi"}]}) == 0
    assert loop_guard.incoming_blocks({"messages": []}) == 0 and loop_guard.incoming_blocks({}) == 0


@pytest.mark.parametrize("raw,want", [(None, 0), ("", 0), ("0", 0), ("junk", 0), ("-3", 0), ("14", 14)])
def test_local_max_blocks_parse(monkeypatch, raw, want):
    if raw is None:
        monkeypatch.delenv("LLM_ROUTER_LOCAL_MAX_BLOCKS", raising=False)
    else:
        monkeypatch.setenv("LLM_ROUTER_LOCAL_MAX_BLOCKS", raw)
    assert loop_guard.local_max_blocks() == want
