"""Regression tests for query_last_prompt_calls staleness gate.

Background: the dashboard's "LAST PROMPT ROUTING" panel was showing rows
that were hours/days old, anchored on the latest row's timestamp instead of
NOW. If a user prompt didn't trigger MCP routing, the panel still rendered
stale rows from the most recent burst — misleading users into thinking
their current prompt cost N tokens when nothing was routed at all.

Fix: anchor the recency window on NOW, and bail when the latest row is
older than max_age_sec.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from llm_router.hooks.dashboard_enhanced import query_last_prompt_calls


def _seed_usage(db_path: Path, rows: list[tuple]) -> None:
    """Create a usage table and insert (model, provider, task_type, in, out, cost, ts_sql) rows."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """CREATE TABLE usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            model TEXT NOT NULL,
            provider TEXT NOT NULL,
            task_type TEXT NOT NULL,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            cost_usd REAL NOT NULL DEFAULT 0.0,
            success INTEGER NOT NULL DEFAULT 1
        )"""
    )
    for model, provider, task_type, in_t, out_t, cost, ts_sql in rows:
        conn.execute(
            "INSERT INTO usage (timestamp, model, provider, task_type, "
            "input_tokens, output_tokens, cost_usd, success) "
            f"VALUES ({ts_sql}, ?, ?, ?, ?, ?, ?, 1)",
            (model, provider, task_type, in_t, out_t, cost),
        )
    conn.commit()
    conn.close()


def test_returns_recent_rows(tmp_path):
    """Rows written seconds ago must be returned."""
    db = tmp_path / "usage.db"
    _seed_usage(db, [
        ("codex/gpt-5.4", "codex", "query", 50, 30, 0.0, "datetime('now', '-3 seconds')"),
        ("codex/gpt-5.4", "codex", "code",  80, 40, 0.0, "datetime('now', '-1 seconds')"),
    ])

    calls = query_last_prompt_calls(db_path=db)
    assert len(calls) == 2
    assert {c["task_type"] for c in calls} == {"query", "code"}


def test_returns_empty_when_latest_is_stale(tmp_path):
    """If the latest row is older than max_age_sec (default 120s), return empty.

    This is the regression test for the bug where stale rows from 19 hours ago
    were being rendered as if they were the user's current prompt.
    """
    db = tmp_path / "usage.db"
    _seed_usage(db, [
        ("codex/gpt-5.4", "codex", "query", 1, 2, 0.0, "datetime('now', '-19 hours')"),
        ("codex/gpt-5.4", "codex", "query", 1, 9, 0.0, "datetime('now', '-19 hours')"),
    ])

    calls = query_last_prompt_calls(db_path=db)
    assert calls == [], "Stale rows must not be returned as 'current prompt' data"


def test_window_anchors_on_now_not_latest_row(tmp_path):
    """The 30s window is relative to NOW. Rows older than NOW-window_sec are excluded
    even if the latest row is within max_age_sec."""
    db = tmp_path / "usage.db"
    _seed_usage(db, [
        # latest row: 10s ago → passes max_age_sec gate
        ("codex/gpt-5.4", "codex", "query", 50, 30, 0.0, "datetime('now', '-10 seconds')"),
        # older sibling: 60s ago → OUTSIDE the 30s window from NOW
        ("codex/gpt-5.4", "codex", "code",  80, 40, 0.0, "datetime('now', '-60 seconds')"),
    ])

    calls = query_last_prompt_calls(db_path=db, window_sec=30)
    # Only the 10s-ago row is within the 30s-from-now window.
    assert len(calls) == 1
    assert calls[0]["task_type"] == "query"


def test_empty_db_returns_empty(tmp_path):
    """No rows in usage table → empty list, not exception."""
    db = tmp_path / "usage.db"
    _seed_usage(db, [])
    assert query_last_prompt_calls(db_path=db) == []


def test_missing_db_returns_empty(tmp_path):
    """No DB file at all → empty list, not exception."""
    assert query_last_prompt_calls(db_path=tmp_path / "nope.db") == []


def test_custom_max_age_sec(tmp_path):
    """Caller can tighten / loosen the staleness threshold."""
    db = tmp_path / "usage.db"
    _seed_usage(db, [
        ("codex/gpt-5.4", "codex", "query", 50, 30, 0.0, "datetime('now', '-90 seconds')"),
    ])

    # Default max_age_sec=120 → the 90s-old row passes the gate, but the 30s
    # window-from-now excludes it. So returns empty.
    assert query_last_prompt_calls(db_path=db) == []

    # Tighten gate to 60s → the 90s-old row fails the gate too. Still empty.
    assert query_last_prompt_calls(db_path=db, max_age_sec=60) == []

    # Loosen window to 120s → row falls within window AND passes gate.
    calls = query_last_prompt_calls(db_path=db, max_age_sec=120, window_sec=120)
    assert len(calls) == 1
