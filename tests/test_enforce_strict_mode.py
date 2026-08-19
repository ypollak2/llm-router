"""Tests for the new ``strict`` enforcement mode + outcome-stamped log lines.

Strict mode (LLM_ROUTER_ENFORCE=strict) disables every escape valve:
* the read-only Bash exception (smart mode allows ``git log``, ``ls``, ...)
* the loop auto-pivot (3× same tool in 2 min → unblock)
* the violation-count auto-pivot (4 violations/turn → unblock)

Each VIOLATION line now carries an ``outcome=…`` tag so the log is
self-explanatory instead of requiring source reads to disambiguate.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path



ROOT = Path(__file__).resolve().parents[1]
ENFORCE_ROUTE_HOOK = ROOT / "src" / "llm_router" / "hooks" / "enforce-route.py"


def _run_hook(
    payload: dict,
    *,
    home: Path,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "LLM_ROUTER_ENFORCE"}
    env["HOME"] = str(home)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(ENFORCE_ROUTE_HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )


def _write_pending(home: Path, session_id: str, **overrides) -> Path:
    router_dir = home / ".llm-router"
    router_dir.mkdir(parents=True, exist_ok=True)
    pending = router_dir / f"pending_route_{session_id}.json"
    data = {
        "expected_tool": "llm_code",
        "task_type": "code",
        "complexity": "moderate",
        "issued_at": time.time(),
        "session_id": session_id,
    }
    data.update(overrides)
    pending.write_text(json.dumps(data), encoding="utf-8")
    return pending


def _enforcement_log(home: Path) -> str:
    log = home / ".llm-router" / "enforcement.log"
    return log.read_text(encoding="utf-8") if log.exists() else ""


# ── Strict mode disables escape valves ────────────────────────────────────────


def test_strict_mode_blocks_readonly_bash(tmp_path):
    """Smart mode allows ``git log``; strict mode treats every Bash as a bypass."""
    session_id = "sess-strict-readonly"
    _write_pending(tmp_path, session_id)

    result = _run_hook(
        {
            "session_id": session_id,
            "tool_name": "Bash",
            "tool_input": {"command": "git log --oneline -5"},
        },
        home=tmp_path,
        extra_env={"LLM_ROUTER_ENFORCE": "strict"},
    )

    assert result.returncode == 0
    out = json.loads(result.stdout)
    assert out["decision"] == "block", (
        "strict mode must reject read-only Bash on code tasks; "
        "stdout was: " + result.stdout
    )
    log = _enforcement_log(tmp_path)
    assert "outcome=BLOCKED(strict)" in log


def test_smart_mode_still_allows_readonly_bash(tmp_path):
    """The strict-mode change must not break the smart-mode escape hatch."""
    session_id = "sess-smart-readonly"
    _write_pending(tmp_path, session_id)

    result = _run_hook(
        {
            "session_id": session_id,
            "tool_name": "Bash",
            "tool_input": {"command": "ls -la"},
        },
        home=tmp_path,
        extra_env={"LLM_ROUTER_ENFORCE": "smart"},
    )

    assert result.returncode == 0
    assert result.stdout.strip() == ""  # exit 0 with no block payload
    log = _enforcement_log(tmp_path)
    assert "outcome=ALLOWED(readonly_bash)" in log


# ── Outcome stamping on the violation log ─────────────────────────────────────


def test_soft_mode_stamps_allowed_outcome(tmp_path):
    session_id = "sess-soft-outcome"
    _write_pending(tmp_path, session_id)

    result = _run_hook(
        {
            "session_id": session_id,
            "tool_name": "Bash",
            "tool_input": {"command": "rm -rf /tmp/scratch"},
        },
        home=tmp_path,
        extra_env={"LLM_ROUTER_ENFORCE": "soft"},
    )

    assert result.returncode == 0
    assert result.stdout.strip() == ""
    log = _enforcement_log(tmp_path)
    assert "outcome=ALLOWED(soft)" in log


def test_hard_mode_stamps_blocked_outcome(tmp_path):
    session_id = "sess-hard-outcome"
    _write_pending(tmp_path, session_id)

    result = _run_hook(
        {
            "session_id": session_id,
            "tool_name": "Bash",
            "tool_input": {"command": "rm -rf /tmp/scratch"},
        },
        home=tmp_path,
        extra_env={"LLM_ROUTER_ENFORCE": "hard"},
    )

    assert result.returncode == 0
    out = json.loads(result.stdout)
    assert out["decision"] == "block"
    log = _enforcement_log(tmp_path)
    assert "outcome=BLOCKED" in log
    # Non-strict block must not be tagged BLOCKED(strict)
    assert "outcome=BLOCKED(strict)" not in log
