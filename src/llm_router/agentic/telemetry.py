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
    output_tokens INTEGER NOT NULL DEFAULT 0,
    -- T-05. NO DEFAULT, matching cost.py's migration: a pre-existing row's
    -- provenance was never measured, and `DEFAULT 0` would assert that it was
    -- production.
    --
    -- This module creates the table itself on a fresh DB, so the column has to
    -- be here as well as in the migration. It was missed on the first pass, and
    -- the consequence was not a visible error: `_default_recorder` is
    -- deliberately fail-open, so `table savings_stats has no column named
    -- is_simulated` went straight into `except: pass` and EVERY agentic
    -- delegation savings row was silently dropped on any fresh install.
    is_simulated INTEGER
)
"""


def _detect_synthetic() -> bool:
    """Is this process writing test data? Delegates to the canonical detector.

    T-05. Local to this module because it runs in contexts where importing the
    full cost module is not guaranteed; the ANSWER still comes from
    `routing_quality.detect_synthetic`, never from a second copy of the rules.
    Fail-closed: if the detector cannot be reached we cannot certify the row as
    production, so it is stamped synthetic rather than counted as real money.
    """
    try:
        from llm_router.routing_quality import detect_synthetic
        return bool(detect_synthetic())
    except Exception:  # noqa: BLE001
        return True


def _db_path() -> Path:
    """The usage ledger, resolved on every call through the canonical resolver.

    This composed `~/.llm-router/usage.db` directly and so did NOT honour
    `LLM_ROUTER_HOME`. A test or probe that believed it was sandboxed wrote a
    savings row into the operator's real ledger — which is exactly the incident
    recorded in `evidence/AUDITOR_INCIDENT.md`, and it happened again here while
    verifying this very module (a fake $1.25 row, stamped `is_simulated=0`,
    landed in the live DB and had to be deleted by hand).

    `LLM_ROUTER_DB_PATH` is still honoured first for the callers that set it.
    """
    override = (os.environ.get("LLM_ROUTER_DB_PATH") or "").strip()
    if override:
        return Path(override)
    from llm_router import paths
    return paths.state_path("usage.db")


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
                # T-05: provenance stamped at write time. Without it this row
                # lands NULL and silently leaves every savings figure.
                "INSERT INTO savings_stats "
                "(timestamp, session_id, task_type, estimated_claude_cost_saved, "
                " external_cost, model_used, host, is_simulated) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    time.strftime("%Y-%m-%d %H:%M:%S"),
                    payload.get("session_id", ""),
                    payload.get("task_type", "code"),
                    payload.get("saved_usd", 0.0),
                    payload.get("actual_usd", 0.0),
                    payload.get("model", "llm_router-agentic-router"),
                    # Not 'claude_code': that host is how savings.VERIFIED_SAVED_SQL
                    # recognises the hook's realized-gated rows, and this saving
                    # is a flat estimate recorded whatever the outcome.
                    "agentic",
                    1 if _detect_synthetic() else 0,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as _exc:  # noqa: BLE001 — telemetry is fail-open; must never break a delegation
        # T-14: still fail-open, but no longer SILENT. A failed write here loses
        # an agentic delegation's savings row and reported nothing: no error, no log, no counter. 92
        # persistence sites had this shape; this is one of the ones that loses
        # data a user would notice missing.
        try:
            from llm_router import failopen as _fo
            _fo.record("CHZ-FO-AGENTIC-TELEMETRY-WRITE", _exc)
        except Exception:  # noqa: BLE001
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
