"""Tests for the quota-savings posture section of ``llm_router doctor``.

Each check is isolated so a missing env var / missing file / database
state can be exercised independently. The goal is to verify the
*advice* is correct, not the cosmetics of the rendered output — so
assertions look for the actionable substring (env var name, env value,
filename, simple-share percentage) rather than exact rendering.
"""

from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path

import pytest

from llm_router.commands.doctor import _check_savings_posture


# Strip ANSI escape codes so assertions don't have to dodge them.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(lines: list[str]) -> list[str]:
    return [_ANSI.sub("", line).strip() for line in lines]


@pytest.fixture
def clean_env(monkeypatch):
    """Strip every environment variable the posture check looks at."""
    for var in (
        "OPENROUTER_API_KEY",
        "DEEPSEEK_API_KEY",
        "LLM_ROUTER_SIDECAR_PREFETCH",
        "LLM_ROUTER_RESPONSE_ROUTER",
        "LLM_ROUTER_ENFORCE",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def fake_home(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    llm_router_dir = tmp_path / ".llm-router"
    llm_router_dir.mkdir(parents=True, exist_ok=True)
    return tmp_path


# ── Empty environment baseline ──────────────────────────────────────────


def test_baseline_warns_on_every_missing_signal(clean_env, fake_home):
    body = "\n".join(_plain(_check_savings_posture()))
    assert "OPENROUTER_API_KEY not set" in body
    assert "DEEPSEEK_API_KEY not set" in body
    assert "LLM_ROUTER_SIDECAR_PREFETCH not set" in body
    # response_router defaults on → not a warning
    assert "LLM_ROUTER_RESPONSE_ROUTER" in body
    # F01/North Star: default enforce mode is now "smart" (enforce routing out of
    # the box — blocks Q&A until routed, allows code work).
    assert "LLM_ROUTER_ENFORCE=smart (default)" in body
    # INV-007: doctor now reports per-session shards (last_classification_*.json).
    assert "last_classification_*.json missing" in body
    assert "usage.db missing" in body


# ── OpenRouter key ──────────────────────────────────────────────────────


def test_openrouter_env_var_set_marks_ok(clean_env, monkeypatch, fake_home):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    body = "\n".join(_plain(_check_savings_posture()))
    assert "OPENROUTER_API_KEY set" in body
    assert "NOT loaded into env" not in body


def test_openrouter_stored_but_not_loaded_warns(clean_env, fake_home):
    """The isolation pattern (~/.llm-router/openrouter-routerarena.env file
    storing the key but NOT exported) should be detected — that's
    intentional for the user but worth surfacing for diagnostics."""
    env_file = fake_home / ".llm-router" / "openrouter-routerarena.env"
    env_file.write_text("export OPENROUTER_API_KEY=sk-or-test\n")
    body = "\n".join(_plain(_check_savings_posture()))
    assert "stored at" in body and "NOT loaded into env" in body


# ── Sidecar + response router ──────────────────────────────────────────


def test_sidecar_enabled_marks_ok(clean_env, monkeypatch, fake_home):
    monkeypatch.setenv("LLM_ROUTER_SIDECAR_PREFETCH", "1")
    body = "\n".join(_plain(_check_savings_posture()))
    assert "LLM_ROUTER_SIDECAR_PREFETCH=on" in body


def test_response_router_explicitly_off_warns(clean_env, monkeypatch, fake_home):
    monkeypatch.setenv("LLM_ROUTER_RESPONSE_ROUTER", "off")
    body = "\n".join(_plain(_check_savings_posture()))
    assert "LLM_ROUTER_RESPONSE_ROUTER=off" in body
    assert "explicitly disabled" in body


# ── Enforcement mode ───────────────────────────────────────────────────


@pytest.mark.parametrize("mode", ["hard", "strict"])
def test_strict_modes_pass(clean_env, monkeypatch, fake_home, mode):
    monkeypatch.setenv("LLM_ROUTER_ENFORCE", mode)
    body = "\n".join(_plain(_check_savings_posture()))
    assert f"LLM_ROUTER_ENFORCE={mode}" in body
    assert "bypasses are blocked" in body


@pytest.mark.parametrize("mode", ["off", "shadow"])
def test_off_modes_warn(clean_env, monkeypatch, fake_home, mode):
    monkeypatch.setenv("LLM_ROUTER_ENFORCE", mode)
    body = "\n".join(_plain(_check_savings_posture()))
    assert f"LLM_ROUTER_ENFORCE={mode}" in body
    assert "advisory only" in body


# ── Hint freshness ──────────────────────────────────────────────────────


def test_fresh_hint_marks_ok(clean_env, fake_home):
    # INV-007: doctor now globs last_classification_<session_id>.json.
    # A shard for any session counts as "the bridge is firing somewhere".
    hint = fake_home / ".llm-router" / "last_classification_test.json"
    hint.write_text("{}")
    body = "\n".join(_plain(_check_savings_posture()))
    assert "fresh" in body and "hook hint bridge active" in body


def test_old_hint_warns(clean_env, fake_home):
    """A hint file >1h old means the hook isn't firing — flag it."""
    # INV-007: doctor now globs per-session shards.
    hint = fake_home / ".llm-router" / "last_classification_test.json"
    hint.write_text("{}")
    # Backdate the mtime by 2h.
    two_hours_ago = time.time() - 7200
    import os
    os.utime(hint, (two_hours_ago, two_hours_ago))
    body = "\n".join(_plain(_check_savings_posture()))
    assert "m old" in body or "hook may" in body


# ── Today's simple-share ───────────────────────────────────────────────


def _seed_routing(home: Path, *rows: tuple[str, str]) -> None:
    """Create a usage.db with the given (complexity, model) routing rows
    timestamped in UTC (as production does — routing_decisions.timestamp DEFAULT is
    datetime('now') = UTC) so the handler's date(timestamp,'localtime') maps them
    to today-local. Inserting 'localtime' here double-applied the offset and lost
    the rows near midnight in non-UTC timezones."""
    db = home / ".llm-router" / "usage.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS routing_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT, prompt_hash TEXT, task_type TEXT,
                profile TEXT, complexity TEXT, final_model TEXT,
                final_provider TEXT, input_tokens INTEGER,
                output_tokens INTEGER, cost_usd REAL,
                reason_code TEXT
            )
        """)
        conn.executemany(
            "INSERT INTO routing_decisions (timestamp, complexity, final_model) "
            "VALUES (datetime('now'), ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def test_simple_share_above_30_pct_marks_ok(clean_env, fake_home):
    """7 out of 10 simple = 70% — healthy."""
    rows = [("simple", "flash")] * 7 + [("moderate", "sonnet")] * 3
    _seed_routing(fake_home, *rows)
    body = "\n".join(_plain(_check_savings_posture()))
    assert "70.0%" in body
    assert "boundary fix is firing" in body


def test_simple_share_below_30_warns(clean_env, fake_home):
    """2 out of 10 simple = 20% — below target."""
    rows = [("simple", "flash")] * 2 + [("moderate", "sonnet")] * 8
    _seed_routing(fake_home, *rows)
    body = "\n".join(_plain(_check_savings_posture()))
    assert "20.0%" in body
    assert "below 30%" in body


def test_simple_share_zero_warns_about_router(clean_env, fake_home):
    """0/N is the exact symptom the boundary fix addresses — call it out."""
    rows = [("moderate", "sonnet")] * 5
    _seed_routing(fake_home, *rows)
    body = "\n".join(_plain(_check_savings_posture()))
    assert "boundary fix isn't reaching" in body


def test_zero_routings_today_says_no_data(clean_env, fake_home):
    """Empty routing_decisions today — nothing to measure."""
    db = fake_home / ".llm-router" / "usage.db"
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE routing_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT,
            complexity TEXT, final_model TEXT, reason_code TEXT
        )
    """)
    conn.commit()
    conn.close()
    body = "\n".join(_plain(_check_savings_posture()))
    assert "No routing decisions today" in body


def test_simple_share_ignores_sidecar_backfill(clean_env, fake_home):
    """Backfilled sidecar rows must NOT count toward today's simple-share —
    they're historical, not from live routing."""
    db = fake_home / ".llm-router" / "usage.db"
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE routing_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT,
            complexity TEXT, final_model TEXT, reason_code TEXT
        )
    """)
    # 5 backfilled "simple" + 1 real "moderate" → posture should
    # report 0/1 = 0% simple, NOT 5/6 = 83%.
    conn.executemany(
        "INSERT INTO routing_decisions (timestamp, complexity, final_model, "
        "reason_code) VALUES (datetime('now'), ?, ?, ?)",
        [("simple", "flash", "sidecar_backfill")] * 5
        + [("moderate", "sonnet", None)],
    )
    conn.commit()
    conn.close()
    body = "\n".join(_plain(_check_savings_posture()))
    # 0/1 = 0% expected — the backfill rows must be excluded.
    assert "0/1" in body or "boundary fix isn't" in body
