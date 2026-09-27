"""Exercise the actual Stop subprocess, ledger windows, and Codex JSON contract."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "src/llm_router/hooks/codex-stop.py"


@pytest.fixture
def stop_env(tmp_path):
    state = tmp_path / ".llm-router"
    state.mkdir()
    return {
        **os.environ,
        "HOME": str(tmp_path),
        "LLM_ROUTER_HOME": str(state),
        "LLM_ROUTER_DB_PATH": str(state / "usage.db"),
        "LLM_ROUTER_STOP_HOOK": "condensed",
        "PYTHONPATH": str(ROOT / "src"),
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
    }


def run_stop(env):
    result = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"hook_event_name": "Stop", "session_id": "ongoing-session"}),
        text=True, capture_output=True, env=env, cwd=env["HOME"], timeout=20,
    )
    assert result.returncode == 0, result.stderr
    if not result.stdout:
        return None
    payload = json.loads(result.stdout)
    assert set(payload) == {"systemMessage"}, "reporting must never continue or block the turn"
    return payload["systemMessage"]


def test_stop_reports_distinct_windows_and_preserves_session_on_repeated_turns(stop_env):
    db = Path(stop_env["LLM_ROUTER_DB_PATH"])
    with sqlite3.connect(db) as conn:
        # Hook-written (realized-gated) rows: the only VERIFIED savings (A31).
        # PR5 follow-up: mode="block" is now also required — host+timestamp
        # alone is what an external review found reading a discarded echo
        # draft as verified.
        conn.execute("CREATE TABLE savings_stats (timestamp TEXT, "
                     "estimated_claude_cost_saved REAL, host TEXT, model_used TEXT, "
                     "mode TEXT, is_simulated INTEGER, external_cost REAL DEFAULT 0)")
        conn.execute("INSERT INTO savings_stats "
                     "(timestamp, estimated_claude_cost_saved, host, model_used, mode, is_simulated) "
                     "VALUES (strftime('%Y-%m-%dT%H:%M:%S','now'), "
                     "1.25, 'claude_code', 'ollama/qwen3.5:latest', 'block', 0)")
        conn.execute("INSERT INTO savings_stats "
                     "(timestamp, estimated_claude_cost_saved, host, model_used, mode, is_simulated) "
                     "VALUES (strftime('%Y-%m-%dT%H:%M:%S','now','-2 days'), "
                     "3.5, 'claude_code', 'ollama/qwen3.5:latest', 'block', 0)")
    session = db.parent / "sessions" / "project" / "ongoing-session.jsonl"
    session.parent.mkdir(parents=True)
    session.write_text('{"content":"keep this conversation"}\n')
    before = session.read_bytes()
    first = run_stop(stop_env)
    # Verified (mode='block') figures, each labelled — see the 2026-09-27 fix:
    # both used to come from `query_window(...).saved_usd` and print as a bare
    # "lifetime {money}", indistinguishable from an unverified estimate.
    assert "today: verified $1.25" in first
    assert "lifetime: verified $4.75" in first
    assert run_stop(stop_env) == first
    assert session.read_bytes() == before


def test_pending_savings_are_imported_once(stop_env):
    state = Path(stop_env["LLM_ROUTER_HOME"])
    (state / "savings_log.jsonl").write_text(json.dumps({
        "estimated_saved": 0.125, "external_cost": 0, "model": "ollama/qwen3",
        "host": "codex", "session_id": "ongoing-session", "mode": "realized",
    }) + "\n")
    first = run_stop(stop_env)
    # A31: a Codex pending saving comes from an MCP call nobody observed being
    # used — imported once, kept out of the verified headline, labelled as an
    # estimate beside it (never as "lifetime {money}" unlabelled).
    assert "today: verified $0.00 · est +$0.12 (n=1)" in first
    assert "lifetime: verified $0.00 · est +$0.12 (n=1)" in first
    assert run_stop(stop_env) == first
    assert not (state / "savings_log.jsonl").exists()


def test_new_install_reports_zero_without_creating_a_database(stop_env):
    line = run_stop(stop_env)
    assert "today: verified $0.00" in line
    assert "lifetime: verified $0.00" in line
    # Nothing unverified either — a fresh install must not print an "est"
    # bit with no data behind it.
    assert "est +" not in line
    assert not Path(stop_env["LLM_ROUTER_DB_PATH"]).exists()


def test_figures_match_dashboard_data_summary_exactly(stop_env):
    """The narrowest pin for the 2026-09-27 fix: whatever this hook prints for
    today/lifetime must be the SAME numbers `dashboard_data.summary()` — the
    one canonical figure `llm-router status`/`savings-report`/`gain` and the
    Claude Code Stop hook all read — returns over the SAME database, not a
    second total this hook derives on its own."""
    db = Path(stop_env["LLM_ROUTER_DB_PATH"])
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE savings_stats (timestamp TEXT, "
                     "estimated_claude_cost_saved REAL, host TEXT, model_used TEXT, "
                     "mode TEXT, is_simulated INTEGER, external_cost REAL DEFAULT 0)")
        conn.execute("INSERT INTO savings_stats "
                     "(timestamp, estimated_claude_cost_saved, host, model_used, mode, is_simulated) "
                     "VALUES (strftime('%Y-%m-%dT%H:%M:%S','now'), "
                     "2.0, 'claude_code', 'ollama/qwen3.5:latest', 'block', 0)")
        conn.execute("INSERT INTO savings_stats "
                     "(timestamp, estimated_claude_cost_saved, host, model_used, mode, is_simulated) "
                     "VALUES (strftime('%Y-%m-%dT%H:%M:%S','now'), "
                     "0.5, 'claude_code', 'ollama/qwen3.5:latest', NULL, 0)")

    sys.path.insert(0, str(ROOT / "src"))
    from llm_router import dashboard_data
    today = dashboard_data.summary("today", db_path=db)
    lifetime = dashboard_data.summary("lifetime", db_path=db)

    line = run_stop(stop_env)
    assert f"today: verified ${today.realized_usd:,.2f}" in line
    assert f"est +${today.unverified_usd:,.2f} (n={today.unverified_n:,})" in line
    assert f"lifetime: verified ${lifetime.realized_usd:,.2f}" in line
    assert f"est +${lifetime.unverified_usd:,.2f} (n={lifetime.unverified_n:,})" in line

    # Never show a verified-only figure unlabelled: "lifetime $" or "today $"
    # must always be followed by "verified", never a bare dollar sign — the
    # exact regression this fix closes.
    assert "lifetime $" not in line
    assert "today $" not in line


def test_unreadable_ledger_reports_unavailable_instead_of_zero(stop_env):
    Path(stop_env["LLM_ROUTER_DB_PATH"]).write_text("not a sqlite database")
    assert "savings unavailable" in run_stop(stop_env)


def test_disabled_stop_is_silent(stop_env):
    stop_env["LLM_ROUTER_STOP_HOOK"] = "disabled"
    assert run_stop(stop_env) is None


def test_codex_plugin_uses_non_archiving_stop_reporter():
    hooks = json.loads((ROOT / ".codex-plugin/hooks.json").read_text())["hooks"]
    assert hooks["Stop"] == [{"hooks": [{
        "type": "command", "command": "${CODEX_PLUGIN_ROOT}/hooks/codex-stop.py",
    }]}]
    assert (ROOT / "hooks/codex-stop.py").read_bytes() == HOOK.read_bytes()
