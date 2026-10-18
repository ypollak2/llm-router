"""F92 (rv-613): a cache row inside a session window is not an executed call.

Until now only the six-surface oracle pinned it. This seeds a minimal ``routing_decisions`` table
with real calls and cache rows INSIDE the session window and asserts the session scope counts
only the real ones, with the exact n, under both session filters (by id, by timestamp).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from llm_router import ledger_view as lv

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc).timestamp()
MIN = 60.0


def _ut(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _seed(path):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE routing_decisions (id INTEGER PRIMARY KEY, timestamp TEXT, "
                "task_type TEXT, final_provider TEXT, success INTEGER, reason_code TEXT, "
                "provenance TEXT, trace_id TEXT, session_id TEXT)")
    rows = [
        (10, "ollama", None, "s1"),          # real
        (20, "openai", None, "s1"),          # real
        (30, "openai", "cache_hit", "s1"),   # cache row by reason, in window
        (40, "cache", None, "s1"),           # cache row by provider, in window
        (50, "cache", "cache_hit", "s1"),    # cache row by both, in window
        (15, "openai", None, "other"),       # another session: out of the id scope
    ]
    for off, prov, reason, sid in rows:
        con.execute("INSERT INTO routing_decisions (timestamp, task_type, final_provider, success, "
                    "reason_code, provenance, session_id) VALUES (?,?,?,?,?,?,?)",
                    (_ut(NOW - off * MIN), "query", prov, 1, reason, "runtime", sid))
    con.commit()
    con.close()


def test_a_cache_row_in_the_session_window_is_excluded_by_session_id(tmp_path):
    db = tmp_path / "usage.db"
    _seed(db)
    lw = lv.window_summary("session", now=NOW, session_id="s1", db_path=db,
                           proxy_path=tmp_path / "none.jsonl", seats=None)
    assert lw.n == 2, lw
    assert dict(lw.executed) == {"ollama": 1, "openai": 1}
    assert "cache" not in dict(lw.executed)


def test_a_cache_row_in_the_session_window_is_excluded_by_timestamp(tmp_path):
    db = tmp_path / "usage.db"
    _seed(db)
    lw = lv.window_summary("session", now=NOW, session_start=NOW - 60 * MIN, db_path=db,
                           proxy_path=tmp_path / "none.jsonl", seats=None)
    assert lw.n == 3, lw                     # ollama + openai (s1) + openai (other); no cache row
    assert dict(lw.executed) == {"openai": 2, "ollama": 1}


def test_the_seed_really_holds_the_cache_rows(tmp_path):
    """Anti-vacuity: the cache rows exist in the window, so the exclusion above is doing work."""
    db = tmp_path / "usage.db"
    _seed(db)
    con = sqlite3.connect(db)
    in_scope = con.execute("SELECT COUNT(*) FROM routing_decisions WHERE session_id='s1'").fetchone()[0]
    con.close()
    assert in_scope == 5
