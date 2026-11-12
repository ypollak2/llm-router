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
