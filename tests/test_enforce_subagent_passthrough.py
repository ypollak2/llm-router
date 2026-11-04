"""A hold belongs to the main thread: outside strict, a sub-agent's tool call is never denied by it.

The pending route is written for the owner's prompt in the main thread. A sub-agent shares the session id. A Workflow
agent made 0 routing-tool calls in 142 transcripts (2026-11-04), so a denial could only stop its work. Its calls keep counting towards
the trap, loop and count releases, so the main thread's hold is released when it was before this rule: removing them from
the count would leave the main thread held for longer (review of the first version of this change).
The hook payload names a sub-agent with ``agent_id``; ``agent_type`` alone is the main thread under ``claude --agent``.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOKS = [ROOT / "hooks" / "enforce-route.py", ROOT / "src" / "llm_router" / "hooks" / "enforce-route.py"]
SUB = {"agent_id": "a123", "agent_type": "task-reviewer"}
WRITE = {"tool_name": "Write", "tool_input": {"file_path": "x", "content": "y"}}
BASH = {"tool_name": "Bash", "tool_input": {"command": "git commit -m x"}}


def _run(hook, payload, home, mode="hard"):
    env = {k: v for k, v in os.environ.items() if k not in ("LLM_ROUTER_ENFORCE", "LLM_ROUTER_SLIM")}
    env.update(HOME=str(home), LLM_ROUTER_HOME=str(home / ".llm-router"), LLM_ROUTER_ENFORCE=mode,
               PYTHONPATH=str(ROOT / "src"))
    return subprocess.run([sys.executable, str(hook)], input=json.dumps(payload), capture_output=True, text=True,
                          env=env, timeout=60)


def _pending(home, sid):
    state = home / ".llm-router"
    state.mkdir(parents=True, exist_ok=True)
    path = state / f"pending_route_{sid}.json"
    path.write_text(json.dumps({
        "expected_tool": "llm_query", "task_type": "query", "complexity": "simple", "method": "heuristic",
        "issued_at": time.time(), "turn_id": 1, "session_id": sid, "original_prompt": "What is the capital of France?",
    }))
    return path


def _denied(result):
    out = result.stdout.strip()
    d = json.loads(out) if out else {}
    spec = d.get("hookSpecificOutput") or {}
    return d.get("decision") == "block" or spec.get("permissionDecision") == "deny"


@pytest.fixture(params=HOOKS, ids=["hooks", "package"])
def hook(request):
    return request.param


@pytest.mark.parametrize("mode", ["hard", "smart"])
@pytest.mark.parametrize("call", [
    BASH, WRITE, {"tool_name": "Edit", "tool_input": {"file_path": "x", "old_string": "a", "new_string": "b"}},
], ids=["Bash", "Write", "Edit"])
def test_a_sub_agent_call_is_not_denied_and_the_hold_stays(tmp_path, hook, mode, call):
    pending = _pending(tmp_path, "s1")
    result = _run(hook, {"session_id": "s1", **SUB, **call}, tmp_path, mode)
    assert result.returncode == 0
    assert not _denied(result), result.stdout
    assert pending.exists()


def test_the_main_thread_is_still_held_after_one_call(tmp_path, hook):
    pending = _pending(tmp_path, "s2")
    assert _denied(_run(hook, {"session_id": "s2", **WRITE}, tmp_path))
    assert pending.exists()


def test_agent_type_alone_is_the_main_thread(tmp_path, hook):
    _pending(tmp_path, "s3")
    assert _denied(_run(hook, {"session_id": "s3", "agent_type": "reviewer", **WRITE}, tmp_path))


def test_sub_agent_calls_still_count_towards_the_trap_release(tmp_path, hook):
    # Two same-tool calls in one turn release the hold, whoever made them: this is the release the main thread relied on.
    pending = _pending(tmp_path, "s4")
    assert not _denied(_run(hook, {"session_id": "s4", **SUB, **WRITE}, tmp_path))
    assert pending.exists()
    assert not _denied(_run(hook, {"session_id": "s4", **SUB, **WRITE}, tmp_path))
    assert not pending.exists()
    assert not _denied(_run(hook, {"session_id": "s4", **WRITE}, tmp_path))


def test_a_sub_agent_call_then_the_same_tool_in_the_main_thread_releases_as_before(tmp_path, hook):
    pending = _pending(tmp_path, "s5")
    assert not _denied(_run(hook, {"session_id": "s5", **SUB, **WRITE}, tmp_path))
    assert not _denied(_run(hook, {"session_id": "s5", **WRITE}, tmp_path))
    assert not pending.exists()


def test_strict_keeps_its_denials_for_sub_agents(tmp_path, hook):
    pending = _pending(tmp_path, "s6")
    for _ in range(2):
        assert _denied(_run(hook, {"session_id": "s6", **SUB, **WRITE}, tmp_path, "strict"))
    assert pending.exists()


def test_the_pass_is_logged(tmp_path, hook):
    _pending(tmp_path, "s7")
    _run(hook, {"session_id": "s7", **SUB, **WRITE}, tmp_path)
    assert "ALLOWED(subagent)" in (tmp_path / ".llm-router" / "enforcement.log").read_text()


def test_sub_agent_calls_still_count_towards_the_count_release(tmp_path, hook):
    # Four held calls of different tools: no same-tool block in one turn, so only the count valve can release at the 4th.
    pending = _pending(tmp_path, "s8")
    calls = [WRITE, {"tool_name": "Edit", "tool_input": {"file_path": "x", "old_string": "a", "new_string": "b"}},
             {"tool_name": "MultiEdit", "tool_input": {"file_path": "x", "edits": []}}, BASH]
    for call in calls[:3]:
        assert not _denied(_run(hook, {"session_id": "s8", **SUB, **call}, tmp_path))
        assert pending.exists()
    assert not _denied(_run(hook, {"session_id": "s8", **SUB, **calls[3]}, tmp_path))
    assert not pending.exists()
    assert "AUTO-PIVOT (count)" in (tmp_path / ".llm-router" / "enforcement.log").read_text()


def test_sub_agent_calls_still_count_towards_the_loop_release(tmp_path, hook):
    # The same tool over three turns (turn_id moves, so the per-turn trap never fires): only the loop valve can release.
    pending = _pending(tmp_path, "s9")
    for turn in (1, 2):
        data = json.loads(pending.read_text())
        data["turn_id"] = turn
        pending.write_text(json.dumps(data))
        assert not _denied(_run(hook, {"session_id": "s9", **SUB, **WRITE}, tmp_path))
        assert pending.exists()
    data = json.loads(pending.read_text())
    data["turn_id"] = 3
    pending.write_text(json.dumps(data))
    assert not _denied(_run(hook, {"session_id": "s9", **SUB, **WRITE}, tmp_path))
    assert not pending.exists()
    assert "AUTO-PIVOT (loop)" in (tmp_path / ".llm-router" / "enforcement.log").read_text()
