"""Regression tests: `quota_snapshots.claude_*_pct` must not be stuck at 0.0.

INCIDENT — 2026-09-27 read-only audit of ~/.llm-router/usage.db found `claude_weekly_pct`
and `claude_session_pct` equal to 0.0 on every one of 3,402 rows since 2026-08-31, while
the live statusline and `llm-router status` showed real, non-zero values at the same
moment. The gauge this table exists to feed was reporting nothing useful.

ROOT CAUSE — `hooks/auto-route.py::_log_quota_snapshot_sync` is the actual writer (the
async `cost.log_quota_snapshot` is dead code — see `test_l03_dead_public_api_ratchet.py`).
It read the pressure dict with the *usage.json* dialect's key names:

    pressure.get("session_pct", 0.0)
    pressure.get("weekly_pct", 0.0)
    pressure.get("sonnet_pct", 0.0)

But the actual argument it receives is `_get_pressure()`'s return value, which uses a
DIFFERENT dialect: keys "session", "weekly", "sonnet" (fractions 0.0-1.0, no "_pct"
suffix — see `_get_pressure()` and `_frac()` in the same file). The lookup always missed
and the `, 0.0` fallback fabricated a "0% pressure" reading on every single row,
regardless of the real value the statusline was showing at that instant.

THE FIX — read the correct keys, with NO default (so a genuinely missing key produces
`None`, stored as SQL NULL — never a fabricated 0.0), and relax the `NOT NULL` constraint
that made storing NULL impossible in the first place (`_ensure_quota_snapshots_pct_nullable`).
Existing rows are NOT rewritten; only the constraint changes for future writes.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def auto_route():
    """Load the auto-route hook as a module (stdlib-only, no package import)."""
    src = (
        Path(__file__).resolve().parent.parent
        / "src"
        / "llm_router"
        / "hooks"
        / "auto-route.py"
    )
    spec = importlib.util.spec_from_file_location("auto_route_quota_snapshot", src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _row(db_path: str, prompt_sequence: int):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT claude_session_pct, claude_weekly_pct, claude_sonnet_pct "
            "FROM quota_snapshots WHERE prompt_sequence = ?",
            (prompt_sequence,),
        ).fetchone()
    finally:
        conn.close()


def test_realistic_payload_stores_the_nonzero_pct(auto_route, tmp_path):
    """A real `_get_pressure()`-shaped payload must store its real, non-zero values.

    `_get_pressure()` returns fractions under keys "session"/"weekly"/"sonnet" — this is
    exactly what real routing calls pass in. Before the fix, the writer looked up
    "session_pct"/"weekly_pct"/"sonnet_pct" instead, always missed, and stored 0.0 for
    all three regardless of this payload.
    """
    db_path = str(tmp_path / "usage.db")
    pressure = {"session": 0.24, "weekly": 0.42, "sonnet": 0.10}

    auto_route._log_quota_snapshot_sync(
        session_id="s1",
        prompt_sequence=1,
        prompt_hash=None,
        pressure=pressure,
        routing_decision_id=None,
        final_model="ollama/qwen3.5",
        final_provider="ollama",
        complexity_requested="simple",
        complexity_used="simple",
        was_downgraded=False,
        db_path=db_path,
    )

    session_pct, weekly_pct, sonnet_pct = _row(db_path, 1)
    assert (session_pct, weekly_pct, sonnet_pct) == (0.24, 0.42, 0.10), (
        "stored pct must equal the real pressure reading, not a fail-open 0.0"
    )
    assert weekly_pct != 0.0


def test_unavailable_reading_stores_null_not_zero(auto_route, tmp_path):
    """When the pressure reading truly isn't available, store NULL — never 0.0.

    An empty pressure dict models `_get_pressure()` having nothing to report (e.g. no
    usage.json and the SQLite fallback also came up empty). The old `.get(key, 0.0)`
    fallback could not tell "unavailable" apart from "genuinely 0% used" and always
    wrote 0.0. The fix drops the default, so a missing key becomes `None` in Python and
    NULL in SQLite.
    """
    db_path = str(tmp_path / "usage.db")

    auto_route._log_quota_snapshot_sync(
        session_id="s1",
        prompt_sequence=1,
        prompt_hash=None,
        pressure={},
        routing_decision_id=None,
        final_model="ollama/qwen3.5",
        final_provider="ollama",
        complexity_requested="simple",
        complexity_used="simple",
        was_downgraded=False,
        db_path=db_path,
    )

    session_pct, weekly_pct, sonnet_pct = _row(db_path, 1)
    assert (session_pct, weekly_pct, sonnet_pct) == (None, None, None), (
        "an unavailable reading must store SQL NULL, not a fabricated 0.0"
    )


def test_legacy_not_null_database_is_upgraded_without_rewriting_history(auto_route, tmp_path):
    """A pre-existing NOT NULL `quota_snapshots` table (every production DB before this
    fix) must be usable for new NULL-carrying writes, and its historical rows — including
    their zeros — must NOT be rewritten. This is a constraint fix, not a backfill.
    """
    db_path = str(tmp_path / "usage.db")
    legacy_schema = """
        CREATE TABLE quota_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT DEFAULT (datetime('now')),
            session_id TEXT NOT NULL,
            prompt_sequence INTEGER NOT NULL,
            prompt_hash TEXT,
            claude_session_pct REAL NOT NULL,
            claude_weekly_pct REAL NOT NULL,
            claude_sonnet_pct REAL NOT NULL,
            openai_spent_usd REAL NOT NULL DEFAULT 0,
            gemini_spent_usd REAL NOT NULL DEFAULT 0,
            ollama_available INTEGER NOT NULL DEFAULT 1,
            cache_age_seconds REAL NOT NULL,
            was_cache_fresh INTEGER NOT NULL,
            routing_decision_id INTEGER,
            final_model TEXT,
            final_provider TEXT,
            complexity_requested TEXT,
            complexity_used TEXT,
            was_downgraded INTEGER DEFAULT 0
        )
    """
    conn = sqlite3.connect(db_path)
    conn.execute(legacy_schema)
    conn.execute(
        "INSERT INTO quota_snapshots (session_id, prompt_sequence, claude_session_pct, "
        "claude_weekly_pct, claude_sonnet_pct, cache_age_seconds, was_cache_fresh) "
        "VALUES ('legacy-session', 1, 0.0, 0.0, 0.0, 5.0, 1)"
    )
    conn.commit()
    conn.close()

    auto_route._log_quota_snapshot_sync(
        session_id="s2",
        prompt_sequence=1,
        prompt_hash=None,
        pressure={"session": 0.05, "weekly": 0.33, "sonnet": 0.02},
        routing_decision_id=None,
        final_model="ollama/qwen3.5",
        final_provider="ollama",
        complexity_requested="simple",
        complexity_used="simple",
        was_downgraded=False,
        db_path=db_path,
    )

    conn = sqlite3.connect(db_path)
    try:
        notnull = {
            r[1]: r[3]
            for r in conn.execute("PRAGMA table_info(quota_snapshots)")
            if r[1] in ("claude_session_pct", "claude_weekly_pct", "claude_sonnet_pct")
        }
        assert not any(notnull.values()), "pct columns must be nullable after upgrade"

        legacy_row = conn.execute(
            "SELECT claude_session_pct, claude_weekly_pct, claude_sonnet_pct "
            "FROM quota_snapshots WHERE session_id = 'legacy-session'"
        ).fetchone()
        assert legacy_row == (0.0, 0.0, 0.0), (
            "historical row values must not be rewritten by the constraint fix"
        )

        new_row = conn.execute(
            "SELECT claude_session_pct, claude_weekly_pct, claude_sonnet_pct "
            "FROM quota_snapshots WHERE session_id = 's2'"
        ).fetchone()
        assert new_row == (0.05, 0.33, 0.02)
    finally:
        conn.close()
