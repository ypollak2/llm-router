#!/usr/bin/env python3
# llm-router-hook-version: 1
"""Codex CLI PostToolUse hook — writes a session-level savings marker.

Codex hooks only fire on Bash tool calls, so this hook cannot track every
MCP routing call individually.  Instead it reads the last-known savings
snapshot from ~/.llm-router/codex_session.json (written by llm_auto /
llm_track_usage when called from inside a Codex session) and appends any
un-flushed savings records to savings_log.jsonl so the session-end query
picks them up.

Hook input (stdin): JSON with tool name, input, output (Codex format).
Hook output: nothing (hook runs silently; errors are suppressed).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

STATE_DIR        = Path.home() / ".llm-router"
SESSION_FILE     = STATE_DIR / "codex_session.json"
SAVINGS_LOG_PATH = STATE_DIR / "savings_log.jsonl"
LAST_FLUSH_FILE  = STATE_DIR / "codex_last_flush.txt"

# Minimum seconds between flushes to avoid hammering the log
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
    # Read Codex hook payload (may be empty or invalid — always silent)
    try:
        json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        pass

    # Rate-limit: only flush once per interval
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
                # Ensure host is tagged as codex
                record.setdefault("host", "codex")
                f.write(json.dumps(record) + "\n")
        # Clear pending after successful flush
        session["pending_savings"] = []
        SESSION_FILE.write_text(json.dumps(session))
        _write_flush_time()
    except OSError:
        pass


if __name__ == "__main__":
    main()
