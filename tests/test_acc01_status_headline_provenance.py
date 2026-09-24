"""ACC-01: `llm-router status`'s headline counts only production rows.

Audit 2026-09-24 (09_accounting.md ACC-01, numbers corrected in
13_verify_storage_accounting.md): dashboard_data summed the `usage` and the
per-platform tables with no provenance filter, so the status headline said
$372.66 "saved" while the canonical accessor (cost.get_realized_savings, which
applies cost.production_only: `COALESCE(is_simulated,1) = 0`) said ~$0.11 for
the same tables at the same instant. 96 sentinel claude_usage rows alone
($3.00 each, NULL provenance) contributed $288.

Rows whose provenance was never established are now UNVERIFIED — shown
beside the headline, labelled, never in it — the same rule 3c96d23 applied to
savings_stats. Unknown must not render as the favourable answer (S9).
"""
from __future__ import annotations

import sqlite3

from llm_router import dashboard_data

_NOW = "datetime('now')"


def _db(tmp_path, *, with_provenance=True):
    path = tmp_path / "usage.db"
    conn = sqlite3.connect(path)
    sim = ", is_simulated INTEGER" if with_provenance else ""
    conn.execute(f"CREATE TABLE claude_usage (timestamp TEXT, tokens_used INTEGER, "
                 f"cost_saved_usd REAL{sim})")
    rows = [(0, 1.0), (None, 3.0), (1, 5.0)] if with_provenance else [(None, 3.0)]
    for prov, saved in rows:
        if with_provenance:
            conn.execute(f"INSERT INTO claude_usage VALUES ({_NOW}, 100, ?, ?)", (saved, prov))
        else:
            conn.execute(f"INSERT INTO claude_usage VALUES ({_NOW}, 100, ?)", (saved,))
    conn.commit()
    conn.close()
    return path


def test_only_production_rows_reach_the_headline(tmp_path):
    t = dashboard_data.query_window("lifetime", db_path=_db(tmp_path))
    assert t.saved_usd == 1.0
    assert t.unverified_saved_usd == 3.0 + 5.0
    assert t.unverified_calls == 2
    # Activity still counts every row; only the money is split.
    assert t.calls == 3


def test_the_by_source_invariant_still_holds(tmp_path):
    t = dashboard_data.query_window("lifetime", db_path=_db(tmp_path))
    assert t.saved_usd == sum(s["saved_usd"] for s in t.by_source.values())


def test_a_table_without_a_provenance_column_is_unverified(tmp_path):
    t = dashboard_data.query_window("lifetime", db_path=_db(tmp_path, with_provenance=False))
    assert (t.saved_usd, t.unverified_saved_usd) == (0.0, 3.0)


def test_daily_series_splits_the_same_way(tmp_path):
    rows = dashboard_data.query_daily(14, db_path=_db(tmp_path))
    assert sum(r.saved_usd for r in rows) == 1.0
    assert sum(r.unverified_saved_usd for r in rows) == 8.0


def test_legacy_usage_table_is_split_too(tmp_path):
    path = tmp_path / "usage.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE usage (timestamp TEXT, provider TEXT, success INTEGER, "
                 "input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL, "
                 "is_simulated INTEGER)")
    for prov in (0, None):
        conn.execute(f"INSERT INTO usage VALUES ({_NOW}, 'ollama', 1, 1000000, 0, 0.0, ?)",
                     (prov,))
    conn.commit()
    conn.close()
    t = dashboard_data.query_window("lifetime", db_path=path)
    assert t.saved_usd > 0, "premise: a production row is credited"
    assert abs(t.unverified_saved_usd - t.saved_usd) < 1e-9, \
        "the NULL-provenance twin row must land in unverified, not the headline"


def test_unverified_accumulates_across_every_table(tmp_path):
    """Regression: the savings_stats block ASSIGNED the unverified total, so a
    database with both a platform table and savings_stats silently lost the
    platform's unverified money."""
    path = _db(tmp_path)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE savings_stats (timestamp TEXT, host TEXT, model_used TEXT, "
                 "estimated_claude_cost_saved REAL, input_tokens INTEGER, output_tokens INTEGER)")
    conn.execute("INSERT INTO savings_stats VALUES (strftime('%Y-%m-%dT%H:%M:%S','now'), "
                 "'router', 'ollama/x', 16.0, 1, 1)")
    conn.commit()
    conn.close()
    t = dashboard_data.query_window("lifetime", db_path=path)
    assert t.unverified_saved_usd == 3.0 + 5.0 + 16.0
    assert t.unverified_calls == 3
