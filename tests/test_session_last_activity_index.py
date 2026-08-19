"""Refinement #4 — composite index on ``(state, last_activity_at)``.

The G-029 ``stuck_since_seconds`` filter on the admin agent ledger
(``GET /v1/admin/agents/status``) selects ``ACTIVE`` sessions
whose ``now - last_activity_at >= N``. Without an index that path
falls back to a full sequential scan once the sessions table grows
past a few thousand rows — fine for dev workstations, painful for a
multi-tenant agent platform.

Refinement #4 adds the composite index ``idx_sessions_state_activity``
on ``(state, last_activity_at)``. The leftmost-prefix rule means the
index serves both:

* ``WHERE state = ?`` (the existing ``recent(state=...)`` query)
* ``WHERE state = ? AND last_activity_at < ?`` (the new stuck filter)

This file pins the index's existence + the query planner picking it
for both query shapes via ``EXPLAIN QUERY PLAN``. If a future
refactor drops the index, the test fails fast.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from llm_router.agents.session import SessionStore


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(
        db_path=tmp_path / "sessions.db", check_same_thread=False
    )


# ── 1. Index exists on new DBs ─────────────────────────────────────────────


def test_index_exists_on_fresh_db(store: SessionStore) -> None:
    rows = store._conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='index' "
        "AND name='idx_sessions_state_activity'"
    ).fetchall()
    assert len(rows) == 1
    name, sql = rows[0]
    # Pin the column order — leftmost-prefix matters for query planning.
    assert "state" in sql
    assert "last_activity_at" in sql
    # Make sure state comes first (case-insensitive substring check).
    s_lower = sql.lower()
    assert s_lower.index("state") < s_lower.index("last_activity_at")


def test_index_exists_after_reopen(tmp_path: Path) -> None:
    """Idempotency — reopening the same DB doesn't lose or duplicate
    the index."""
    db = tmp_path / "s.db"
    SessionStore(db_path=db, check_same_thread=False).close()
    store = SessionStore(db_path=db, check_same_thread=False)
    rows = store._conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='index' "
        "AND name='idx_sessions_state_activity'"
    ).fetchone()
    assert rows[0] == 1


# ── 2. Index also lands on pre-existing DBs (migration parity) ────────────


def test_index_added_to_legacy_db_on_open(tmp_path: Path) -> None:
    """A SessionStore opened against a DB that pre-dates the index
    must pick up the new index without an explicit migration step.
    The CREATE INDEX IF NOT EXISTS in ``_SCHEMA`` runs on every
    ``__init__``."""
    db_path = tmp_path / "legacy.db"
    # Hand-build the full pre-refinement-#4 schema (everything up to
    # G-029 finisher) — same column set, no new index.
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL,
            started_at REAL NOT NULL,
            completed_at REAL,
            parent_session_id TEXT,
            budget_cap_usd REAL NOT NULL,
            consumed_usd REAL NOT NULL DEFAULT 0.0,
            step_count INTEGER NOT NULL DEFAULT 0,
            state TEXT NOT NULL,
            framework TEXT,
            max_iterations INTEGER,
            max_recursion_depth INTEGER,
            routing_policy_json TEXT,
            tool_call_count INTEGER NOT NULL DEFAULT 0,
            max_tool_calls INTEGER,
            max_children_concurrent INTEGER,
            last_activity_at REAL
        );
        CREATE INDEX idx_sessions_agent ON sessions(agent_id);
        CREATE INDEX idx_sessions_parent ON sessions(parent_session_id);
        CREATE INDEX idx_sessions_state ON sessions(state);
        """
    )
    conn.commit()
    # Confirm the new index is NOT there yet.
    before = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='index' "
        "AND name='idx_sessions_state_activity'"
    ).fetchone()[0]
    assert before == 0
    conn.close()

    # Re-open via SessionStore — schema runs, index lands.
    store = SessionStore(db_path=db_path, check_same_thread=False)
    after = store._conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='index' "
        "AND name='idx_sessions_state_activity'"
    ).fetchone()[0]
    assert after == 1


# ── 3. Query planner uses the index for the stuck-filter shape ────────────


def test_explain_uses_index_for_state_only(store: SessionStore) -> None:
    """``recent(state=...)`` query: the leftmost prefix of the composite
    index serves the predicate. Existing single-column
    ``idx_sessions_state`` may also be picked — either is fine as
    long as a scan is avoided."""
    plan = store._conn.execute(
        "EXPLAIN QUERY PLAN "
        "SELECT session_id FROM sessions WHERE state = 'active' "
        "ORDER BY started_at DESC LIMIT 10"
    ).fetchall()
    # The plan is a list of (id, parent, notused, detail) rows. We
    # look for any row whose detail mentions an index on sessions.
    details = " ".join(row[-1].lower() for row in plan)
    assert "using index" in details or "using covering index" in details, (
        f"expected an index scan for state predicate; got:\n{details}"
    )


def test_explain_uses_composite_for_stuck_filter(store: SessionStore) -> None:
    """The headline win — the new composite index is picked for the
    ``state + last_activity_at < ?`` predicate that backs the
    ``stuck_since_seconds`` filter."""
    plan = store._conn.execute(
        "EXPLAIN QUERY PLAN "
        "SELECT session_id FROM sessions "
        "WHERE state = 'active' AND last_activity_at < 1000000 "
        "ORDER BY started_at DESC LIMIT 10"
    ).fetchall()
    details = " ".join(row[-1].lower() for row in plan)
    # The composite index name should appear in the explain output
    # for the multi-column predicate; otherwise SQLite is doing a
    # scan and the refinement is silently dead.
    assert "idx_sessions_state_activity" in details, (
        f"expected idx_sessions_state_activity in plan; got:\n{details}"
    )


# ── 4. End-to-end smoke — actual rows queried with the index path ─────────


def test_stuck_query_returns_expected_rows_under_index(
    store: SessionStore,
) -> None:
    """Behavioural smoke: insert active + completed sessions with
    varying ``last_activity_at``, query the stuck shape, get the
    right rows. Confirms the index doesn't break the result."""
    import time

    now = time.time()
    # Active + recent — should NOT match.
    store.create(agent_id="fresh", budget_usd=1.0)
    # Active + stale — should match.
    stale = store.create(agent_id="stale", budget_usd=1.0)
    store._conn.execute(
        "UPDATE sessions SET last_activity_at = ? WHERE session_id = ?",
        (now - 3600, stale.session_id),
    )
    # Completed + stale — should NOT match (state filter).
    done = store.create(agent_id="done", budget_usd=1.0)
    store.complete(done.session_id)
    store._conn.execute(
        "UPDATE sessions SET last_activity_at = ? WHERE session_id = ?",
        (now - 7200, done.session_id),
    )
    store._conn.commit()

    rows = store._conn.execute(
        "SELECT session_id FROM sessions "
        "WHERE state = 'active' AND last_activity_at < ? "
        "ORDER BY started_at DESC",
        (now - 1800,),
    ).fetchall()
    matched_ids = {row[0] for row in rows}
    assert matched_ids == {stale.session_id}
