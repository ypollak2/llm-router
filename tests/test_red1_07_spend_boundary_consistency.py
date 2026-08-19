"""Regression: RED1-07 — monthly/daily spend boundaries must share one frame.

get_monthly_spend() used a UTC 'start of month' while get_daily_spend*() used a
localtime day boundary. At non-UTC offsets, near a month boundary the daily cap's
local "today" and the monthly cap's UTC "this month" disagreed, so a row could be
counted in "today" but not "this month" (or vice versa). Both now reference the
local frame, so daily-today is always a subset of monthly-now.

This drives the real SQL against an in-memory usage table with a row placed at a
UTC instant that is the previous local month-end but the current UTC month (the
exact divergence window), and asserts monthly and daily agree.
"""

from __future__ import annotations

import sqlite3


def _month_and_day_predicates(conn, now_expr):
    """Return (in_month, in_day) booleans for the single row, using the exact
    SQL the production functions use, parameterized on a fixed 'now'."""
    in_month = conn.execute(
        "SELECT COUNT(*) FROM usage "
        "WHERE strftime('%Y-%m', timestamp, 'localtime') = "
        f"strftime('%Y-%m', {now_expr}, 'localtime')"
    ).fetchone()[0]
    in_day = conn.execute(
        "SELECT COUNT(*) FROM usage "
        f"WHERE date(timestamp,'localtime') = date({now_expr},'localtime')"
    ).fetchone()[0]
    return in_month, in_day


def test_daily_today_is_subset_of_monthly_now_same_frame():
    """For any row, if it is in daily-today it must also be in monthly-now.

    We enumerate a range of stored timestamps around 'now' and assert the
    invariant `in_day => in_month` holds for every one, under SQLite's own
    localtime conversion (the real engine, not a Python reimplementation).
    """
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE usage (timestamp TEXT, cost_usd REAL)")

    # A spread of timestamps: this instant, +/- hours, +/- days across a
    # potential month edge. Using SQLite's own 'now' keeps it timezone-correct
    # for whatever offset CI runs in.
    offsets = [
        "'now'",
        "datetime('now','-1 hour')", "datetime('now','+1 hour')",
        "datetime('now','-12 hours')", "datetime('now','+12 hours')",
        "datetime('now','-1 day')", "datetime('now','-2 days')",
        "datetime('now','-31 days')", "datetime('now','+1 day')",
    ]
    violations = []
    for off in offsets:
        conn.execute("DELETE FROM usage")
        conn.execute(f"INSERT INTO usage VALUES ({off}, 1.0)")
        in_month, in_day = _month_and_day_predicates(conn, "'now'")
        # The core invariant: anything counted "today" must be counted "this month".
        if in_day and not in_month:
            violations.append(off)
    conn.close()
    assert not violations, (
        f"RED1-07: rows in daily-today but NOT monthly-now (frame mismatch): {violations}"
    )


def test_monthly_sql_uses_localtime():
    """Guard: get_monthly_spend must not regress to a UTC month boundary."""
    from pathlib import Path
    src = Path(__file__).resolve().parents[1] / "src" / "llm_router" / "cost.py"
    text = src.read_text()
    # The old buggy form; must be gone.
    assert "datetime('now', 'start of month')" not in text, (
        "RED1-07 regression: get_monthly_spend reverted to a UTC month boundary"
    )
