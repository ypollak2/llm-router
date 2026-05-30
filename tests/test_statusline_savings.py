"""Regression tests for the Claude Code statusline savings display.

Pre-v9.4.0 behaviour:
    ``hooks/statusline-command.sh`` queried only the ``usage`` table and computed
    its own Opus baseline from raw token counts. Two failure modes:

    1. Today's DIRECT routings (Ollama/Gemini/OpenAI executed in-hook by
       auto-route.py) were never logged to the ``usage`` table at all —
       they go to ``savings_log.jsonl`` and only land in ``savings_stats``
       at session-end. So a session driven entirely by DIRECT routing
       showed ``$0.00 saved`` in the live statusline.
    2. The hardcoded Opus rate overstated savings for simple/moderate
       tasks that would realistically route to Haiku/Sonnet (per the
       complexity-aware baseline that fix #2 introduced in cost.py).

Fix:
    statusline-command.sh now:
    - Prefers the populated ``saved_usd`` column when it's > 0 (v9.4.0+).
    - Falls back to the legacy Opus-token math for rows from older versions
      where saved_usd is still 0.0.
    - Adds un-flushed savings from ``savings_log.jsonl`` to the total so
      the live statusline includes the current session's DIRECT routings.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest


SCRIPT = (
    Path(__file__).parent.parent
    / "src"
    / "llm_router"
    / "hooks"
    / "statusline-command.sh"
)


@pytest.fixture
def fake_home(tmp_path):
    """Temp HOME with an empty .llm-router so the script writes/reads in isolation."""
    (tmp_path / ".llm-router").mkdir()
    return tmp_path


def _seed_usage_db(home: Path, rows: list[dict]) -> None:
    """Create usage.db with the v9.4.0+ schema and the given rows."""
    db = home / ".llm-router" / "usage.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """CREATE TABLE IF NOT EXISTS usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT DEFAULT (datetime('now')),
            model TEXT NOT NULL,
            provider TEXT NOT NULL,
            task_type TEXT,
            profile TEXT,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cost_usd REAL DEFAULT 0.0,
            latency_ms REAL DEFAULT 0.0,
            success INTEGER DEFAULT 1,
            baseline_model TEXT,
            potential_cost_usd REAL DEFAULT 0.0,
            saved_usd REAL DEFAULT 0.0,
            complexity TEXT DEFAULT 'moderate'
        )"""
    )
    cols = (
        "timestamp, model, provider, input_tokens, output_tokens, cost_usd, "
        "success, baseline_model, potential_cost_usd, saved_usd"
    )
    for r in rows:
        conn.execute(
            f"INSERT INTO usage ({cols}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                r["timestamp"],
                r["model"],
                r["provider"],
                r["input_tokens"],
                r["output_tokens"],
                r["cost_usd"],
                r["success"],
                r.get("baseline_model"),
                r.get("potential_cost_usd", 0.0),
                r.get("saved_usd", 0.0),
            ),
        )
    conn.commit()
    conn.close()


def _seed_savings_log(home: Path, records: list[dict]) -> None:
    path = home / ".llm-router" / "savings_log.jsonl"
    with path.open("w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _run_statusline(home: Path) -> str:
    """Run the statusline shell script with HOME pointed at the temp dir.

    The script consumes stdin (Claude Code pipes session JSON) so we send {}.
    """
    env = {**os.environ, "HOME": str(home), "LLM_ROUTER_ENFORCE": "soft"}
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        input="{}",
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    assert result.returncode == 0, f"script failed: {result.stderr}"
    return result.stdout


def _today_utc_iso() -> str:
    """Match SQLite default `datetime('now')` format — UTC, no T separator."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ── Tests ────────────────────────────────────────────────────────────────────


def test_uses_saved_usd_column_when_populated(fake_home):
    """v9.4.0+ rows have saved_usd populated — statusline must use it."""
    _seed_usage_db(
        fake_home,
        [
            {
                "timestamp": _today_utc_iso(),
                "model": "ollama/qwen3.5:latest",
                "provider": "ollama",
                "input_tokens": 1000,
                "output_tokens": 500,
                "cost_usd": 0.0,
                "success": 1,
                "baseline_model": "sonnet",
                "potential_cost_usd": 0.0105,
                "saved_usd": 0.0105,
            }
        ],
    )
    out = _run_statusline(fake_home)
    assert "$0.01 saved" in out, f"expected $0.01 saved, got: {out!r}"


def test_includes_pending_savings_log_jsonl(fake_home):
    """savings_log.jsonl (DIRECT routings) must contribute to the statusline."""
    _seed_savings_log(
        fake_home,
        [
            {
                "timestamp": _today_utc_iso(),
                "session_id": "s1",
                "task_type": "code",
                "estimated_saved": 0.012,
                "external_cost": 0.0,
                "model": "ollama/qwen3.5:latest",
                "host": "claude_code",
            }
        ],
    )
    out = _run_statusline(fake_home)
    assert "saved" in out
    assert "$0.01 saved" in out


def test_combines_db_and_jsonl(fake_home):
    """Persisted (usage.db) and pending (savings_log.jsonl) sum together."""
    _seed_usage_db(
        fake_home,
        [
            {
                "timestamp": _today_utc_iso(),
                "model": "gemini/gemini-2.5-flash",
                "provider": "gemini",
                "input_tokens": 1000,
                "output_tokens": 500,
                "cost_usd": 0.000225,
                "success": 1,
                "baseline_model": "sonnet",
                "potential_cost_usd": 0.0105,
                "saved_usd": 0.010275,
            }
        ],
    )
    _seed_savings_log(
        fake_home,
        [
            {
                "timestamp": _today_utc_iso(),
                "session_id": "s1",
                "task_type": "code",
                "estimated_saved": 0.020,
                "external_cost": 0.0,
                "model": "ollama/qwen3.5:latest",
                "host": "claude_code",
            }
        ],
    )
    out = _run_statusline(fake_home)
    # 0.010275 + 0.020 = 0.030275 → "$0.03 saved"
    assert "$0.03 saved" in out, f"expected $0.03 saved, got: {out!r}"


def test_zero_savings_omits_segment(fake_home):
    """No savings anywhere → no '$ saved' segment in the statusline output."""
    # No DB, no JSONL — just run
    out = _run_statusline(fake_home)
    assert "saved" not in out
