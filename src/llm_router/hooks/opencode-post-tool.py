#!/usr/bin/env python3
# llm-router-hook-version: 1
"""OpenCode PostToolUse hook — writes a session-level savings marker.

Reads pending savings records from ~/.llm-router/opencode_session.json
(written by llm_auto / llm_track_usage when called from inside an OpenCode session)
and flushes them to savings_log.jsonl with host=opencode so the session-end
query picks them up.

Hook input (stdin): JSON with tool name, input, output (OpenCode format).
Hook output: nothing (hook runs silently; errors are suppressed).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

STATE_DIR        = Path.home() / ".llm-router"
SESSION_FILE     = STATE_DIR / "opencode_session.json"
SAVINGS_LOG_PATH = STATE_DIR / "savings_log.jsonl"
LAST_FLUSH_FILE  = STATE_DIR / "opencode_last_flush.txt"

_FLUSH_INTERVAL = 30


def _read_session() -> dict:
    try:
        return json.loads(SESSION_FILE.read_text())
    except Exception:
        return {}


def _last_flush_time() -> float:
    try:
        return float(LAST_FLUSH_FILE.read_text().strip())
    except Exception:
        return 0.0


def _write_flush_time() -> None:
    try:
        LAST_FLUSH_FILE.write_text(str(time.time()))
    except OSError:
        pass


def main() -> None:
    try:
        json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        pass

    if time.time() - _last_flush_time() < _FLUSH_INTERVAL:
        return

    session = _read_session()
    pending = session.get("pending_savings", [])
    if not pending:
        return

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with SAVINGS_LOG_PATH.open("a") as f:
            for record in pending:
                record.setdefault("host", "opencode")
                f.write(json.dumps(record) + "\n")
        session["pending_savings"] = []
        SESSION_FILE.write_text(json.dumps(session))
        _write_flush_time()
    except OSError:
        pass


if __name__ == "__main__":
    main()
