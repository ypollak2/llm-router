"""Delegation savings telemetry — records a delegation's honest saving into
llm_router's existing ``savings_stats`` ledger (the table ``llm_savings`` reads).

The recorder is injected so tests use a fake; the default writes a savings_stats
row and is FAIL-OPEN — telemetry must never break or block a delegation.
"""
from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

Recorder = Callable[[dict[str, Any]], Awaitable[None]]

_SAVINGS_DDL = """
CREATE TABLE IF NOT EXISTS savings_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    session_id TEXT NOT NULL,
    task_type TEXT NOT NULL,
    estimated_claude_cost_saved REAL NOT NULL,
    external_cost REAL NOT NULL,
    model_used TEXT NOT NULL,
    host TEXT NOT NULL DEFAULT 'claude_code',
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0
)
"""


def _db_path() -> Path:
    return Path(os.environ.get("LLM_ROUTER_DB_PATH") or (Path(os.path.expanduser("~")) / ".llm-router" / "usage.db"))


def savings_payload(
    result: dict[str, Any], *, model: str = "llm_router-agentic-router", session_id: str = ""
) -> dict[str, Any]:
    """Build the telemetry row from a serialized delegation result dict."""
    sv = result.get("savings", {}) or {}
    return {
        "model": model,
        "session_id": session_id,
        "task_type": result.get("task_type", "code"),
        "outcome": result.get("outcome", "unknown"),
        "saved_usd": float(sv.get("saved_usd", 0.0)),
        "actual_usd": float(sv.get("actual_usd", 0.0)),
    }


async def _default_recorder(payload: dict[str, Any]) -> None:
    """Append a savings_stats row. Fail-open — never raises."""
    try:
        path = _db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path))
        try:
            conn.execute(_SAVINGS_DDL)
            conn.execute(
                "INSERT INTO savings_stats "
                "(timestamp, session_id, task_type, estimated_claude_cost_saved, "
                " external_cost, model_used, host) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    time.strftime("%Y-%m-%d %H:%M:%S"),
                    payload.get("session_id", ""),
                    payload.get("task_type", "code"),
                    payload.get("saved_usd", 0.0),
                    payload.get("actual_usd", 0.0),
                    payload.get("model", "llm_router-agentic-router"),
                    "claude_code",
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: S110, BLE001 — telemetry is fail-open; must never break a delegation
        pass


async def record_delegation_savings(
    result: dict[str, Any],
    *,
    recorder: Recorder | None = None,
    model: str = "llm_router-agentic-router",
    session_id: str = "",
) -> dict[str, Any]:
    """Record a delegation's savings via ``recorder`` (default: savings_stats)."""
    payload = savings_payload(result, model=model, session_id=session_id)
    rec = recorder or _default_recorder
    try:
        await rec(payload)
    except Exception:  # noqa: S110, BLE001 — fail-open telemetry
        pass
    return payload
