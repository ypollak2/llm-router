"""Regression: DIRECT-routed (free-provider) calls must contribute their token
counts to the dashboard totals. Before the v7.4 fix, savings_stats had no token
columns, so the dashboard under-counted tokens for Ollama/Codex routing.
"""
import json
import pathlib
import sqlite3
import types
from datetime import datetime, timezone

from llm_router import dashboard_data as dd
from llm_router.hooks import savings_logger as sl

_TS = datetime.now(timezone.utc).isoformat()


def _mk(db, with_tokens: bool):
    c = sqlite3.connect(db)
    cols = ("input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0" if with_tokens else "")
    extra = (", input_tokens, output_tokens" if with_tokens else "")
    vals = ((350, 480) if with_tokens else ())
    c.execute(
        "CREATE TABLE savings_stats(id INTEGER PRIMARY KEY AUTOINCREMENT,timestamp TEXT,session_id TEXT,"
        "task_type TEXT,estimated_claude_cost_saved REAL,external_cost REAL,model_used TEXT,"
        "host TEXT DEFAULT 'claude_code'" + (", " + cols if cols else "") + ")"
    )
    c.execute(
        "INSERT INTO savings_stats(timestamp,session_id,task_type,estimated_claude_cost_saved,"
        "external_cost,model_used,host" + extra + ") VALUES(?,?,?,?,?,?,?" + (",?,?" if with_tokens else "") + ")",
        (_TS, "s", "query", 0.012, 0.0, "ollama/hermes3:8b", "claude_code", *vals),
    )
    c.commit()
    c.close()


def test_savings_stats_tokens_counted(tmp_path):
    db = str(tmp_path / "test.db")
    _mk(db, with_tokens=True)
    wt = dd.query_window("lifetime", db_path=db)
    assert wt.calls == 1
    assert wt.tokens == 830  # 350 + 480


def test_old_schema_without_token_columns_is_graceful(tmp_path):
    db = str(tmp_path / "test_old.db")
    _mk(db, with_tokens=False)
    wt = dd.query_window("lifetime", db_path=db)
    assert wt.calls == 1 and wt.tokens == 0  # no crash, defaults to 0


def test_savings_logger_persists_token_counts(monkeypatch, tmp_path):
    monkeypatch.setattr(pathlib.Path, "home", staticmethod(lambda: tmp_path))
    fake = types.SimpleNamespace(
        model=types.SimpleNamespace(provider="ollama", model="hermes3:8b"),
        input_tokens=350, output_tokens=480,
    )
    sl.log_direct_savings(fake, "query", "simple", "sess1")
    rec = json.loads((tmp_path / ".llm-router" / "savings_log.jsonl").read_text().strip())
    assert rec["input_tokens"] == 350
    assert rec["output_tokens"] == 480
