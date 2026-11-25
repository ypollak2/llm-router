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
    # blocks_since_claude: 2, 5, 8, 11, 14 -> at step 5 (rows[4]) pending=14, would reach cap=14 -> fallback
    assert [r["decision"] for r in rows[:5]] == ["served"] * 5
    assert [r["blocks_since_claude"] for r in rows[:5]] == [2, 5, 8, 11, 14]
    assert (rows[5]["decision"], rows[5]["reason"]) == ("fallback", "cache_block_cap")
    assert rows[5]["detail"] == "cache_block_cap:15" and rows[5]["blocks_since_claude"] == 15
    assert rows[5]["pin_skipped"] == "cache_block_cap" and "pin_change" not in rows[5]["local_mode"]
    assert len(backend.calls) == 6  # 5 served before the hand-off, none at it, 1 after
    # the counter reset after the Claude step: the next step is served locally again, counting from 0 + its own
    assert rows[6]["decision"] == "served" and rows[6]["blocks_since_claude"] == 3


def test_blockcap_counter_arithmetic():
    bc = BlockCap()
    assert bc.would_reach("s", 0, 14) is None
    assert bc.settle("s", 1, 2) == 3 and bc.settle("s", 1, 2) == 6
    assert bc.would_reach("s", 1, 8) == 7  # pending 6 + 1 = 7; 7 + 1 >= 8
    assert bc.would_reach("s", 1, 9) is None
    assert bc.settle("s", 1, None) == 7  # a Claude step records what it faced ...
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
