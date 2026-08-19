"""Phase 0 Step 1 — backward-safe schema migration for the realized-savings columns.

`execution_events` is created via `CREATE TABLE IF NOT EXISTS` with no ALTER path,
so a pre-Phase-0 `~/.llm-router/usage.db` lacks `classifier_cost_usd`,
`failed_attempt_cost_usd`, `baseline_tokens`, and `adoption_method`. This proves
`_connect()`'s new `_MIGRATIONS` pass:
  * adds the columns to an OLD-schema DB without touching existing rows,
  * leaves pre-migration rows NULL-safe on the new columns,
  * is idempotent (running it again — or opening a DB `_DDL` already created with
    the columns — never raises `sqlite3.OperationalError`),
  * `INSERT OR IGNORE` idempotency (event_id primary key) still holds post-migration.
"""
from __future__ import annotations

import sqlite3
import uuid

import pytest

from llm_router.execution_ledger import (
    LedgerEvent,
    _connect,
    get_route_accounting,
    record_event,
)

# The pre-Phase-0 DDL (frozen snapshot, minus the 4 new columns) — simulates a
# real pre-existing ~/.llm-router/usage.db created before this migration shipped.
_OLD_DDL = """
CREATE TABLE IF NOT EXISTS execution_events (
    schema_version INTEGER NOT NULL,
    event_id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    session_id TEXT,
    turn_id TEXT,
    route_id TEXT,
    attempt_id TEXT,
    event_type TEXT NOT NULL,
    task_type TEXT,
    routing_profile TEXT,
    host_mode TEXT,
    provider TEXT,
    model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_write_tokens INTEGER,
    measured_cost_usd REAL,
    baseline_equivalent_cost_usd REAL,
    hook_input_tokens INTEGER,
    hook_output_tokens INTEGER,
    accepted INTEGER,
    rejected INTEGER,
    rejection_reason TEXT,
    escalation_reason TEXT,
    fallback_reason TEXT,
    provider_failure_reason TEXT,
    used_by_host INTEGER,
    realization_status TEXT,
    override_type TEXT,
    terminal_state TEXT,
    metadata TEXT
);
"""


def test_migration_adds_columns_to_old_schema_db(tmp_path):
    """Simulate a pre-Phase-0 DB, then let `_connect()` migrate it in place."""
    db_path = tmp_path / "usage.db"

    # 1. Build the DB with the OLD schema and one pre-migration row (raw sqlite3,
    #    bypassing execution_ledger.py entirely — this is what a real old DB looks
    #    like on disk).
    raw = sqlite3.connect(str(db_path))
    raw.executescript(_OLD_DDL)
    old_event_id = str(uuid.uuid4())
    raw.execute(
        "INSERT INTO execution_events "
        "(schema_version, event_id, ts, session_id, route_id, event_type, "
        " measured_cost_usd, baseline_equivalent_cost_usd) "
        "VALUES (1, ?, 100.0, 's-old', 'r-old', 'attempt_completed', 0.01, 0.03)",
        (old_event_id,),
    )
    raw.commit()
    raw.close()

    # Sanity: the old DB genuinely lacks the new columns pre-migration.
    check = sqlite3.connect(str(db_path))
    old_cols = {r[1] for r in check.execute("PRAGMA table_info(execution_events)")}
    check.close()
    assert "classifier_cost_usd" not in old_cols
    assert "adoption_method" not in old_cols

    # 2. `_connect()` (the same path record_event()/_load_rows() use) must migrate
    #    it in place without raising and without touching the pre-existing row.
    conn = _connect(db_path)
    conn.close()

    check = sqlite3.connect(str(db_path))
    check.row_factory = sqlite3.Row
    new_cols = {r[1] for r in check.execute("PRAGMA table_info(execution_events)")}
    for col in ("classifier_cost_usd", "failed_attempt_cost_usd",
                "baseline_tokens", "adoption_method"):
        assert col in new_cols, f"migration did not add {col}"

    # 3. Old row survives untouched, new columns are NULL-safe (not an error, not
    #    a fabricated default).
    row = check.execute(
        "SELECT * FROM execution_events WHERE event_id = ?", (old_event_id,)
    ).fetchone()
    check.close()
    assert row is not None
    assert row["measured_cost_usd"] == pytest.approx(0.01)
    assert row["classifier_cost_usd"] is None
    assert row["failed_attempt_cost_usd"] is None
    assert row["baseline_tokens"] is None
    assert row["adoption_method"] is None


def test_migration_is_idempotent_on_already_current_db(tmp_path, monkeypatch):
    """Opening an already-current DB (created via the current `_DDL`, which already
    has the new columns) a second time must not raise — `_MIGRATIONS` degrades to a
    silent no-op via the per-statement try/except."""
    db_path = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_ROUTER_EXECUTION_LEDGER_DB", str(db_path))

    conn1 = _connect(db_path)
    conn1.close()
    # Second open re-runs _MIGRATIONS against a DB that already has the columns
    # (added by _DDL this time, not by a migration) — must still be a no-op.
    conn2 = _connect(db_path)
    conn2.close()

    cols = {r[1] for r in sqlite3.connect(str(db_path)).execute(
        "PRAGMA table_info(execution_events)")}
    assert "adoption_method" in cols


def test_old_rows_null_safe_through_record_event_and_aggregation(tmp_path, monkeypatch):
    """After migrating an old-schema DB, new writes via record_event() populate the
    new columns, old rows stay NULL on them, and aggregation over the mixed set
    doesn't blow up on the NULLs (Gap-1/2/3 columns are read defensively)."""
    db_path = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_ROUTER_EXECUTION_LEDGER_DB", str(db_path))

    raw = sqlite3.connect(str(db_path))
    raw.executescript(_OLD_DDL)
    old_id = str(uuid.uuid4())
    raw.execute(
        "INSERT INTO execution_events "
        "(schema_version, event_id, ts, route_id, event_type, measured_cost_usd) "
        "VALUES (1, ?, 100.0, 'r-mix', 'attempt_completed', 0.02)",
        (old_id,),
    )
    raw.commit()
    raw.close()

    new_ev = LedgerEvent(
        session_id="s-mix",
        route_id="r-mix",
        attempt_id=str(uuid.uuid4()),
        event_type="attempt_completed",
        measured_cost_usd=0.01,
        baseline_equivalent_cost_usd=0.05,
        classifier_cost_usd=0.001,
        baseline_tokens=500,
        adoption_method="door_call",
        realization_status="verified_used",
    )
    assert record_event(new_ev, path=db_path)

    # Aggregation must not raise on the mixed old-NULL / new-populated rows.
    acc = get_route_accounting("r-mix", path=db_path)
    assert acc.actual_cost_usd == pytest.approx(0.03)  # 0.02 (old) + 0.01 (new)


def test_insert_or_ignore_idempotency_holds_post_migration(tmp_path, monkeypatch):
    """INV-COST-003 must still hold after the schema change: re-recording the same
    event_id (now with the new columns present) is a silent no-op, not an error and
    not a duplicate row."""
    db_path = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_ROUTER_EXECUTION_LEDGER_DB", str(db_path))

    ev = LedgerEvent(
        session_id="s-dup",
        route_id="r-dup",
        attempt_id=str(uuid.uuid4()),
        event_type="attempt_completed",
        measured_cost_usd=0.01,
        classifier_cost_usd=0.002,
        adoption_method="agent_marked",
    )
    assert record_event(ev, path=db_path)
    assert record_event(ev, path=db_path)  # same event_id — must be ignored

    conn = _connect(db_path)
    n = conn.execute(
        "SELECT COUNT(*) FROM execution_events WHERE event_id = ?", (ev.event_id,)
    ).fetchone()[0]
    conn.close()
    assert n == 1

    acc = get_route_accounting("r-dup", path=db_path)
    assert acc.actual_cost_usd == pytest.approx(0.01)  # not double-counted
