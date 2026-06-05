"""Regression tests for the v10.1.4 dashboard scope/token rollup fixes.

Three behaviours under test:

1. ``_query_cumulative_savings`` rolls tokens from ``claude_usage`` /
   ``codex_usage`` / ``gemini_usage`` into the cumulative totals so the
   ``today`` row in the SAVINGS panel shows a token count even when the
   legacy ``usage`` table has no rows for today.

2. ``_query_routing_logic`` uses a start-of-day cutoff (not the session
   start) so the ROUTING panel's window matches the SAVINGS panel's
   ``today`` scope. Before this change the panels mixed two windows
   without a label, which made apparent discrepancies impossible to
   reason about.

3. ``llm-router explain-dashboard`` (commands.explain_dashboard) prints
   per-panel diagnostics — source tables, windows, row counts — so any
   future drift is debuggable in seconds rather than requiring a code
   read.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).parent.parent


@pytest.fixture
def fake_state_dir(tmp_path, monkeypatch):
    """Temp ~/.llm-router with empty DB + tracking JSONL.

    Patches the module-level constants that ``session-end.py`` and the
    explain-dashboard command resolve at import time, so writes/reads
    land in the test directory instead of the real one.
    """
    state = tmp_path / ".llm-router"
    state.mkdir()
    db = state / "usage.db"
    tracking = state / "model_tracking.jsonl"

    monkeypatch.setenv("HOME", str(tmp_path))
    return state, db, tracking


def _seed_claude_usage(db: Path, *, tokens: int, saved: float) -> None:
    conn = sqlite3.connect(str(db))
    conn.execute(
        """CREATE TABLE IF NOT EXISTS claude_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT DEFAULT (datetime('now')),
            model TEXT NOT NULL,
            tokens_used INTEGER NOT NULL,
            complexity TEXT NOT NULL,
            cost_saved_usd REAL NOT NULL DEFAULT 0,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0
        )"""
    )
    conn.execute(
        "INSERT INTO claude_usage (model, tokens_used, complexity, cost_saved_usd) "
        "VALUES (?, ?, ?, ?)",
        ("claude-haiku-4-5", tokens, "auto", saved),
    )
    conn.commit()
    conn.close()


def _seed_savings_stats(db: Path, *, count: int, total_saved: float) -> None:
    conn = sqlite3.connect(str(db))
    conn.execute(
        """CREATE TABLE IF NOT EXISTS savings_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            session_id TEXT NOT NULL,
            task_type TEXT NOT NULL,
            estimated_claude_cost_saved REAL NOT NULL,
            external_cost REAL NOT NULL,
            model_used TEXT NOT NULL,
            host TEXT NOT NULL DEFAULT 'claude_code'
        )"""
    )
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    per_row = total_saved / max(count, 1)
    for _ in range(count):
        conn.execute(
            "INSERT INTO savings_stats (timestamp, session_id, task_type, "
            "estimated_claude_cost_saved, external_cost, model_used) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (now, "s1", "code", per_row, 0.0, "ollama/qwen2.5:7b"),
        )
    conn.commit()
    conn.close()


def _seed_tracking_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


# ── 1. Token rollup ──────────────────────────────────────────────────────────


def test_cumulative_rolls_claude_usage_tokens(fake_state_dir, monkeypatch):
    """SAVINGS today row must show tokens_used from claude_usage."""
    state, db, _ = fake_state_dir
    _seed_claude_usage(db, tokens=1500, saved=0.42)

    # Patch the session-end module's constants to point at the fake state.
    import importlib
    sys.path.insert(0, str(PROJECT_ROOT / "src"))
    se = importlib.import_module("llm_router.hooks").__path__  # noqa: F841
    spec = importlib.util.spec_from_file_location(
        "session_end_test",
        PROJECT_ROOT / "src" / "llm_router" / "hooks" / "session-end.py",
    )
    se_mod = importlib.util.module_from_spec(spec)
    monkeypatch.setattr(spec.loader, "exec_module", spec.loader.exec_module)
    spec.loader.exec_module(se_mod)

    monkeypatch.setattr(se_mod, "DB_PATH", str(db))
    monkeypatch.setattr(se_mod, "STATE_DIR", str(state))

    result = se_mod._query_cumulative_savings()
    today = [r for r in result if r[0] == "today"]
    assert today, "today row missing from cumulative result"
    label, calls, total_in, total_out, saved = today[0]
    # claude_usage's tokens_used (1500) should land in total_in via the rollup.
    assert (total_in + total_out) == 1500, (
        f"expected 1500 rolled tokens, got in={total_in} out={total_out}"
    )
    assert calls == 1
    assert saved == pytest.approx(0.42, abs=0.0001)


# ── 2. ROUTING window = today ────────────────────────────────────────────────


def test_routing_logic_uses_today_cutoff(fake_state_dir, monkeypatch):
    """_query_routing_logic must filter by start-of-day, not session start."""
    state, _db, tracking = fake_state_dir

    now = time.time()
    start_of_day = (
        __import__("datetime")
        .datetime.now()
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .timestamp()
    )

    # Three rows: two today, one yesterday.
    _seed_tracking_jsonl(
        tracking,
        [
            {"timestamp": now - 5, "classification_method": "heuristic",
             "classification_confidence": 1.0},
            {"timestamp": now - 100, "classification_method": "heuristic-weak",
             "classification_confidence": 0.5},
            {"timestamp": start_of_day - 3600, "classification_method": "ollama",
             "classification_confidence": 0.9},  # yesterday — must be excluded
        ],
    )

    # Need usage.db to exist for the function to proceed.
    sqlite3.connect(str(_db)).close()

    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "session_end_test2",
        PROJECT_ROOT / "src" / "llm_router" / "hooks" / "session-end.py",
    )
    se_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(se_mod)
    monkeypatch.setattr(se_mod, "DB_PATH", str(_db))
    monkeypatch.setattr(se_mod, "STATE_DIR", str(state))

    # Pass an arbitrary session_start far in the past — the function should
    # IGNORE it now and use start-of-day instead.
    result = se_mod._query_routing_logic(session_start=now - 86400 * 30)
    methods = {r["method"]: r["hits"] for r in result}
    assert "heuristic" in methods
    assert "heuristic-weak" in methods
    assert "ollama" not in methods, (
        f"yesterday's ollama entry should be filtered by start-of-day cutoff, "
        f"got methods: {methods}"
    )


# ── 3. explain-dashboard prints the per-panel breakdown ──────────────────────


def test_daily_14d_includes_v93_tables(fake_state_dir, monkeypatch):
    """14-DAY ACTIVITY chart must include per-platform tables, not just `usage`.

    Pre-v10.1.5 the chart only queried `usage` so days that routed entirely
    through claude_usage / codex_usage / gemini_usage / savings_stats were
    invisible. Today's row is the most affected (subscription routing →
    claude_usage), and the chart underreported by 50%+ in practice.
    """
    state, db, _ = fake_state_dir

    # Seed a `usage` row for today so the table exists (also tests the
    # legacy path doesn't double-count).
    conn = sqlite3.connect(str(db))
    conn.execute("""CREATE TABLE IF NOT EXISTS usage (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT DEFAULT (datetime('now')),
        model TEXT NOT NULL,
        provider TEXT NOT NULL,
        input_tokens INTEGER DEFAULT 0,
        output_tokens INTEGER DEFAULT 0,
        cost_usd REAL DEFAULT 0.0,
        success INTEGER DEFAULT 1
    )""")
    conn.execute(
        "INSERT INTO usage (model, provider, input_tokens, output_tokens, cost_usd, success) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("ollama/qwen2.5:7b", "ollama", 100, 50, 0.0, 1),
    )
    conn.commit()
    conn.close()

    # Also seed claude_usage with 1500 tokens_used for today.
    _seed_claude_usage(db, tokens=1500, saved=0.42)

    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "se_daily14d_test",
        PROJECT_ROOT / "src" / "llm_router" / "hooks" / "session-end.py",
    )
    se_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(se_mod)
    monkeypatch.setattr(se_mod, "DB_PATH", str(db))

    rows = se_mod._query_daily_14d()
    assert rows, "expected at least one day of activity"

    total_calls = sum(r[1] for r in rows)
    total_tokens = sum(r[2] for r in rows)
    # usage row contributes 1 call + 150 tokens; claude_usage contributes
    # 1 call + 1500 tokens. Total = 2 calls, 1650 tokens.
    assert total_calls == 2, f"expected 2 calls (1 usage + 1 claude_usage), got {total_calls}"
    assert total_tokens == 1650, (
        f"expected 1650 tokens (150 usage + 1500 claude_usage), got {total_tokens}"
    )


def test_explain_dashboard_prints_sources(fake_state_dir, monkeypatch, capsys):
    """explain-dashboard must print panel sources and the today totals."""
    state, db, tracking = fake_state_dir
    _seed_claude_usage(db, tokens=1000, saved=0.50)
    _seed_savings_stats(db, count=17, total_saved=0.023)
    _seed_tracking_jsonl(
        tracking,
        [{"timestamp": time.time() - 10, "classification_method": "heuristic",
          "classification_confidence": 1.0}],
    )

    # Patch the module's path constants.
    from llm_router.commands import explain_dashboard as ed
    monkeypatch.setattr(ed, "DB_PATH", db)
    monkeypatch.setattr(ed, "TRACKING_PATH", tracking)
    monkeypatch.setattr(ed, "STATE_DIR", state)

    rc = ed.cmd_explain_dashboard()
    assert rc == 0
    out = capsys.readouterr().out
    # Each panel section must appear.
    assert "ROUTING panel" in out
    assert "SAVINGS panel" in out
    assert "14-DAY ACTIVITY panel" in out
    # Source labels.
    assert "model_tracking.jsonl" in out
    assert "claude_usage" in out
    assert "savings_stats" in out
    # Today's totals (1000 tokens, ~$0.52 combined).
    assert "1,000" in out
    assert "$0.5234" in out or "$0.52" in out
