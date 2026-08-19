"""ENF-FIX-3 — an enforcement auto-pivot is recorded as a first-class escalation.

When the routed door genuinely can't complete the work, enforcement releases the
lock (an auto-pivot) so the host proceeds. Previously that release was only a
text-log line — invisible to accounting. The North-Star ladder treats reaching
Claude as an *escalation*, which must be reconciled in the canonical ledger
(INV-COST-001). This test drives a trap auto-pivot and asserts an
``escalation_started`` event lands in ``execution_events``.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "src" / "llm_router" / "hooks" / "enforce-route.py"


def _ledger_db(home) -> Path:
    return home / ".llm-router" / "usage.db"


def _run(payload, home):
    e = {k: v for k, v in os.environ.items() if k not in ("LLM_ROUTER_ENFORCE", "LLM_ROUTER_SLIM")}
    e["HOME"] = str(home)
    e["LLM_ROUTER_ENFORCE"] = "hard"
    # Pin the ledger db to this test's home so a session-level
    # LLM_ROUTER_EXECUTION_LEDGER_DB / LLM_ROUTER_DB_PATH can't redirect the write.
    e["LLM_ROUTER_EXECUTION_LEDGER_DB"] = str(_ledger_db(home))
    e.pop("LLM_ROUTER_DB_PATH", None)
    return subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload),
                          capture_output=True, text=True, env=e)


def _pending(home, sid, **ov):
    d = home / ".llm-router"
    d.mkdir(parents=True, exist_ok=True)
    data = {"expected_tool": "llm_code", "task_type": "code", "complexity": "moderate",
            "method": "heuristic", "issued_at": time.time(), "turn_id": 1, "session_id": sid,
            "original_prompt": "Refactor the parser module for clarity."}
    data.update(ov)
    (d / f"pending_route_{sid}.json").write_text(json.dumps(data))


def _escalation_events(home):
    db = _ledger_db(home)
    if not db.exists():
        return []
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT escalation_reason, metadata FROM execution_events "
            "WHERE event_type = 'escalation_started'"
        ).fetchall()
        return rows
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def test_trap_autopivot_records_escalation_event(tmp_path):
    """Fail-before: a trap auto-pivot only wrote a text-log line; the ledger had
    no escalation event. Pass-after: an escalation_started event is recorded."""
    sid = "esc1"
    _pending(tmp_path, sid)
    cmd = {"command": "git commit -am wip"}  # a write (non-readonly) Bash

    # Block 1: pending active, write-Bash blocked (violation 1, turn-block 1).
    r1 = _run({"session_id": sid, "tool_name": "Bash", "tool_input": cmd}, tmp_path)
    assert json.loads(r1.stdout)["decision"] == "block"
    assert not _escalation_events(tmp_path), "no escalation before the pivot"

    # Block 2 same tool/turn → trap auto-pivot releases the lock.
    r2 = _run({"session_id": sid, "tool_name": "Bash", "tool_input": cmd}, tmp_path)
    assert r2.returncode == 0 and r2.stdout.strip() == "", "trap pivot must release the lock"

    events = _escalation_events(tmp_path)
    assert events, "the auto-pivot must record a first-class escalation event"
    reason, meta = events[0]
    assert reason == "trap"
    assert "enforce-route" in (meta or ""), "escalation records its source"


def test_calling_the_routed_door_does_not_record_an_escalation(tmp_path):
    """Guard: a clean one-step route (calling an llm_* door) clears the lock and
    is NOT an escalation — escalation is only for a pivot to the host."""
    sid = "esc2"
    _pending(tmp_path, sid)
    r = _run({"session_id": sid, "tool_name": "mcp__llm_router__llm", "tool_input": {"prompt": "x"}}, tmp_path)
    assert r.returncode == 0
    assert not _escalation_events(tmp_path), "calling the door is a clean route, not an escalation"
