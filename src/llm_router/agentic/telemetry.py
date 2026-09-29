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


#: model_used of the legacy flat-credit rows (milestones x $0.20, zero tokens,
#: written whatever the outcome). Read-time filtered out of every headline via
#: ``savings.EXCLUDED_SAVINGS_MODELS``; the rows themselves are kept.
LEGACY_FLAT_MODEL = "llm_router-agentic-router"
#: model_used of a row this module persists now: a completed delegation priced
#: from its own measured token counts. A distinct name so the read-time filter
#: on the legacy rows can never swallow a measured one.
MEASURED_MODEL = "llm_router-agentic-measured"


def _measured_tokens(result: dict[str, Any]) -> tuple[int, int] | None:
    """(input, output) tokens the delegation actually consumed, or None.

    Only ``result["usage"]`` counts as a measurement. ``compute_savings``'s
    ``baseline_usd`` is ``len(milestones) * baseline_cost_per_milestone`` — a
    constant times a count — so it is never treated as one.
    """
    usage = result.get("usage")
    if not isinstance(usage, dict):
        return None
    raw_in, raw_out = usage.get("input_tokens"), usage.get("output_tokens")
    if raw_in is None or raw_out is None:  # absent is unmeasured, not zero
        return None
    try:
        in_tok, out_tok = int(raw_in), int(raw_out)
    except (TypeError, ValueError):
        return None
    if in_tok < 0 or out_tok < 0 or in_tok + out_tok == 0:
        return None
    return in_tok, out_tok


def savings_payload(
    result: dict[str, Any], *, model: str = MEASURED_MODEL, session_id: str = ""
) -> dict[str, Any]:
    """Build the telemetry row from a serialized delegation result dict.

    Phase 0.2b (audit 2026-09-29, claim C3): the old payload copied
    ``savings.saved_usd`` — a flat ``milestones x $0.20`` credit — for every
    delegation whatever its outcome: 529 rows, $110.20, 91% of the all-time
    ``savings_stats`` total. A row is now persisted ONLY when the delegation
    completed AND reported real token counts; its saving is the counterfactual
    cost of those tokens on the savings baseline model minus what was actually
    spent. Anything else yields ``persisted=False`` and ``saved_usd=0.0`` and is
    skipped, not written as a $0 row: a $0 row would still add to the ``n``
    beside every "est. saved" figure while carrying no measurement, and the
    delegation's outcome is already recorded by ``routing_quality.record_delegation``.
    """
    sv = result.get("savings", {}) or {}
    actual = float(sv.get("actual_usd", 0.0) or 0.0)
    outcome = result.get("outcome", "unknown")
    tokens = _measured_tokens(result) if outcome == "complete" else None
    saved = 0.0
    if tokens is not None:
        from llm_router import pricing
        baseline = pricing.cost_usd(pricing.savings_baseline_model(), *tokens)
        if baseline is None:  # unpriced baseline: no counterfactual, no row
            tokens = None
        else:
            saved = baseline - actual
    return {
        "model": model,
        "session_id": session_id,
        "task_type": result.get("task_type", "code"),
        "outcome": outcome,
        "saved_usd": saved,
        "actual_usd": actual,
        "input_tokens": tokens[0] if tokens else 0,
        "output_tokens": tokens[1] if tokens else 0,
        "persisted": tokens is not None,
    }


async def _default_recorder(payload: dict[str, Any]) -> None:
    """Append a savings_stats row. Fail-open — never raises.

    Skips a payload that carries no measured outcome (``persisted`` False) —
    see :func:`savings_payload`.
    """
    if not payload.get("persisted"):
        return
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
                " external_cost, model_used, host, input_tokens, output_tokens, "
                " is_simulated) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    time.strftime("%Y-%m-%d %H:%M:%S"),
                    payload.get("session_id", ""),
                    payload.get("task_type", "code"),
                    payload.get("saved_usd", 0.0),
                    payload.get("actual_usd", 0.0),
                    payload.get("model", MEASURED_MODEL),
                    # Not 'claude_code': that host is how savings.VERIFIED_SAVED_SQL
                    # recognises the hook's realized-gated rows, and this saving
                    # is an estimate nobody observed replacing a Claude turn.
                    "agentic",
                    payload.get("input_tokens", 0),
                    payload.get("output_tokens", 0),
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
    model: str = MEASURED_MODEL,
    session_id: str = "",
) -> dict[str, Any]:
    """Record a delegation's savings via ``recorder`` (default: savings_stats).

    An injected ``recorder`` still receives every payload (including
    ``persisted=False`` ones); the default recorder skips those.
    """
    payload = savings_payload(result, model=model, session_id=session_id)
    rec = recorder or _default_recorder
    try:
        await rec(payload)
    except Exception:  # noqa: S110, BLE001 — fail-open telemetry
        pass
    return payload
