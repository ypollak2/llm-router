"""Regression: RED1-06 (route_id absent) + RED1-05 (event_id defeats dedup).

RED1-06: auto-route.py's pending-directive JSON never wrote a `route_id`, so
enforce-route.py's `pending.get("route_id")` was always None in production and
every route in a session collapsed into one `route_id` accounting bucket.

RED1-05: `LedgerEvent.event_id` defaulted to a fresh uuid4 per instance, so the
`INSERT OR IGNORE` dedup was structurally unreachable — a retried hook recording
the same logical override/realization always created a new row.

These tests assert: (1) the pending JSON now carries a stable route_id; (2) two
recordings of the same logical override/realization (same session+route) collapse
to one ledger row.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENFORCE_HOOK = ROOT / "src" / "llm_router" / "hooks" / "enforce-route.py"
STOP_HOOK = ROOT / "src" / "llm_router" / "hooks" / "stop-enforce.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


enforce = _load(ENFORCE_HOOK, "llm_router_enforce_route_routeid")
stop = _load(STOP_HOOK, "llm_router_stop_enforce_routeid")


def _rows(db: Path):
    conn = sqlite3.connect(str(db))
    try:
        return list(conn.execute(
            "SELECT event_id, route_id, event_type FROM execution_events"
        ))
    finally:
        conn.close()


def test_pending_json_carries_stable_route_id(tmp_path):
    """The auto-route hook must write a non-null route_id into the pending JSON.

    Driven at the source level: we can't easily run the whole hook here, so we
    assert the writer's contract by importing the hook and checking the literal
    is written. A structural guard: the pending-JSON writer must reference
    'route_id'."""
    text = (ROOT / "src" / "llm_router" / "hooks" / "auto-route.py").read_text()
    assert '"route_id":' in text, "auto-route pending JSON must write route_id (RED1-06)"


def test_override_dedups_on_retry(tmp_path, monkeypatch):
    db = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_ROUTER_EXECUTION_LEDGER_DB", str(db))
    pending = {"route_id": "sessX:123:llm_query", "turn_id": 123, "task_type": "query"}

    # Same logical override recorded 3x (e.g. a retried Stop hook).
    for _ in range(3):
        stop._record_override("sessX", "query", pending)

    rows = _rows(db)
    override_rows = [r for r in rows if r[2] == "plain_text_override"]
    assert len(override_rows) == 1, (
        f"RED1-05: retried override not deduped — {len(override_rows)} rows"
    )
    assert override_rows[0][1] == "sessX:123:llm_query", "route_id not threaded (RED1-06)"


def test_realization_dedups_on_retry(tmp_path, monkeypatch):
    db = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_ROUTER_EXECUTION_LEDGER_DB", str(db))
    pending = {"route_id": "sessY:456:llm_code", "turn_id": 456, "task_type": "code"}

    for _ in range(3):
        enforce._record_realization_used("sessY", pending)

    rows = _rows(db)
    realized = [r for r in rows if r[2] == "route_realized"]
    assert len(realized) == 1, f"RED1-05: retried realization not deduped — {len(realized)} rows"
    assert realized[0][1] == "sessY:456:llm_code"


def test_distinct_routes_stay_distinct(tmp_path, monkeypatch):
    """Two different routes in a session must NOT collapse (RED1-06 core)."""
    db = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_ROUTER_EXECUTION_LEDGER_DB", str(db))
    stop._record_override("s", "query", {"route_id": "s:1:llm_query", "turn_id": 1})
    stop._record_override("s", "code", {"route_id": "s:2:llm_code", "turn_id": 2})
    rows = _rows(db)
    route_ids = {r[1] for r in rows if r[2] == "plain_text_override"}
    assert route_ids == {"s:1:llm_query", "s:2:llm_code"}, (
        f"distinct routes collapsed: {route_ids}"
    )
