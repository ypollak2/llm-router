"""PR-A: ``LLM_ROUTER_CLAUDE_MARKER`` keeps marked conversations off the local model."""

from __future__ import annotations

import pytest

from llm_router.proxy import local_mode

ENV = "LLM_ROUTER_CLAUDE_MARKER"
M = local_mode.CLAUDE_MARKER


def _body(first, later=None):
    msgs = [{"role": "user", "content": first}]
    if later is not None:
        msgs += [{"role": "assistant", "content": "ok"}, {"role": "user", "content": later}]
    return {"messages": msgs}


def _decide(body):
    return local_mode.decide_local(body, local_mode.LocalSession())


@pytest.mark.parametrize("flag,first,later,blocked", [
    ("1", f"review this {M}", None, True),
    ("1", "review this", None, False),
    ("0", f"review this {M}", None, False),
    (None, f"review this {M}", None, False),
    ("1", "review this", f"now {M}", False),
    ("1", [{"type": "text", "text": "a"}, {"type": "text", "text": f"b {M}"}], None, True),
    ("1", [{"type": "text", "text": "plain"}], None, False),
    ("1", "[[llm-router:claude]]", None, False),
])
def test_marker(monkeypatch, flag, first, later, blocked):
    if flag is None:
        monkeypatch.delenv(ENV, raising=False)
    else:
        monkeypatch.setenv(ENV, flag)
    d = _decide(_body(first, later))
    assert (d.reason == local_mode.REASON_CLAUDE_MARKER) is blocked


def test_marker_beats_exempt_and_pin_but_not_kill(monkeypatch):
    monkeypatch.setenv(ENV, "1")
    b = _body(M)
    s = local_mode.LocalSession(exempt="subagent_thread", pinned=local_mode.PIN_CLAUDE)
    assert local_mode.decide_local(b, s).reason == "claude_marker"
    assert local_mode.decide_local(b, local_mode.LocalSession(kill_switch=True)).reason == "kill_switch"


def test_first_user_message_need_not_be_messages_0(monkeypatch):
    monkeypatch.setenv(ENV, "1")
    body = {"messages": [{"role": "assistant", "content": "hi"}, {"role": "user", "content": M}]}
    assert _decide(body).reason == local_mode.REASON_CLAUDE_MARKER


def test_first_user_message_with_tool_result_blocks_first(monkeypatch):
    monkeypatch.setenv(ENV, "1")
    blocks = [{"type": "tool_result", "tool_use_id": "t", "content": f"x {M}"}, {"type": "text", "text": M}]
    assert _decide(_body(blocks)).reason == local_mode.REASON_CLAUDE_MARKER
    only_result = [{"type": "tool_result", "tool_use_id": "t", "content": M}]
    assert _decide(_body(only_result)).reason != local_mode.REASON_CLAUDE_MARKER


def test_compaction_changes_the_conversation_key_so_no_pin_can_cover_it(monkeypatch):
    """Documents the residual leak: the pin key hashes the first message, so a history whose marked
    first message is gone is a different conversation and the marker is not seen."""
    from llm_router.proxy.cache_cost import conversation_key
    monkeypatch.setenv(ENV, "1")
    marked = _body(f"task {M}", "next")
    compacted = {"messages": [{"role": "user", "content": "summary of earlier work"}]}
    assert conversation_key(marked, "s") != conversation_key(compacted, "s")
    assert _decide(compacted).reason != local_mode.REASON_CLAUDE_MARKER
