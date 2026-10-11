"""LEDGER-ERR-1: error rows (``usage.reason`` ``error_*``, success=0) are not served calls.

The router now writes one such row per failed call (tests/test_ledger_err1_error_row.py). A
consumer that counts "usage rows" must leave them out, like cache rows (provider_classes).
Consumers that already filter ``success = 1`` (share card, digest, session hooks, test_delta)
need nothing; the ones below did not.
"""
from __future__ import annotations

import datetime as _dt
import sqlite3
from pathlib import Path

from llm_router.provider_classes import SQL_NOT_ERROR_ROW, is_error_reason


def _db(tmp_path: Path) -> Path:
    db = tmp_path / "usage.db"
    c = sqlite3.connect(str(db))
    c.execute("CREATE TABLE usage (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT DEFAULT "
              "CURRENT_TIMESTAMP, model TEXT, provider TEXT, task_type TEXT, input_tokens INT, "
              "output_tokens INT, cost_usd REAL, latency_ms REAL, success INT DEFAULT 1, "
              "is_simulated INT DEFAULT 0, saved_usd REAL DEFAULT 0, reason TEXT)")
    rows = [("ollama/q", "ollama", 5, 3, 0.0, 4000.0, 1, "router_chain"),
            ("anthropic/claude-x", "anthropic", 5, 3, 0.01, 2000.0, 1, "router_chain"),
            # a failed call that burned 244 s on a local model, and one with no model at all
            ("ollama/qwen3-coder:30b", "ollama", 0, 0, 0.0, 244000.0, 0, "error_all_models_failed"),
            ("none", "none", 0, 0, 0.0, 3000.0, 0, "error_all_models_failed")]
    for m, p, i, o, cost, lat, ok, reason in rows:
        c.execute("INSERT INTO usage (model, provider, task_type, input_tokens, output_tokens, cost_usd, "
                  "latency_ms, success, reason) VALUES (?,?,?,?,?,?,?,?,?)", (m, p, "code", i, o, cost, lat, ok, reason))
    c.commit()
    c.close()
    return db


def test_predicate_and_helper_agree(tmp_path):
    db = _db(tmp_path)
    c = sqlite3.connect(str(db))
    kept = c.execute(f"SELECT COUNT(*) FROM usage WHERE {SQL_NOT_ERROR_ROW}").fetchone()[0]
    assert kept == 2
    assert is_error_reason("error_timeout") and not is_error_reason("router_chain") and not is_error_reason(None)


def test_statusline_mix_excludes_error_rows(tmp_path):
    from llm_router.statusline_segments import mix_segment
    _db(tmp_path)
    assert mix_segment(str(tmp_path)) == {"mix_local": "1", "mix_paid": "1"}


def test_statusline_literal_equals_the_shared_predicate():
    src = (Path(__file__).resolve().parent.parent / "src/llm_router/statusline_segments.py").read_text()
    assert SQL_NOT_ERROR_ROW.replace("\\", "\\\\") in src


def test_a_database_without_the_reason_column_is_read_unfiltered(tmp_path):
    from llm_router.provider_classes import not_error_row_sql
    c = sqlite3.connect(str(tmp_path / "old.db"))
    c.execute("CREATE TABLE usage (id INTEGER, model TEXT)")
    assert not_error_row_sql(c) == "1"


def test_routing_health_excludes_error_rows(tmp_path):
    from llm_router import routing_health
    db = _db(tmp_path)
    out = routing_health.routed_calls(days=2, db=db, today=_dt.date.today())
    assert sum(d["calls"] for d in out.values()) == 2
    assert sum(d["local"] for d in out.values()) == 1


def test_routing_report_excludes_error_rows(tmp_path, monkeypatch):
    from llm_router import routing_report
    _db(tmp_path)
    monkeypatch.setattr(routing_report, "_home", lambda: tmp_path)
    text = routing_report.generate_report()
    assert "qwen3-coder" not in text and "none" not in text.split("usage")[-1].lower().split("\n")[0]
    assert "244000" not in text and "244.0" not in text
