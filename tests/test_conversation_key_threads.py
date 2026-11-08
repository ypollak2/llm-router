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


def _continue(tail_text: str, parent: str = "msg_1") -> dict:
    """A Message Threads continue: only the tail after the anchor reply, no preamble, no tools."""
    return {"thread": {"type": "continue", "previous_message_id": parent},
            "messages": [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                                       "content": tail_text}]}]}


def test_a_thread_continue_keys_on_its_own_tail_not_on_the_creates_first_message():
    """Pre-existing behaviour, pinned here: a continue carries no first user turn, so conversation_key
    (which local_step calls on the unstripped key_body, server.py:682/:1308) hashes the tail's first
    message. It differs from the create's key and from another tail's, and is stable for the same
    tail and when only a cache_control marker moves. A continue is therefore never matched to the
    create's pin; local_step reads it as started-elsewhere (never served locally), the safe side."""
    create = _body("brief A")
    create["thread"] = {"type": "create"}
    k_create = conversation_key(create, SID)
    k_cont = conversation_key(_continue("out 1"), SID)
    assert k_cont != k_create
    assert k_cont == conversation_key(_continue("out 1", parent="msg_2"), SID)
    assert k_cont != conversation_key(_continue("out 2"), SID)
    marked = _continue("out 1")
    marked["messages"][0]["content"][0]["cache_control"] = {"type": "ephemeral"}
    assert conversation_key(marked, SID) == k_cont
