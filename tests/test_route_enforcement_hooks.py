"""Tests for routing enforcement behavior in the shipped hook scripts."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AUTO_ROUTE_HOOK = ROOT / "src" / "llm_router" / "hooks" / "auto-route.py"
ENFORCE_ROUTE_HOOK = ROOT / "src" / "llm_router" / "hooks" / "enforce-route.py"


def _run_hook(
    hook_path: Path,
    payload: dict,
    *,
    home: Path,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "HOME": str(home)}
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(hook_path)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )


def _write_pending(home: Path, session_id: str, **overrides) -> Path:
    router_dir = home / ".llm-router"
    router_dir.mkdir(parents=True, exist_ok=True)
    pending_path = router_dir / f"pending_route_{session_id}.json"
    data = {
        "expected_tool": "llm_query",
        "task_type": "query",
        "complexity": "simple",
        "issued_at": time.time(),
        "session_id": session_id,
    }
    data.update(overrides)
    pending_path.write_text(json.dumps(data), encoding="utf-8")
    return pending_path


def test_enforce_route_blocks_work_tools_by_default(tmp_path):
    """Hard enforcement is the default when no env override is provided."""
    session_id = "sess-hard-default"
    _write_pending(tmp_path, session_id)

    result = _run_hook(
        ENFORCE_ROUTE_HOOK,
        {"session_id": session_id, "tool_name": "Bash"},
        home=tmp_path,
    )

    assert result.returncode == 0
    out = json.loads(result.stdout)
    assert out["decision"] == "block"
    assert "Directive:" in out["reason"]
    assert "Tool blocked:  Bash" in out["reason"]


def test_enforce_route_soft_mode_still_logs_but_allows(tmp_path):
    """Users can explicitly relax enforcement without losing violation logging."""
    session_id = "sess-soft-override"
    _write_pending(tmp_path, session_id)

    result = _run_hook(
        ENFORCE_ROUTE_HOOK,
        {"session_id": session_id, "tool_name": "Bash"},
        home=tmp_path,
        extra_env={"LLM_ROUTER_ENFORCE": "soft"},
    )

    assert result.returncode == 0
    assert result.stdout.strip() == ""
    log_text = (tmp_path / ".llm-router" / "enforcement.log").read_text(encoding="utf-8")
    assert "VIOLATION" in log_text
    assert "expected=llm_query" in log_text


def test_enforce_route_blocks_file_tools_for_qa_tasks(tmp_path):
    """Glob/Read/Grep/LS are blocked for Q&A tasks — reading files is equivalent to self-answering."""
    for tool_name in ("Read", "Glob", "Grep", "LS"):
        session_id = f"sess-qa-{tool_name.lower()}"
        _write_pending(tmp_path, session_id, task_type="query")

        result = _run_hook(
            ENFORCE_ROUTE_HOOK,
            {"session_id": session_id, "tool_name": tool_name},
            home=tmp_path,
            extra_env={"LLM_ROUTER_ENFORCE": "hard"},
        )

        assert result.returncode == 0, f"{tool_name} should be blocked for query tasks"
        out = json.loads(result.stdout)
        assert out["decision"] == "block"
        assert tool_name in out["reason"]


def test_enforce_route_allows_file_tools_for_code_tasks(tmp_path):
    """Glob/Read/Grep/LS are allowed for code tasks — needed to find files before editing."""
    session_id = "sess-code-read-allowed"
    _write_pending(tmp_path, session_id, task_type="code", expected_tool="llm_code")

    for tool_name in ("Read", "Glob", "Grep", "LS"):
        result = _run_hook(
            ENFORCE_ROUTE_HOOK,
            {"session_id": session_id, "tool_name": tool_name},
            home=tmp_path,
            extra_env={"LLM_ROUTER_ENFORCE": "hard"},
        )

        assert result.returncode == 0
        assert result.stdout.strip() == "", f"{tool_name} should be allowed for code tasks"


def test_auto_route_logs_unrouted_previous_turn_on_next_prompt(tmp_path):
    """A pending route that survives to the next prompt is recorded as NO_ROUTE."""
    session_id = "sess-unrouted-prior-turn"
    pending_path = _write_pending(tmp_path, session_id)
    old_pending = json.loads(pending_path.read_text(encoding="utf-8"))

    result = _run_hook(
        AUTO_ROUTE_HOOK,
        {
            "session_id": session_id,
            "prompt": "Write a blog post about routing economics",
        },
        home=tmp_path,
    )

    assert result.returncode == 0
    out = json.loads(result.stdout)
    ctx = out["hookSpecificOutput"]["contextForAgent"]
    assert "PREVIOUS TURN VIOLATED ROUTING" in ctx
    assert "expected llm_query for query/simple" in ctx
    new_pending = json.loads(pending_path.read_text(encoding="utf-8"))
    assert new_pending["issued_at"] > old_pending["issued_at"]
    assert new_pending["task_type"] != old_pending["task_type"]

    log_text = (tmp_path / ".llm-router" / "enforcement.log").read_text(encoding="utf-8")
    assert "NO_ROUTE" in log_text
    assert "expected=llm_query" in log_text
    assert "task=query/simple" in log_text
    assert "prior unrouted turn" in out["systemMessage"]
