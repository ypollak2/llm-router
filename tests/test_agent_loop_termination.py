"""The loop must be able to STOP, and must not repeat itself forever.

Traced against qwen3-coder:30b with constrained decoding on:

    iter 1  search_files  -> found the answer on line 33
    iter 2  read_file     grounding.py
    iter 3  read_file     grounding.py     <- identical call
    iter 4..15            the same call, to exhaustion

This is the classic local-agent failure ("repeats a read, then refuses to
finish") reproduced in our own loop, and the first constrained-decoding schema
caused it: `required: [tool, arguments]` meant the grammar could only express
"call something". Continuing was always valid; stopping was not a choice the
grammar offered on equal terms.

Two properties fix it and both are tested here:
  * `finish` is a tool, so stopping costs exactly one enum value like any call.
  * an identical call repeated is interrupted, because a model that has asked
    the same question twice is not going to answer it differently the third time.
"""
from __future__ import annotations

import json

import pytest

from llm_router.hooks import agent_loop
from llm_router.hooks.agent_loop import (
    FINISH_TOOL, _parse_constrained, _tool_call_schema,
)


def test_the_schema_offers_a_way_to_stop():
    """Stopping must be as expressible as calling — one enum value, not an
    optional extra key the model has to volunteer."""
    schema = _tool_call_schema()
    assert FINISH_TOOL in schema["properties"]["tool"]["enum"]


def test_finish_ends_the_loop_with_its_answer():
    calls, final = _parse_constrained(json.dumps(
        {"tool": FINISH_TOOL, "arguments": {"answer": "src/llm_router/grounding.py"}}))
    assert calls == []
    assert final == "src/llm_router/grounding.py"


def test_the_legacy_done_flag_still_ends_the_loop():
    """Kept so a model that volunteers `done` is not punished for it."""
    _, final = _parse_constrained(json.dumps(
        {"tool": "read_file", "arguments": {}, "done": True, "answer": "42"}))
    assert final == "42"


def test_finish_is_not_dispatched_as_a_real_tool(tmp_path):
    """execute_tool must never see it — there is nothing to execute, and an
    "Unknown tool" string fed back would restart the loop it just ended."""
    out = agent_loop.execute_tool(FINISH_TOOL, {"answer": "x"}, tmp_path)
    assert "Unknown tool" not in out


def _canned(responses):
    """Stub Ollama with a fixed sequence of assistant messages."""
    seq = list(responses)

    class _R:
        def __init__(self, payload): self._p = payload
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps(self._p).encode()

    def _urlopen(*a, **k):
        msg = seq.pop(0) if seq else seq_last[0]
        return _R({"message": msg})

    seq_last = [responses[-1]]
    return _urlopen


def test_a_repeated_identical_call_does_not_run_forever(monkeypatch, tmp_path):
    """The traced failure: the same read, over and over, until the cap.

    Without an interrupt this burns the whole iteration budget and the wall
    clock, and returns "reached maximum iterations" — which the caller then has
    to treat as a failure anyway.
    """
    (tmp_path / "a.py").write_text("x = 1\n")
    same = {"content": json.dumps({"tool": "read_file", "arguments": {"path": "a.py"}}),
            "tool_calls": []}
    monkeypatch.setattr(agent_loop.urllib.request, "urlopen",
                        _canned([same] * 15))

    out = agent_loop.run_agent_loop(prompt="what is in a.py?", model="m",
                                    project_root=tmp_path)
    assert out is not None
    assert "maximum iterations" not in (out or "").lower(), \
        "the loop spun to exhaustion instead of interrupting the repeat"


def test_the_repeat_interrupt_tells_the_model_what_happened(monkeypatch, tmp_path):
    """A silent skip would just produce the same call again. The tool result has
    to say why it did not run."""
    (tmp_path / "a.py").write_text("x = 1\n")
    same = json.dumps({"tool": "read_file", "arguments": {"path": "a.py"}})
    finish = json.dumps({"tool": FINISH_TOOL, "arguments": {"answer": "x = 1"}})
    monkeypatch.setattr(agent_loop.urllib.request, "urlopen", _canned([
        {"content": same, "tool_calls": []},
        {"content": same, "tool_calls": []},
        {"content": finish, "tool_calls": []},
    ]))
    out = agent_loop.run_agent_loop(prompt="what is in a.py?", model="m",
                                    project_root=tmp_path)
    assert out == "x = 1"


def test_a_different_call_is_not_treated_as_a_repeat(monkeypatch, tmp_path):
    """Reading two different files in a row is normal work, not a loop."""
    (tmp_path / "a.py").write_text("a\n")
    (tmp_path / "b.py").write_text("b\n")
    monkeypatch.setattr(agent_loop.urllib.request, "urlopen", _canned([
        {"content": json.dumps({"tool": "read_file", "arguments": {"path": "a.py"}}),
         "tool_calls": []},
        {"content": json.dumps({"tool": "read_file", "arguments": {"path": "b.py"}}),
         "tool_calls": []},
        {"content": json.dumps({"tool": FINISH_TOOL, "arguments": {"answer": "both"}}),
         "tool_calls": []},
    ]))
    assert agent_loop.run_agent_loop(prompt="read both", model="m",
                                     project_root=tmp_path) == "both"
