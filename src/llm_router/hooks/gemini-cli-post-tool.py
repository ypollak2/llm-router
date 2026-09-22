#!/usr/bin/env python3
# llm_router-hook-version: 1
"""Gemini CLI PostToolUse hook — writes a session-level savings marker.

Reads pending savings records from ~/.llm-router/gemini_session.json
(written by llm_auto / llm_track_usage when called from inside a Gemini CLI session)
and flushes them to savings_log.jsonl with host=gemini_cli so the session-end
query picks them up.

Hook input (stdin): JSON with tool name, input, output (Gemini CLI extension format).
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
    return _state_dir() / "gemini_session.json"
def _savings_log_path():
    return _state_dir() / "savings_log.jsonl"
def _last_flush_file():
    return _state_dir() / "gemini_last_flush.txt"

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

    _state_dir().mkdir(parents=True, exist_ok=True)
    try:
        with _savings_log_path().open("a") as f:
            for record in pending:
                record.setdefault("host", "gemini_cli")
                f.write(json.dumps(record) + "\n")
        session["pending_savings"] = []
        _session_file().write_text(json.dumps(session))
        _write_flush_time()
    except OSError:
        pass


def main() -> None:
    # Flush savings, then always surface the live "LLM Router is working" indicator.
    try:
        _flush()
    finally:
        try:
            from llm_router.observability.surface_status import emit_indicator

            emit_indicator("gemini_cli")
        except Exception:
            pass


if __name__ == "__main__":
    main()
