"""docs/BUGS.md AGENT-EMPTY-1: an empty 200 reply with no done_reason is a backend failure."""
from __future__ import annotations

import json

import pytest

from llm_router.hooks import agent_loop
from llm_router.tools import local_task as lt

pytestmark = pytest.mark.usefixtures("open_url_via_urlopen")


def _fake(monkeypatch, bodies):
    calls = []

    class _R:
        def __init__(self, p): self._p = p
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps(self._p).encode()

    def _urlopen(*a, **k):
        calls.append(1)
        return _R(bodies[min(len(calls), len(bodies)) - 1])

    monkeypatch.setattr(agent_loop.urllib.request, "urlopen", _urlopen)
    return calls


BAD = {"model": "m", "created_at": "t", "message": {"role": "assistant", "content": ""}, "done": True}
GOOD_EMPTY = {"model": "m", "message": {"role": "assistant", "content": ""}, "done": True,
              "done_reason": "stop", "eval_count": 1}


def test_empty_reply_without_done_reason_is_a_backend_error(monkeypatch, tmp_path):
    calls = _fake(monkeypatch, [BAD])
    out = agent_loop.run_agent_loop(prompt="p", model="m", project_root=tmp_path, read_only=True)
    assert out is None
    assert agent_loop.last_end_reason() == agent_loop.BACKEND_EMPTY_REPLY
    assert len(calls) == 2, "exactly one retry"


def test_retry_that_succeeds_returns_the_answer(monkeypatch, tmp_path):
    ok = {"message": {"role": "assistant", "content": "hello"}, "done": True, "done_reason": "stop"}
    _fake(monkeypatch, [BAD, ok])
    out = agent_loop.run_agent_loop(prompt="p", model="m", project_root=tmp_path, read_only=True)
    assert out == "hello"
    assert agent_loop.last_end_reason() is None


def test_genuine_empty_reply_with_done_reason_is_unchanged(monkeypatch, tmp_path):
    calls = _fake(monkeypatch, [GOOD_EMPTY])
    out = agent_loop.run_agent_loop(prompt="p", model="m", project_root=tmp_path, read_only=True)
    assert out is None
    assert agent_loop.last_end_reason() is None
    assert len(calls) == 1, "no retry for a genuine empty answer"


def test_no_retry_without_budget(monkeypatch, tmp_path):
    calls = _fake(monkeypatch, [BAD])
    agent_loop.run_agent_loop(prompt="p", model="m", project_root=tmp_path,
                              read_only=True, deadline_s=3.0)
    assert len(calls) == 1
    assert agent_loop.last_end_reason() == agent_loop.BACKEND_EMPTY_REPLY


def test_local_task_surfaces_backend_error(monkeypatch, tmp_path):
    _fake(monkeypatch, [BAD])
    monkeypatch.setattr(lt, "_take_state", lambda root, like=None: {"kind": "none", "files": {}})
    res = lt._run_task_serial(agent_loop.run_agent_loop, "do it", "m", tmp_path, 60.0, False, None)
    report, error, status = res[0], res[1], res[5]
    assert status == lt.BACKEND_ERROR
    assert report is None
    assert "done_reason" in error and "out of memory" in error
