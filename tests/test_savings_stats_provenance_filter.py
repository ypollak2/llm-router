"""savings_stats must get the same provenance/test filter every other money
table gets in dashboard_data.py.

Measured 2026-09-27 on ~/.llm-router/usage.db: savings_stats (9,057 rows) was
read in ``query_window``/``query_daily`` with NO ``is_simulated`` filter at
all, while ``usage``/``claude_usage``/``codex_usage``/``gemini_usage`` all
gate their money sums through ``_production_pred``. 17% of savings_stats
(1,559 of 9,057 rows) is test traffic. A test-session row could reach the
headline through this one table alone.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from llm_router import dashboard_data

_NOW = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _db_with_mixed_provenance(tmp_path):
    path = tmp_path / "usage.db"
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE savings_stats (
        timestamp TEXT, session_id TEXT, host TEXT, model_used TEXT,
        estimated_claude_cost_saved REAL, mode TEXT, is_simulated INTEGER
    )""")
    # Production row: a real routed call, is_simulated=0.
    conn.execute(
        "INSERT INTO savings_stats VALUES (?, ?, ?, ?, ?, ?, ?)",
        (_NOW, "b9f04425-6176-4bea-b46e-1cfdfd44785e", "claude_code",
         "ollama/qwen3", 2.00, "block", 0),
    )
    # Test-session row: a benchmark/test run whose is_simulated was never
    # stamped (NULL) — the exact shape of the 587-id gap this defect covers.
    conn.execute(
        "INSERT INTO savings_stats VALUES (?, ?, ?, ?, ?, ?, ?)",
        (_NOW, "wiring-sess", "claude_code", "ollama/qwen3", 50.00, "block", None),
    )
    conn.commit()
    conn.close()
    return path


def test_a_test_session_row_is_excluded_from_the_headline(tmp_path):
    """The $50.00 test-session row must not inflate the headline the $2.00
    production row is entitled to."""
    path = _db_with_mixed_provenance(tmp_path)
    totals = dashboard_data.query_window("lifetime", db_path=path)

    assert totals.by_source["savings_stats"]["saved_usd"] == 2.00, (
        "a test-session row (is_simulated NULL) reached the money figure"
    )


def test_a_test_session_row_is_excluded_from_the_daily_series(tmp_path):
    path = _db_with_mixed_provenance(tmp_path)
    rows = dashboard_data.query_daily(14, db_path=path)
    assert sum(r.saved_usd for r in rows) == 2.00


def test_explicitly_marked_simulated_row_is_also_excluded(tmp_path):
    """The narrower case: is_simulated=1 (confirmed test), not just NULL."""
    path = tmp_path / "usage.db"
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE savings_stats (
        timestamp TEXT, session_id TEXT, host TEXT, model_used TEXT,
        estimated_claude_cost_saved REAL, mode TEXT, is_simulated INTEGER
    )""")
    conn.execute(
        "INSERT INTO savings_stats VALUES (?, ?, ?, ?, ?, ?, ?)",
        (_NOW, "real-session", "claude_code", "ollama/qwen3", 3.00, "block", 0),
    )
    conn.execute(
        "INSERT INTO savings_stats VALUES (?, ?, ?, ?, ?, ?, ?)",
        (_NOW, "bench-session", "claude_code", "ollama/qwen3", 99.00, "block", 1),
    )
    conn.commit()
    conn.close()
    totals = dashboard_data.query_window("lifetime", db_path=path)
    assert totals.by_source["savings_stats"]["saved_usd"] == 3.00
