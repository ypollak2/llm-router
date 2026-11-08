"""D-445: every thread of a Claude Code session opens with the same long preamble (CLAUDE.md,
memory index). conversation_key hashed only the first 8000 chars of it, so the main thread and
its sub-agents shared one key, and so one local pin (2026-11-08: 97 sub-agent rows served
``pinned_claude`` off the main thread's pin). Bodies here are synthetic."""
from __future__ import annotations

import json

from llm_router.proxy.cache_cost import conversation_key

PREAMBLE = "<system-reminder>shared claude.md and memory index line. " * 400  # > 8000 chars
SID = "sess-1"


def _body(brief: str, *, turns: int = 1, cache_marker: bool = False) -> dict:
    block = {"type": "text", "text": brief}
    if cache_marker:
        block["cache_control"] = {"type": "ephemeral"}
    msgs = [{"role": "user", "content": [{"type": "text", "text": PREAMBLE}, block]}]
    for i in range(turns - 1):
        msgs += [{"role": "assistant", "content": [{"type": "text", "text": f"step {i}"}]},
                 {"role": "user", "content": [{"type": "text", "text": f"result {i}"}]}]
    return {"messages": msgs}


def test_preamble_is_longer_than_the_old_prefix():
    assert len(json.dumps(_body("x")["messages"][0]["content"])) > 8000


def test_threads_sharing_a_long_preamble_get_different_keys():
    assert conversation_key(_body("brief A"), SID) != conversation_key(_body("brief B"), SID)


def test_one_thread_keeps_one_key_over_turns_and_cache_marker_moves():
    k1 = conversation_key(_body("brief A", turns=1, cache_marker=True), SID)
    assert k1 == conversation_key(_body("brief A", turns=2), SID) == conversation_key(_body("brief A", turns=5), SID)
    assert conversation_key(_body("brief A"), SID) != conversation_key(_body("brief A"), "sess-2")
