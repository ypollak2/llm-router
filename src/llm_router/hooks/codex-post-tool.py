#!/usr/bin/env python3
# llm_router-hook-version: 1
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

def _router_home():
    """Router state dir, resolved per call so LLM_ROUTER_HOME is honoured.

    M-04: this was a module constant bound at import, so a hook launched with
    LLM_ROUTER_HOME set still wrote to the operator's real home directory.

    Imports locally: hooks are standalone scripts with varied import headers and
    several do not import Path or os at module scope.
    """
    import os as _os
    from pathlib import Path as _P

    base = _os.environ.get("LLM_ROUTER_HOME", "").strip()
    return _P(base).expanduser() if base else _P.home() / ".llm-router"


def _state_dir():
    return _router_home()
def _session_file():
    return _state_dir() / "codex_session.json"
def _savings_log_path():
    return _state_dir() / "savings_log.jsonl"
def _last_flush_file():
    return _state_dir() / "codex_last_flush.txt"

# Minimum seconds between flushes to avoid hammering the log
_FLUSH_INTERVAL = 30


def _read_session() -> dict:
    try:
        return json.loads(_session_file().read_text())
    except Exception:
        return {}


def _last_flush_time() -> float:
    try:
        return float(_last_flush_file().read_text().strip())
    except Exception:
        return 0.0


def _write_flush_time() -> None:
    try:
        _last_flush_file().write_text(str(time.time()))
    except OSError:
        pass


def _flush() -> None:
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

    _state_dir().mkdir(parents=True, exist_ok=True)
    try:
        with _savings_log_path().open("a") as f:
            for record in pending:
                # Ensure host is tagged as codex
                record.setdefault("host", "codex")
                f.write(json.dumps(record) + "\n")
        # Clear pending after successful flush
        session["pending_savings"] = []
        _session_file().write_text(json.dumps(session))
        _write_flush_time()
    except OSError:
        pass


def main() -> None:
    # Flush savings, then ALWAYS surface the live indicator (even when there was
    # nothing new to flush) so the user sees that LLM Router is working on Codex.
    try:
        _flush()
    finally:
        try:
            from llm_router.observability.surface_status import emit_indicator

            emit_indicator("codex")
        except Exception:
            pass


if __name__ == "__main__":
    main()
