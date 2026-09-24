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
                     "mode TEXT)")
        conn.execute("INSERT INTO savings_stats VALUES (strftime('%Y-%m-%dT%H:%M:%S','now'), "
                     "1.25, 'claude_code', 'ollama/qwen3.5:latest', 'block')")
        conn.execute("INSERT INTO savings_stats VALUES (strftime('%Y-%m-%dT%H:%M:%S','now','-2 days'), "
                     "3.5, 'claude_code', 'ollama/qwen3.5:latest', 'block')")
    session = db.parent / "sessions" / "project" / "ongoing-session.jsonl"
    session.parent.mkdir(parents=True)
    session.write_text('{"content":"keep this conversation"}\n')
    before = session.read_bytes()
    first = run_stop(stop_env)
    assert "saved today ~$1.25" in first
    assert "lifetime ~$4.75" in first
    assert "estimated, all hosts" in first
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
    # used — imported once, kept out of the headline, labelled beside it.
    assert "saved today ~$0.0000" in first
    assert "lifetime ~$0.0000" in first
    assert "+ $0.12 unverified, n=1" in first
    assert run_stop(stop_env) == first
    assert not (state / "savings_log.jsonl").exists()


def test_new_install_reports_zero_without_creating_a_database(stop_env):
    assert "saved today ~$0.0000 · lifetime ~$0.0000" in run_stop(stop_env)
    assert not Path(stop_env["LLM_ROUTER_DB_PATH"]).exists()


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
