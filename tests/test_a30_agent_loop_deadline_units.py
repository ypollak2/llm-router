"""The hook's deadline is an INSTANT; the agent loop's is a DURATION.

Observed 2026-09-24: `UserPromptSubmit hook ... timed out after 60s — output
discarded`. The debug log shows `TOOL LOOP RESCUE` then `DIRECT: needs_tools=True`
and no terminal line — the process was killed, not finished.

auto-route passes `deadline_s=_loop_deadline()`, i.e. `time.monotonic() + 55`.
execute_agent forwarded it unchanged to run_agent_loop, which checks
`time.monotonic() - started >= deadline_s` — a duration. Under the venv's
Python 3.11 `time.monotonic()` is ~5.2M seconds, so the loop's cap was ~60 days
and only the 60s per-call urlopen timeout bounded anything.

Two properties close it, both measured on the values actually received:
  * execute_agent hands the loop the TIME LEFT, not the instant;
  * no single HTTP call may be given a timeout beyond what remains.
"""
from __future__ import annotations

import json
import time
from unittest.mock import patch

from llm_router.hooks import agent_loop
from llm_router.hooks.direct_executor import ModelSpec, execute_agent


def test_execute_agent_hands_the_loop_a_duration_not_an_instant():
    chain = [ModelSpec("ollama", "hermes3:8b")]
    # Premise: an instant from this clock is far larger than any sane duration,
    # otherwise the test below cannot tell the two apart. Offset so the premise
    # also holds on a clock that starts near zero.
    instant = time.monotonic() + 1000.0 + 20.0
    assert instant > 1000.0

    with patch("llm_router.hooks.agent_loop.run_agent_loop",
               return_value="did the task, all good") as mock_run:
        execute_agent("do something", chain, deadline_s=instant - 1000.0)

    got = mock_run.call_args.kwargs["deadline_s"]
    assert got is not None
    assert 0 < got <= 20.0, f"loop received {got!r}; expected ~20s left, not an instant"


def test_execute_agent_without_a_deadline_stays_unbounded():
    chain = [ModelSpec("ollama", "hermes3:8b")]
    with patch("llm_router.hooks.agent_loop.run_agent_loop",
               return_value="did the task, all good") as mock_run:
        execute_agent("do something", chain)
    assert mock_run.call_args.kwargs["deadline_s"] is None


def test_an_exhausted_deadline_does_not_start_the_loop():
    chain = [ModelSpec("ollama", "hermes3:8b")]
    with patch("llm_router.hooks.agent_loop.run_agent_loop",
               return_value="did the task, all good") as mock_run:
        out = execute_agent("do something", chain, deadline_s=time.monotonic() - 1.0)
    assert out is None
    assert not mock_run.called


def test_no_call_is_given_more_time_than_the_budget_has_left(monkeypatch, tmp_path):
    # A finish with no tool run is rejected by design, so read a file first.
    (tmp_path / "a.py").write_text("x = 1\n")
    replies = [
        {"tool": "read_file", "arguments": {"path": "a.py"}},
        {"tool": agent_loop.FINISH_TOOL, "arguments": {"answer": "done"}},
    ]
    timeouts: list[float] = []

    class _R:
        def __init__(self, reply): self._reply = reply
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self):
            return json.dumps({"message": {"content": json.dumps(self._reply),
                                           "tool_calls": []}}).encode()

    def _urlopen(req, timeout=None):
        timeouts.append(timeout)
        return _R(replies.pop(0))

    monkeypatch.setattr(agent_loop.urllib.request, "urlopen", _urlopen)
    out = agent_loop.run_agent_loop(prompt="x", model="m", project_root=tmp_path,
                                    timeout_per_call=60, deadline_s=5.0)
    assert out == "done"
    assert len(timeouts) == 2, f"expected two model calls, saw {timeouts}"
    assert all(t <= 5.0 for t in timeouts), f"per-call timeouts {timeouts} exceed the 5s budget"
