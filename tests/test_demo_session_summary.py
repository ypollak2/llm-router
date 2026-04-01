"""Demo 2 — Session-End Hook E2E.

Demonstrates and validates the session-end hook (v4) output:
  1. Builds a real SQLite usage DB with routing records
  2. Runs the hook script via subprocess (exactly as Claude Code does)
  3. Asserts the formatted summary contains the expected fields
  4. Validates savings math against the Sonnet baseline

Run standalone:
    uv run pytest tests/test_demo_session_summary.py -v -s
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

HOOK_SCRIPT = Path(__file__).parent.parent / "src/llm_router/hooks/session-end.py"

SONNET_INPUT_PER_M  = 3.0
SONNET_OUTPUT_PER_M = 15.0


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _build_usage_db(db_path: str, session_start: float, rows: list[dict]) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            task_type TEXT,
            model TEXT,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cost_usd REAL DEFAULT 0.0,
            success INTEGER DEFAULT 1,
            session_id TEXT
        )
    """)
    for r in rows:
        ts = _iso(session_start + r.get("offset_secs", 60))
        conn.execute(
            "INSERT INTO usage (timestamp, task_type, model, input_tokens, output_tokens, cost_usd, success) "
            "VALUES (?, ?, ?, ?, ?, ?, 1)",
            (ts, r["task_type"], r["model"], r["in_tok"], r["out_tok"], r["cost"]),
        )
    conn.commit()
    conn.close()


def _run_hook(state_dir: str, session_start: float) -> dict:
    """Run the hook script and return the parsed JSON output."""
    env = {**os.environ, "HOME": state_dir}
    # The hook reads SESSION_START_FILE and DB_PATH from ~/.llm-router/
    # We point HOME to our tmpdir so it reads our fixtures.
    llm_dir = os.path.join(state_dir, ".llm-router")
    os.makedirs(llm_dir, exist_ok=True)

    # Write session_start.txt
    with open(os.path.join(llm_dir, "session_start.txt"), "w") as f:
        f.write(str(session_start))

    result = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=json.dumps({"session_id": "test-session"}),
        capture_output=True,
        text=True,
        env={**os.environ,
             "HOME": state_dir,
             # Override state dir paths via env the hook uses
             },
        timeout=10,
    )
    assert result.returncode == 0, f"Hook failed:\n{result.stderr}"
    if not result.stdout.strip():
        return {}
    return json.loads(result.stdout.strip())


# ---------------------------------------------------------------------------
# Demo 2a — Typical routing session summary
# ---------------------------------------------------------------------------

class TestDemo_SessionSummary:
    """Hook formats a human-readable routing summary from real SQLite data."""

    def test_typical_session_produces_summary(self, tmp_path):
        """A session with mixed routing calls produces a valid summary."""
        session_start = time.time() - 300  # 5 minutes ago

        llm_dir = tmp_path / ".llm-router"
        llm_dir.mkdir()

        # Build fixture DB
        db_path = str(llm_dir / "usage.db")
        _build_usage_db(db_path, session_start, [
            {"task_type": "llm_query",    "model": "gemini/gemini-2.0-flash",
             "in_tok": 200, "out_tok": 80,  "cost": 0.0001, "offset_secs": 30},
            {"task_type": "llm_code",     "model": "openai/gpt-4o-mini",
             "in_tok": 500, "out_tok": 300, "cost": 0.0008, "offset_secs": 90},
            {"task_type": "llm_research", "model": "perplexity/sonar",
             "in_tok": 800, "out_tok": 400, "cost": 0.0040, "offset_secs": 150},
        ])

        # Write session_start.txt
        with open(llm_dir / "session_start.txt", "w") as f:
            f.write(str(session_start))

        result = subprocess.run(
            [sys.executable, str(HOOK_SCRIPT)],
            input=json.dumps({"session_id": "demo-session"}),
            capture_output=True, text=True,
            env={**os.environ, "HOME": str(tmp_path)},
            timeout=10,
        )
        assert result.returncode == 0, f"Hook exited non-zero:\n{result.stderr}"
        assert result.stdout.strip(), "Hook produced no output for a non-empty session"

        out = json.loads(result.stdout.strip())
        msg = out["systemMessage"]

        assert "3 calls" in msg, f"Expected '3 calls' in summary:\n{msg}"
        assert "llm_query" in msg
        assert "llm_code" in msg
        assert "llm_research" in msg
        assert "saved" in msg.lower()

        print(f"\n[Demo 2a] Session summary:\n{msg}")

    def test_savings_percentage_is_correct(self, tmp_path):
        """Savings % is correctly computed against the Sonnet baseline."""
        session_start = time.time() - 300

        llm_dir = tmp_path / ".llm-router"
        llm_dir.mkdir()

        in_tok, out_tok, actual_cost = 1_000_000, 100_000, 0.10
        baseline = (in_tok * SONNET_INPUT_PER_M + out_tok * SONNET_OUTPUT_PER_M) / 1_000_000
        expected_pct = round((baseline - actual_cost) / baseline * 100)

        _build_usage_db(str(llm_dir / "usage.db"), session_start, [
            {"task_type": "llm_query", "model": "gemini/gemini-2.0-flash",
             "in_tok": in_tok, "out_tok": out_tok, "cost": actual_cost, "offset_secs": 60},
        ])
        with open(llm_dir / "session_start.txt", "w") as f:
            f.write(str(session_start))

        result = subprocess.run(
            [sys.executable, str(HOOK_SCRIPT)],
            input=json.dumps({}),
            capture_output=True, text=True,
            env={**os.environ, "HOME": str(tmp_path)},
            timeout=10,
        )
        assert result.returncode == 0
        msg = json.loads(result.stdout)["systemMessage"]

        assert f"{expected_pct}%" in msg, (
            f"Expected {expected_pct}% savings in:\n{msg}"
        )
        print(f"\n[Demo 2b] Savings math: baseline=${baseline:.4f}, "
              f"actual=${actual_cost:.4f}, saved={expected_pct}%\n{msg}")

    def test_empty_session_produces_no_output(self, tmp_path):
        """A session with no routing calls produces empty output (exit 0, no JSON)."""
        session_start = time.time() - 300

        llm_dir = tmp_path / ".llm-router"
        llm_dir.mkdir()

        # Empty DB — no rows
        _build_usage_db(str(llm_dir / "usage.db"), session_start, [])
        with open(llm_dir / "session_start.txt", "w") as f:
            f.write(str(session_start))

        result = subprocess.run(
            [sys.executable, str(HOOK_SCRIPT)],
            input=json.dumps({}),
            capture_output=True, text=True,
            env={**os.environ, "HOME": str(tmp_path)},
            timeout=10,
        )
        assert result.returncode == 0
        assert not result.stdout.strip(), (
            f"Empty session should produce no output, got: {result.stdout!r}"
        )
        print("\n[Demo 2c] Empty session -> no output (correct)")

    def test_model_names_truncated_cleanly(self, tmp_path):
        """Long model names are truncated to keep alignment clean."""
        session_start = time.time() - 300

        llm_dir = tmp_path / ".llm-router"
        llm_dir.mkdir()

        long_model = "openai/gpt-4o-turbo-preview-2024-04-09-extra-verbose-name"
        _build_usage_db(str(llm_dir / "usage.db"), session_start, [
            {"task_type": "llm_code", "model": long_model,
             "in_tok": 300, "out_tok": 100, "cost": 0.001, "offset_secs": 60},
        ])
        with open(llm_dir / "session_start.txt", "w") as f:
            f.write(str(session_start))

        result = subprocess.run(
            [sys.executable, str(HOOK_SCRIPT)],
            input=json.dumps({}),
            capture_output=True, text=True,
            env={**os.environ, "HOME": str(tmp_path)},
            timeout=10,
        )
        assert result.returncode == 0
        msg = json.loads(result.stdout)["systemMessage"]

        # Verify truncation happened — no line exceeds reasonable terminal width
        for line in msg.splitlines():
            assert len(line) <= 120, f"Line too long ({len(line)} chars): {line!r}"
        assert "…" in msg, "Long model name should be truncated with ellipsis"
        print(f"\n[Demo 2d] Long model name truncated:\n{msg}")

    def test_cc_delta_shown_when_snapshot_present(self, tmp_path):
        """CC subscription shows start->end delta when session_start_cc_pct.json exists."""
        session_start = time.time() - 300

        llm_dir = tmp_path / ".llm-router"
        llm_dir.mkdir()

        # Write session-start CC snapshot (simulates what session-start hook saves)
        start_snap = {"session_pct": 3.0, "weekly_pct": 71.0, "sonnet_pct": 0.0,
                      "updated_at": session_start}
        with open(llm_dir / "session_start_cc_pct.json", "w") as f:
            json.dump(start_snap, f)

        # Write a "current" usage.json (session-end live fetch will fail in test env,
        # so fall back to this cached file)
        current = {"session_pct": 30.0, "weekly_pct": 73.0, "sonnet_pct": 3.0,
                   "highest_pressure": 0.73, "updated_at": time.time()}
        with open(llm_dir / "usage.json", "w") as f:
            json.dump(current, f)

        with open(llm_dir / "session_start.txt", "w") as f:
            f.write(str(session_start))

        # Empty DB (no external routing this session)
        _build_usage_db(str(llm_dir / "usage.db"), session_start, [])

        result = subprocess.run(
            [sys.executable, str(HOOK_SCRIPT)],
            input=json.dumps({}),
            capture_output=True, text=True,
            env={**os.environ, "HOME": str(tmp_path)},
            timeout=10,
        )
        assert result.returncode == 0, f"Hook exited non-zero:\n{result.stderr}"
        assert result.stdout.strip(), "Hook produced no output despite CC data being present"

        msg = json.loads(result.stdout)["systemMessage"]

        # Delta values: session 3→30 (+27), weekly 71→73 (+2), sonnet 0→3 (+3)
        assert "3.0% → 30.0%" in msg, f"Expected session delta in:\n{msg}"
        assert "+27.0pp" in msg,      f"Expected +27pp session delta in:\n{msg}"
        assert "71.0% → 73.0%" in msg, f"Expected weekly delta in:\n{msg}"
        assert "+2.0pp" in msg,       f"Expected +2pp weekly delta in:\n{msg}"

        print(f"\n[Demo 2e] CC delta display:\n{msg}")
