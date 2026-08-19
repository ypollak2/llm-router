"""Regression: CHZ-EXT-204 — realization telemetry was never populated.

Every `execution_events` row was written with `realization_status`,
`used_by_host` and `accepted` = NULL, because only directive *injection* was
recorded — never whether the host honored or bypassed it. A run where 97.7% of
directives were bypassed was therefore indistinguishable in telemetry from a
perfect one.

The fix adds a positive `verified_used` write on the honor path
(enforce-route.py `_record_realization_used`) to match the existing
`verified_overridden` write on the plain-text-override path (stop-enforce.py).
These tests prove that a mixed honor/override run produces non-NULL realization
rows AND a computable bypass rate.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENFORCE_HOOK = ROOT / "src" / "llm_router" / "hooks" / "enforce-route.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


enforce = _load(ENFORCE_HOOK, "llm_router_enforce_route_hook")


def _rows(db: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute(
            "SELECT session_id, event_type, realization_status, used_by_host, "
            "accepted FROM execution_events"
        ))
    finally:
        conn.close()


def test_honored_route_writes_non_null_realization(tmp_path, monkeypatch) -> None:
    db = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_ROUTER_EXECUTION_LEDGER_DB", str(db))

    pending = {"task_type": "query", "route_id": "r-1", "turn_id": "t-1"}
    enforce._record_realization_used("sess-A", pending)

    rows = _rows(db)
    assert len(rows) == 1
    r = rows[0]
    assert r["realization_status"] == "verified_used", "honor must write verified_used"
    assert r["used_by_host"] == 1, "used_by_host must be non-NULL / true"
    assert r["accepted"] == 1, "accepted must be non-NULL / true"
    assert r["session_id"] == "sess-A", "direct rows must carry session_id (CHZ-PRV-06)"


def test_bypass_rate_is_computable_from_telemetry(tmp_path, monkeypatch) -> None:
    """A mixed run must yield a measurable realized-vs-total ratio (was impossible)."""
    db = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_ROUTER_EXECUTION_LEDGER_DB", str(db))

    from llm_router.execution_ledger import LedgerEvent, record_event

    # 3 honored, 7 overridden — a 70% bypass run that must be visible.
    for i in range(3):
        enforce._record_realization_used(f"s{i}", {"task_type": "query", "route_id": f"r{i}"})
    for i in range(7):
        record_event(LedgerEvent(
            session_id=f"o{i}",
            route_id=f"ro{i}",
            event_type="plain_text_override",
            task_type="query",
            realization_status="verified_overridden",
            used_by_host=False,
        ))

    rows = _rows(db)
    total = len(rows)
    null_realization = sum(1 for r in rows if r["realization_status"] is None)
    used = sum(1 for r in rows if r["realization_status"] == "verified_used")
    overridden = sum(1 for r in rows if r["realization_status"] == "verified_overridden")

    assert total == 10
    assert null_realization == 0, "CHZ-EXT-204: no row may have NULL realization now"
    assert used == 3 and overridden == 7
    bypass_rate = overridden / total
    assert abs(bypass_rate - 0.7) < 1e-9, "bypass rate must be computable and correct"
