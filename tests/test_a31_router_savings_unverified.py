"""A saving nobody observed being used is not a headline saving.

Measured 2026-09-24 over all 8,969 savings_stats rows:

    host / writer                                   rows   saved     gated?
    claude_code, hook, after the realized gate     1,081   $0.00     yes
    claude_code, hook, before the gate                67   $1.13     no
    claude_code, agentic telemetry (hardcoded)       517   $103.40   no — flat $0.20
    router / gateway / sdk (log_receipt_savings)   7,304   $6.29     no

Only the UserPromptSubmit hook's `realized` gate observes whether a routed answer
REPLACED Claude's turn. Everything else is kept and shown as "unverified", and
never enters a headline. The ONE place that decides this is
`savings.VERIFIED_SAVED_SQL`; every query site goes through it.
"""
from __future__ import annotations

import asyncio
import pathlib
import sqlite3
from datetime import datetime, timezone

from llm_router import dashboard_data
from llm_router.savings import (
    REALIZED_GATE_SINCE, UNVERIFIED_CALLS_SQL, UNVERIFIED_SAVED_SQL,
    VERIFIED_SAVED_SQL, unverified_note,
)

_DDL = """CREATE TABLE savings_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL, session_id TEXT, task_type TEXT,
    estimated_claude_cost_saved REAL NOT NULL, external_cost REAL DEFAULT 0,
    model_used TEXT, host TEXT, input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0, mode TEXT, is_simulated INTEGER)"""

_NOW = datetime.now(timezone.utc).isoformat()
_PRE_GATE = "2026-09-01T12:00:00+00:00"

#: (label, timestamp, host, model, saved) — distinct powers of two so every sum
#: identifies exactly which rows it included.
_ROWS = [
    ("hook, gated",        _NOW,      "claude_code", "ollama/qwen3.5:latest",     0.5),
    ("router",             _NOW,      "router",      "ollama/qwen3-coder:30b",    1.0),
    ("gateway",            _NOW,      "gateway",     "ollama/qwen3.5:latest",     2.0),
    ("sdk",                _NOW,      "sdk",         "ollama/qwen3.5:latest",     4.0),
    ("NULL host",          _NOW,      None,          "ollama/qwen3.5:latest",     8.0),
    ("agentic as hook",    _NOW,      "claude_code", "llm_router-agentic-router", 16.0),
    ("hook, pre-gate",     _PRE_GATE, "claude_code", "ollama/qwen3.5:latest",     32.0),
]


def _db(tmp_path, rows=_ROWS) -> pathlib.Path:
    path = tmp_path / "usage.db"
    conn = sqlite3.connect(path)
    conn.execute(_DDL)
    for _, ts, host, model, saved in rows:
        conn.execute(
            "INSERT INTO savings_stats (timestamp, session_id, task_type, "
            "estimated_claude_cost_saved, model_used, host, is_simulated) "
            "VALUES (?, 's1', 'query', ?, ?, ?, 0)", (ts, saved, model, host))
    conn.commit()
    conn.close()
    return path


def _sums(path) -> tuple[float, float, int]:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            f"SELECT SUM({VERIFIED_SAVED_SQL}), SUM({UNVERIFIED_SAVED_SQL}), "
            f"SUM({UNVERIFIED_CALLS_SQL}) FROM savings_stats").fetchone()
    finally:
        conn.close()


# ── The predicate ─────────────────────────────────────────────────────────

def test_premise_the_pre_gate_row_is_before_the_gate():
    assert _PRE_GATE < REALIZED_GATE_SINCE < _NOW


def test_only_the_gated_hook_row_is_verified(tmp_path):
    verified, unverified, n = _sums(_db(tmp_path))
    assert verified == 0.5
    assert unverified == 1 + 2 + 4 + 8 + 16 + 32
    assert n == 6


def test_verified_and_unverified_partition_the_ledger(tmp_path):
    verified, unverified, _ = _sums(_db(tmp_path))
    assert verified + unverified == sum(r[-1] for r in _ROWS)


def test_an_unknown_host_is_unverified_not_dropped_and_not_verified(tmp_path):
    """S9: unknown must not render as the favourable answer — and must not
    vanish either, or the partition above silently stops adding up."""
    only_null = [r for r in _ROWS if r[2] is None]
    verified, unverified, n = _sums(_db(tmp_path, only_null))
    assert (verified, unverified, n) == (0, 8.0, 1)


# ── The surfaces ──────────────────────────────────────────────────────────

def test_query_window_keeps_unverified_out_of_the_headline(tmp_path):
    t = dashboard_data.query_window("lifetime", db_path=_db(tmp_path))
    assert t.saved_usd == 0.5
    assert t.unverified_saved_usd == 63.0
    assert t.unverified_calls == 6
    # The existing invariant still holds over the verified figure.
    assert t.saved_usd == sum(s["saved_usd"] for s in t.by_source.values())


def test_query_daily_keeps_unverified_out_of_the_chart(tmp_path):
    recent = [r for r in _ROWS if r[1] == _NOW]
    rows = dashboard_data.query_daily(14, db_path=_db(tmp_path, recent))
    assert sum(r.saved_usd for r in rows) == 0.5
    assert sum(r.unverified_saved_usd for r in rows) == 1 + 2 + 4 + 8 + 16


def test_the_savings_report_labels_unverified_beside_the_verified_total(
        tmp_path, monkeypatch):
    from llm_router.commands import savings_report
    monkeypatch.setattr(savings_report, "_get_db_path", lambda: _db(tmp_path))
    out = savings_report.render_savings_report("all")
    assert "savings_stats ledger: $0.5000 verified across 7 routed call(s)" in out
    assert "+ $63.00 unverified" in out and "n=6" in out


def test_the_savings_report_prints_no_unverified_line_when_there_is_none(
        tmp_path, monkeypatch):
    from llm_router.commands import savings_report
    gated = [r for r in _ROWS if r[0] == "hook, gated"]
    monkeypatch.setattr(savings_report, "_get_db_path",
                        lambda: _db(tmp_path, gated))
    out = savings_report.render_savings_report("all")
    assert "savings_stats ledger: $0.5000 verified" in out
    assert "unverified" not in out


def test_unverified_note_is_empty_at_zero_and_keeps_small_amounts_visible():
    assert unverified_note(0.0, 0) == ""
    assert "$0.0005" in unverified_note(0.0005, 1)
    assert "$6.25" in unverified_note(6.25, 7006)


# ── The writer that mislabelled itself ────────────────────────────────────

def test_agentic_telemetry_no_longer_claims_the_hook_host(tmp_path, monkeypatch):
    from llm_router.agentic import telemetry
    db = tmp_path / "agentic.db"
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(db))
    asyncio.run(telemetry.record_delegation_savings(
        {"savings": {"saved_usd": 0.2, "actual_usd": 0.0}}))
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute("SELECT host FROM savings_stats").fetchall()
    finally:
        conn.close()
    assert rows == [("agentic",)], rows


def test_a_table_predating_host_counts_as_unverified_not_as_an_error(tmp_path):
    """Old schemas cannot say who wrote a row: everything is unverified, and
    the query still answers instead of failing (Codex Stop printed $0 for it)."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE savings_stats (timestamp TEXT, "
                 "estimated_claude_cost_saved REAL)")
    conn.execute("INSERT INTO savings_stats VALUES (?, 1.25)", (_NOW,))
    conn.commit()
    conn.close()
    t = dashboard_data.query_window("lifetime", db_path=path)
    assert (t.saved_usd, t.unverified_saved_usd, t.unverified_calls) == (0.0, 1.25, 1)
    rows = dashboard_data.query_daily(14, db_path=path)
    assert sum(r.unverified_saved_usd for r in rows) == 1.25


def test_the_python_twin_agrees_with_the_sql_row_for_row(tmp_path):
    """surface_status sums in Python; the rule must not fork. Includes a
    mixed-case agentic model, because SQLite LIKE is case-insensitive."""
    from llm_router.savings import is_verified_saving
    rows = _ROWS + [("agentic, mixed case", _NOW, "claude_code",
                     "LLM_Router-Agentic-Router", 64.0),
                    ("NULL model", _NOW, "claude_code", None, 128.0)]
    path = _db(tmp_path, rows)
    conn = sqlite3.connect(path)
    try:
        sql = conn.execute(
            f"SELECT host, model_used, timestamp, "
            f"CASE WHEN {VERIFIED_SAVED_SQL} > 0 THEN 1 ELSE 0 END "
            f"FROM savings_stats ORDER BY id").fetchall()
    finally:
        conn.close()
    assert len(sql) == len(rows)
    assert sum(r[3] for r in sql) == 1, "premise: exactly one verified row seeded"
    for host, model, ts, verified in sql:
        assert is_verified_saving(host, model, ts) == bool(verified), (host, model, ts)
