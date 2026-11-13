"""usage-refresh must act on Claude Code's snake_case PostToolUse payload.

It read only camelCase `toolName`, so on Claude Code (`tool_name`) it exited 0 and
did nothing: no savings-log row, no stale-usage hint. These tests run the REAL hook
script in a subprocess against a scratch LLM_ROUTER_HOME.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / "src" / "llm_router" / "hooks" / "usage-refresh.py"
TOOL = "mcp__llm_router__llm_query"


def _run(tmp_path: Path, payload: dict) -> list[dict]:
    home = tmp_path / "lr"
    home.mkdir()
    env = {**os.environ, "LLM_ROUTER_HOME": str(home), "HOME": str(tmp_path)}
    r = subprocess.run(
        [sys.executable, str(HOOK)], input=json.dumps(payload), text=True,
        capture_output=True, env=env, timeout=60,
    )
    assert r.returncode == 0, r.stderr
    log = home / "savings_log.jsonl"
    return [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []


@pytest.mark.parametrize("key", ["tool_name", "toolName"])
def test_hook_acts_on_both_payload_shapes(tmp_path, key):
    rows = _run(tmp_path, {key: TOOL, "session_id": "s1", "hook_event_name": "PostToolUse"})
    assert len(rows) == 1, f"{key} payload produced {len(rows)} savings rows"
    assert rows[0]["tool"] == TOOL and rows[0]["task_type"] == "query"


def test_snake_case_wins_over_camel_case(tmp_path):
    rows = _run(tmp_path, {"tool_name": TOOL, "toolName": "Bash"})
    assert [r["tool"] for r in rows] == [TOOL]


def test_non_routing_snake_case_tool_is_ignored(tmp_path):
    assert _run(tmp_path, {"tool_name": "Bash"}) == []
