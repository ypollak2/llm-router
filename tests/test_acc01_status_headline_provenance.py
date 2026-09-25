"""ACC-01: `llm-router status`'s headline counts only production rows.

Audit 2026-09-24 (09_accounting.md ACC-01, numbers corrected in
13_verify_storage_accounting.md): dashboard_data summed the `usage` and the
per-platform tables with no provenance filter, so the status headline said
$372.66 "saved" while the canonical accessor (cost.get_realized_savings, which
applies cost.production_only: `COALESCE(is_simulated,1) = 0`) said ~$0.11 for
the same tables at the same instant. 96 sentinel claude_usage rows alone
($3.00 each, NULL provenance) contributed $288.

PR6 (North Star 8+12) superseded this file's original fix. ACC-01 had these
tables' PRODUCTION rows (is_simulated=0) reach the "verified" headline and
only non-production rows land in "unverified" — but `_production_pred` is a
test-traffic filter, not a "was this draft actually used" signal, and using
it as the verified axis was itself the S9 shape ("unknown/adverse treated as
favourable") this project keeps re-finding. `usage`/`claude_usage`/
`codex_usage`/`gemini_usage` carry no "used" column at all, so NONE of their
money can be verified — only `savings_stats`'s `mode='block'` row (PR #147,
`savings.VERIFIED_SAVED_SQL`) can be. `_production_pred` now does only its
original, narrower job: dropping non-production (test/sentinel/unknown-
provenance) rows from the money figure entirely, rather than promoting the
rest to "verified". Unknown must not render as the favourable answer (S9) —
and, per this PR, a fixture row must not render as a real, if unconfirmed,
user saving either.
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
    """PR6: `claude_usage` has no "used" column, so NOTHING it contributes can
    be verified — the production row (is_simulated=0, $1.00) now lands in
    `unverified_saved_usd`, same as before. The non-production rows (NULL and
    is_simulated=1, $3.00 + $5.00) are dropped entirely rather than folded
    into "unverified": a sentinel/unknown-provenance row is not a real, if
    unconfirmed, user saving."""
    t = dashboard_data.query_window("lifetime", db_path=_db(tmp_path))
    assert t.saved_usd == 0.0
    assert t.unverified_saved_usd == 1.0
    assert t.unverified_calls == 1
    # Activity still counts every row; only the money is split/dropped.
    assert t.calls == 3


def test_the_by_source_invariant_still_holds(tmp_path):
    t = dashboard_data.query_window("lifetime", db_path=_db(tmp_path))
    assert t.saved_usd == sum(s["saved_usd"] for s in t.by_source.values())


def test_a_table_without_a_provenance_column_contributes_nothing(tmp_path):
    """No `is_simulated` column at all means provenance can never be
    established, so `_production_pred` (now purely a drop filter) treats
    every row as failing it. Nothing reaches EITHER bucket — not verified
    (never possible for this table) and not unverified either, since a row
    that might be a test fixture is not a real user saving."""
    t = dashboard_data.query_window("lifetime", db_path=_db(tmp_path, with_provenance=False))
    assert (t.saved_usd, t.unverified_saved_usd) == (0.0, 0.0)


def test_daily_series_splits_the_same_way(tmp_path):
    rows = dashboard_data.query_daily(14, db_path=_db(tmp_path))
    assert sum(r.saved_usd for r in rows) == 0.0
    assert sum(r.unverified_saved_usd for r in rows) == 1.0


def test_legacy_usage_table_is_split_too(tmp_path):
    """A production row and a NULL-provenance twin with DIFFERENT token
    counts, so the assertion can tell whether the NULL twin was dropped
    (unverified == the production row alone) or incorrectly folded in
    (unverified == both rows)."""
    path = tmp_path / "usage.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE usage (timestamp TEXT, provider TEXT, success INTEGER, "
                 "input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL, "
                 "is_simulated INTEGER)")
    # Production: 1,000,000 input tokens. NULL-provenance twin: 4,000,000 —
    # 4x, so a bug that folds it in is unmissable rather than off by a
    # coincidentally-equal amount.
    conn.execute(f"INSERT INTO usage VALUES ({_NOW}, 'ollama', 1, 1000000, 0, 0.0, 0)")
    conn.execute(f"INSERT INTO usage VALUES ({_NOW}, 'ollama', 1, 4000000, 0, 0.0, NULL)")
    conn.commit()
    conn.close()
    t = dashboard_data.query_window("lifetime", db_path=path)
    assert t.saved_usd == 0.0, "usage has no 'used' column — never verified"
    assert t.unverified_saved_usd > 0, "premise: the production row is credited somewhere"
    from llm_router import pricing
    baseline_model = dashboard_data._BASELINE_MODEL
    expected = 1_000_000 * pricing.input_rate(baseline_model) / 1_000_000
    assert abs(t.unverified_saved_usd - expected) < 1e-9, (
        "unverified_saved_usd must equal the PRODUCTION row alone — the "
        "NULL-provenance twin (4x the tokens) must be dropped, not folded in"
    )


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
    # PR6: claude_usage's $1.00 production row (was verified pre-PR6) now
    # joins unverified too; its $3.00/$5.00 non-production twins are dropped.
    # savings_stats's $16.00 (host='router', no mode) is unverified either way.
    assert t.unverified_saved_usd == 1.0 + 16.0
    assert t.unverified_calls == 2
