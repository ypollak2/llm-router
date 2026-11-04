"""A hold belongs to the main thread: a sub-agent's tool call is never held by it.

The pending route is written for the owner's prompt in the main thread. A sub-agent (Agent tool or a
Workflow ``agent()``) shares the session id but cannot satisfy that hold: the router's MCP tools are not in
its tool list (0 ``mcp__llm_router__*`` calls in 130 workflow agents, 2026-11-04). The hold could only block
it: 4 ``blocked`` rows in hold_events.jsonl in 2026-11-03T20:22Z..2026-11-04T21:44Z, all inside sub-agents.
The hook payload names a sub-agent with ``agent_id`` (text_job_binds.jsonl rows record it live).
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


def _decision(result):
    out = result.stdout.strip()
    return json.loads(out) if out else {}


def _held(result):
    d = _decision(result)
    spec = d.get("hookSpecificOutput") or {}
    return d.get("decision") == "block" or spec.get("permissionDecision") == "deny"


@pytest.mark.parametrize("hook", HOOKS, ids=["hooks", "package"])
@pytest.mark.parametrize("tool,tool_input", [
    ("Bash", {"command": "git status"}),
    ("Write", {"file_path": "x", "content": "y"}),
    ("Edit", {"file_path": "x", "old_string": "a", "new_string": "b"}),
])
def test_a_sub_agent_tool_call_is_not_held_and_the_hold_stays(tmp_path, hook, tool, tool_input):
    pending = _pending(tmp_path, "s1")
    result = _run(hook, {"session_id": "s1", "agent_id": "a123", "agent_type": "task-reviewer",
                         "tool_name": tool, "tool_input": tool_input}, tmp_path)
    assert result.returncode == 0
    assert not _held(result), result.stdout
    assert pending.exists()  # the main thread's hold is untouched


@pytest.mark.parametrize("hook", HOOKS, ids=["hooks", "package"])
def test_the_main_thread_is_still_held(tmp_path, hook):
    _pending(tmp_path, "s2")
    result = _run(hook, {"session_id": "s2", "tool_name": "Write", "tool_input": {"file_path": "x", "content": "y"}},
                  tmp_path)
    assert _held(result), result.stdout


@pytest.mark.parametrize("hook", HOOKS, ids=["hooks", "package"])
def test_agent_type_alone_is_not_a_sub_agent(tmp_path, hook):
    # `claude --agent x` runs the MAIN thread with agent_type set and no agent_id: the hold applies.
    _pending(tmp_path, "s3")
    result = _run(hook, {"session_id": "s3", "agent_type": "reviewer", "tool_name": "Write",
                         "tool_input": {"file_path": "x", "content": "y"}}, tmp_path)
    assert _held(result), result.stdout
