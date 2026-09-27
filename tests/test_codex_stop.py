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
        # is_simulated=0: measured 2026-09-27, savings_stats was read with NO
        # provenance filter at all — the fix that closed that gap treats a row
        # with no is_simulated value the same fail-closed way every other
        # money table already did, so these rows must say "production"
        # explicitly to keep testing the Stop hook's window logic rather than
        # its (separately-tested) provenance filtering.
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
    # 2026-09-27: ONE labelled estimate per window (realized+unverified
    # merged) — both used to come from `query_window(...).saved_usd` and
    # print as a bare "lifetime {money}", indistinguishable from an
    # unverified estimate; the PR #178 fix labelled the split, and this
    # later fix merges it into a single always-estimate figure.
    assert "today ~$1.25 est" in first
    assert "lifetime ~$4.75 est" in first
    assert run_stop(stop_env) == first
    assert session.read_bytes() == before


def test_pending_savings_are_imported_once(stop_env):
    state = Path(stop_env["LLM_ROUTER_HOME"])
    (state / "savings_log.jsonl").write_text(json.dumps({
        "estimated_saved": 0.125, "external_cost": 0, "model": "ollama/qwen3",
        "host": "codex", "session_id": "ongoing-session", "mode": "realized",
        # Explicit production stamp: an absent is_simulated imports as NULL and
        # (since the savings_stats provenance-filter fix) is dropped from every
        # money figure, same as the other four ledgers — this test is about
        # the Stop hook's pending-import display, not provenance filtering.
        "is_simulated": 0,
    }) + "\n")
    first = run_stop(stop_env)
    # A31: a Codex pending saving comes from an MCP call nobody observed being
    # used — imported once, and shown as part of the single estimate (2026-
    # 09-27: no separate "verified $0.00 · est +$Y" pair any more — realized
    # and unverified are merged into ONE always-labelled figure).
    assert "today ~$0.12 est" in first
    assert "lifetime ~$0.12 est" in first
    assert run_stop(stop_env) == first
    assert not (state / "savings_log.jsonl").exists()


def test_new_install_reports_zero_without_creating_a_database(stop_env):
    line = run_stop(stop_env)
    assert "today ~$0.00 est" in line
    assert "lifetime ~$0.00 est" in line
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
    # 2026-09-27: ONE labelled estimate (Summary.compact()), never a separate
    # "verified $X" / "est +$Y" pair — see Summary.estimated_usd's docstring.
    assert f"today {today.compact()}" in line
    assert f"lifetime {lifetime.compact()}" in line

    # Never show a bare, unlabelled figure: "lifetime $" or "today $" must
    # always carry the "~"/"est" qualifier — the exact regression this fix
    # closes — and "verified"/"unverified" must never reach this surface.
    assert "lifetime $" not in line
    assert "today $" not in line
    assert "verified" not in line.lower()


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
