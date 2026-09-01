"""Regression tests for the Claude Code statusline savings display.

SUPERSEDED 2026-08-18 — read this before restoring anything below.
-----------------------------------------------------------------
The v9.4.0 behaviour these tests pinned was itself an ad-hoc fix for
UNDER-REPORTING: the `usage` table alone missed DIRECT routings, so the script
grew a JSONL reader and a second query bolted alongside it.

That solved one missing source and left the general problem. Measured on
2026-08-18, three surfaces each queried a different subset and each presented it
as the day's total:

    usage alone             840 rows    $78.68
    savings_stats alone   1,109 rows   $102.88
    query_window (union)  2,215 rows   $205.19   <- the actual total

The statusline now delegates to ``llm_router.dashboard_data.query_window``, which
unions all five sources and is what the Stop line already used. That is
INV-COST-004 — "the aggregation functions are the ONLY cost totals; surfaces
delegate" — honoured rather than re-implemented.

The four tests that asserted the hand-rolled query were REPLACED, not deleted:
their intent (DIRECT routings must be counted) is preserved below and now holds
by construction, because the union includes savings_stats. Enforced by
tests/test_savings_surfaces_delegate.py.


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
import pathlib
import subprocess
import sys
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
    """Temp HOME with an empty .llm_router so the script writes/reads in isolation."""
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


def _run_statusline(home: Path, stdin_json: dict | None = None) -> str:
    """Run the statusline shell script with HOME pointed at the temp dir.

    The script consumes stdin (Claude Code pipes session JSON). Tests can
    pass a real-looking payload via ``stdin_json`` to exercise cwd /
    transcript_path extraction; default is ``{}`` for back-compat.

    NO_COLOR=1 is set so tests can assert on plain text without ANSI
    escape codes leaking into the assertion strings.
    """
    # The running interpreter's directory goes FIRST on PATH.
    #
    # The statusline resolves a python that can `import llm_router` by probing
    # candidates on PATH. This harness simulates a machine where llm_router IS
    # installed, so a llm_router-capable interpreter must be discoverable — otherwise
    # the test asserts against an environment no real user has.
    #
    # Without this, G-D failed: it runs `.wheelvenv/bin/python -m pytest` by
    # absolute path, so the wheel venv is never on PATH, the script found only
    # the system python3 (which cannot import llm_router), and the savings segment
    # was correctly omitted — a real behaviour, tested under conditions that made
    # it look like a bug.
    env = {
        **os.environ,
        "HOME": str(home),
        "LLM_ROUTER_ENFORCE": "soft",
        "NO_COLOR": "1",
        "PATH": os.pathsep.join(
            [str(pathlib.Path(sys.executable).parent), os.environ.get("PATH", "")]
        ),
    }
    payload = json.dumps(stdin_json) if stdin_json is not None else "{}"
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        input=payload,
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





def test_zero_savings_omits_segment(fake_home):
    """No savings anywhere → no money emoji segment in the statusline."""
    # No DB, no JSONL — just run
    out = _run_statusline(fake_home)
    assert "💰" not in out


def _seed_platform_tables(home: Path, rows: dict[str, list[dict]]) -> None:
    """Seed v9.3 per-platform tables with the given rows.

    Schema mirrors what cost.py creates: claude_usage / codex_usage /
    gemini_usage each have `timestamp`, `model`, `tokens_used`, `complexity`,
    `cost_saved_usd`, `routing_overhead_usd`.
    """
    db = home / ".llm-router" / "usage.db"
    conn = sqlite3.connect(str(db))
    for table, table_rows in rows.items():
        conn.execute(
            f"""CREATE TABLE IF NOT EXISTS {table} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT DEFAULT (datetime('now')),
                model TEXT NOT NULL,
                tokens_used INTEGER NOT NULL DEFAULT 0,
                complexity TEXT NOT NULL DEFAULT 'moderate',
                cost_saved_usd REAL NOT NULL DEFAULT 0,
                routing_overhead_usd REAL NOT NULL DEFAULT 0
            )"""
        )
        for r in table_rows:
            conn.execute(
                f"INSERT INTO {table} (timestamp, model, tokens_used, "
                f"cost_saved_usd, routing_overhead_usd) VALUES (?, ?, ?, ?, ?)",
                (
                    r["timestamp"],
                    r["model"],
                    r.get("tokens_used", 0),
                    r.get("cost_saved_usd", 0.0),
                    r.get("routing_overhead_usd", 0.0),
                ),
            )
    conn.commit()
    conn.close()


def test_reads_v93_per_platform_tables(fake_home):
    """v10.1.3+: per-platform tables (claude_usage etc.) must contribute.

    Regression for a real bug where the statusline only queried the legacy
    `usage` table and reported $0 on days with v9.3+ routing decisions.
    """
    _seed_platform_tables(
        fake_home,
        {
            "claude_usage": [
                {
                    "timestamp": _today_utc_iso(),
                    "model": "claude-haiku-4-5",
                    "tokens_used": 1500,
                    "cost_saved_usd": 0.50,
                    "routing_overhead_usd": 0.01,
                }
            ],
            "codex_usage": [
                {
                    "timestamp": _today_utc_iso(),
                    "model": "gpt-5.4",
                    "tokens_used": 800,
                    "cost_saved_usd": 0.15,
                }
            ],
            "gemini_usage": [
                {
                    "timestamp": _today_utc_iso(),
                    "model": "gemini-2.5-flash",
                    "tokens_used": 600,
                    "cost_saved_usd": 0.05,
                }
            ],
        },
    )
    out = _run_statusline(fake_home)
    # 0.50 + 0.15 + 0.05 = 0.70 → "💰 $0.70"
    assert "$0.70" in out, f"expected $0.70 in savings segment, got: {out!r}"


def test_last_route_uses_per_session_glob(fake_home):
    """v10.1.3+: last_route_<session>.json files, newest by mtime."""
    import time as _time

    # Old route (>5min ago) — must be ignored
    old = fake_home / ".llm-router" / "last_route_old.json"
    old.write_text(json.dumps({
        "task_type": "query",
        "tool": "llm_query",
        "saved_at": _time.time() - 600,
    }))

    # Recent route — must be shown
    recent = fake_home / ".llm-router" / "last_route_new.json"
    recent.write_text(json.dumps({
        "task_type": "code",
        "tool": "llm_code",
        "saved_at": _time.time() - 30,
    }))

    out = _run_statusline(fake_home)
    # v10.1.5 separator is `·` not `|`; the last route segment renders as
    # "🔀 code" because tool=="code" after stripping the "llm_" prefix.
    assert "🔀" in out, f"expected route arrow emoji, got: {out!r}"
    last_segment = out.strip().split("·")[-1]
    assert "code" in last_segment, (
        f"expected 'code' in last segment, got: {last_segment!r}"
    )


# ── v10.1.5: new segments (reset / cwd / context) ────────────────────────────


def _seed_usage_json(home: Path, **overrides) -> None:
    """Write ~/.llm-router/usage.json with sensible defaults plus overrides."""
    data = {
        "session_pct": 8.0,
        "weekly_pct": 20.0,
        "sonnet_pct": 0.0,
        "session_resets_at": None,
        "updated_at": _today_utc_iso(),
        "highest_pressure": 0.2,
    }
    data.update(overrides)
    # Drop None-valued keys so the script's `if not raw: raise` branch fires.
    data = {k: v for k, v in data.items() if v is not None}
    (home / ".llm-router" / "usage.json").write_text(json.dumps(data))


def test_reset_segment_renders_future_time(fake_home):
    """v10.1.5: session_resets_at in the future → '⏰ HH:MMpm' segment."""
    from datetime import timedelta
    future = datetime.now(timezone.utc) + timedelta(hours=2, minutes=30)
    _seed_usage_json(
        fake_home,
        session_resets_at=future.isoformat().replace("+00:00", "Z"),
    )
    out = _run_statusline(fake_home)
    assert "⏰" in out, f"expected reset clock emoji, got: {out!r}"


def test_reset_segment_skipped_if_in_past(fake_home):
    """A past session_resets_at must NOT render."""
    from datetime import timedelta
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    _seed_usage_json(
        fake_home,
        session_resets_at=past.isoformat().replace("+00:00", "Z"),
    )
    out = _run_statusline(fake_home)
    assert "⏰" not in out, f"reset segment leaked for past time: {out!r}"


def test_cwd_segment_renders_basename(fake_home):
    """v10.1.5: cwd from stdin JSON → '📂 <basename>' segment."""
    out = _run_statusline(
        fake_home,
        stdin_json={"cwd": "/Users/anyone/Projects/cool-app", "session_id": "x"},
    )
    assert "📂" in out, f"expected folder emoji, got: {out!r}"
    assert "cool-app" in out, f"expected basename, got: {out!r}"
    assert "/Users/anyone/Projects" not in out, (
        f"full path leaked into segment: {out!r}"
    )


def test_context_segment_renders_bar_and_size(fake_home, tmp_path):
    """v10.1.5: transcript with usage → '🧠 Nk ██░░░░ N%' segment."""
    transcript = tmp_path / "session.jsonl"
    # Three messages — the LAST one with usage is the one shown.
    transcript.write_text("\n".join([
        json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}),
        json.dumps({"type": "assistant", "message": {"role": "assistant",
                    "usage": {"input_tokens": 100,
                              "cache_creation_input_tokens": 5000,
                              "cache_read_input_tokens": 45000,
                              "output_tokens": 200}}}),
    ]) + "\n")
    out = _run_statusline(
        fake_home,
        stdin_json={"transcript_path": str(transcript), "session_id": "x"},
    )
    assert "🧠" in out, f"expected brain emoji, got: {out!r}"
    # 100 + 5000 + 45000 = 50100 tokens → "50.1k"
    assert "50.1k" in out, f"expected 50.1k, got: {out!r}"
    # 50100 / 200000 = 25% (default cap)
    assert "25%" in out, f"expected 25% context bar, got: {out!r}"


def test_context_segment_detects_1m_model(fake_home, tmp_path):
    """Model id containing '1m' should raise context cap to 1,000,000."""
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(json.dumps({
        "type": "assistant",
        "message": {"role": "assistant",
                    "usage": {"input_tokens": 0,
                              "cache_creation_input_tokens": 0,
                              "cache_read_input_tokens": 250000,
                              "output_tokens": 0}},
    }) + "\n")
    out = _run_statusline(
        fake_home,
        stdin_json={
            "transcript_path": str(transcript),
            "session_id": "x",
            "model": {"id": "claude-opus-4-7[1m]"},
        },
    )
    # 250k tokens / 1M cap = 25%
    assert "250.0k" in out, f"expected 250.0k tokens, got: {out!r}"
    assert "25%" in out, f"expected 25% (1M cap detected), got: {out!r}"


def test_statusline_delegates_rather_than_computing_savings():
    """The three replaced tests asserted a hand-rolled query. This asserts the
    contract that replaced it: the script must not sum a savings column itself.

    Their shared intent — DIRECT routings must be counted — now holds by
    construction, because query_window unions savings_stats where those land.
    """
    import re as _re
    from pathlib import Path as _P

    script = (_P(__file__).resolve().parents[1]
              / "src" / "llm_router" / "hooks" / "statusline-command.sh").read_text()

    assert "query_window" in script, (
        "statusline no longer delegates to dashboard_data.query_window — it is "
        "computing savings itself again, which under-reports by reading one "
        "table when the value spans five."
    )
    for column in ("saved_usd", "estimated_claude_cost_saved", "cost_saved_usd"):
        assert not _re.search(
            rf"SUM\s*\(\s*(COALESCE\s*\(\s*)?{column}\b", script, _re.I
        ), f"statusline sums {column} directly again — see INV-COST-004"


def test_the_savings_figure_is_labelled():
    """A bare `$102.31` beside a quota percentage reads as SPEND.

    That ambiguity is what prompted this whole change: the number meant savings,
    looked like cost, and disagreed with two other surfaces. The label is not
    decoration.
    """
    from pathlib import Path as _P

    script = (_P(__file__).resolve().parents[1]
              / "src" / "llm_router" / "hooks" / "statusline-command.sh").read_text()
    # Match the RENDER line, not the file's header comment — which also contains
    # 💰 and tripped the first version of this test. Third time today a textual
    # check confused a mention for a use; the fix is always to anchor on the
    # construct that does the work, here `parts+=(`.
    render = [line for line in script.splitlines()
              if "parts+=(" in line and "💰" in line]
    assert render, "no 💰 render line found in the statusline script"

    # The label moved INTO render_money(). It used to be appended here by the
    # shell, which put it after the coverage note — "~$34 saved (33% observed)
    # today" — reading as though the coverage were today's rather than the
    # saving. So assert the label on the rendered OUTPUT, which is what a user
    # sees, rather than on the shell string, which is an implementation detail.
    from llm_router.dashboard_data import WindowTotals, render_money

    line = render_money(
        WindowTotals(window="today", calls=1, tokens=1, saved_usd=34.0),
        session_usd=0.0,
    )
    assert "today" in line, f"the 💰 segment carries no period label: {line!r}"
    assert "saved" in line, f"the 💰 segment does not say what the money is: {line!r}"
    assert line.index("saved") < line.index("today"), (
        f"the verb must precede the scope, else the scope modifies the wrong "
        f"clause: {line!r}"
    )
