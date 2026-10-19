"""The Codex Stop hook prints the session's statusline snapshot line (P35-3).

The hook reads ``snapshots/codex-<session>.json`` (a stat and one JSON read, no ``llm_router`` import),
prints its ``line`` as the systemMessage and starts a detached ``statusline --refresh`` for the next turn.
The savings figures this file used to pin (windows, distinct today/lifetime sums, the dashboard_data
identity) are the statusline snapshot's job now; tests/test_p35_statusline.py and test_p35_fixtures.py
pin them. What stays here is the Codex JSON contract, the honest n/a, the refresher and the pair."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "src/llm_router/hooks/codex-stop.py"
SESSION = "ongoing-session"
LINE = "router: fixture/model (policy_table) | offload 40% (n=5, session)"


@pytest.fixture
def stop_env(tmp_path):
    state = tmp_path / ".llm-router"
    state.mkdir()
    # A recorder instead of the real refresher: it writes its argv to a file, so a test can see a spawn.
    rec = tmp_path / "refresher.sh"
    rec.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{tmp_path}/spawned.txt"\n')
    rec.chmod(rec.stat().st_mode | stat.S_IXUSR)
    return {
        **os.environ,
        "HOME": str(tmp_path),
        "LLM_ROUTER_HOME": str(state),
        "LLM_ROUTER_DB_PATH": str(state / "usage.db"),
        "LLM_ROUTER_STOP_HOOK": "condensed",
        "LLM_ROUTER_STATUSLINE_REFRESH_CMD": str(rec),
        "PYTHONPATH": str(ROOT / "src"),
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
    }


def seed(env, line=LINE, age_s=5.0, session=SESSION, **extra):
    snaps = Path(env["LLM_ROUTER_HOME"]) / "snapshots"
    snaps.mkdir(exist_ok=True)
    path = snaps / f"codex-{session}.json"
    path.write_text(json.dumps({"v": 1, "written_at": time.time() - age_s, "line": line, **extra}))
    return path


def run_stop(env, session=SESSION, payload=None):
    stdin = payload if payload is not None else json.dumps({"hook_event_name": "Stop", "session_id": session})
    result = subprocess.run([sys.executable, str(HOOK)], input=stdin, text=True, capture_output=True,
                            env=env, cwd=env["HOME"], timeout=20)
    assert result.returncode == 0, result.stderr
    if not result.stdout:
        return None
    payload = json.loads(result.stdout)
    assert set(payload) == {"systemMessage"}, "reporting must never continue or block the turn"
    return payload["systemMessage"]


def spawned(env, wait_s=5.0):
    """argv the recorder wrote (None when it never ran): the refresher is detached, so poll."""
    f = Path(env["HOME"]) / "spawned.txt"
    end = time.monotonic() + wait_s
    while time.monotonic() < end:
        if f.exists() and f.read_text().endswith("\n"):
            return f.read_text().split()
        time.sleep(0.05)
    return None


def test_message_is_the_snapshot_line_and_the_session_is_left_alone(stop_env):
    seed(stop_env)
    session = Path(stop_env["LLM_ROUTER_HOME"]) / "sessions" / "project" / f"{SESSION}.jsonl"
    session.parent.mkdir(parents=True)
    session.write_text('{"content":"keep this conversation"}\n')
    before = session.read_bytes()
    first = run_stop(stop_env)
    assert first == LINE
    assert run_stop(stop_env) == first
    assert session.read_bytes() == before


def test_no_snapshot_is_na_never_a_number_and_creates_no_database(stop_env):
    assert run_stop(stop_env) == "n/a"
    assert not Path(stop_env["LLM_ROUTER_DB_PATH"]).exists()


def test_snapshot_older_than_15_minutes_is_na(stop_env):
    seed(stop_env, age_s=14 * 60)
    assert run_stop(stop_env) == LINE
    seed(stop_env, age_s=16 * 60)
    assert run_stop(stop_env) == "n/a"


@pytest.mark.parametrize("bad", [
    {"line": ""}, {"line": 7}, {"line": None}, {"written_at": "now"}, {"written_at": True},
    {"written_at": time.time() + 3600}, {"v": 2},
])
def test_unusable_snapshot_fields_are_na(stop_env, bad):
    path = seed(stop_env)
    data = json.loads(path.read_text())
    data.update(bad)
    path.write_text(json.dumps(data))
    assert run_stop(stop_env) == "n/a"


def test_unreadable_snapshot_is_na_and_says_why_on_stderr(stop_env):
    path = seed(stop_env)
    path.write_text("not json")
    result = subprocess.run([sys.executable, str(HOOK)], input=json.dumps({"session_id": SESSION}), text=True,
                            capture_output=True, env=stop_env, timeout=20)
    assert json.loads(result.stdout)["systemMessage"] == "n/a"
    assert "snapshot unreadable" in result.stderr


def test_database_is_not_opened_whatever_it_holds(stop_env):
    """The old hook summed the ledger itself (and reported 'savings unavailable' on a bad database)."""
    Path(stop_env["LLM_ROUTER_DB_PATH"]).write_text("not a sqlite database")
    seed(stop_env)
    assert run_stop(stop_env) == LINE


@pytest.mark.parametrize("payload", ["", "not json", "[]", json.dumps({"session_id": 5}),
                                     json.dumps({"session_id": "../../etc/passwd"}),
                                     json.dumps({"session_id": "a b"}), json.dumps({})])
def test_no_usable_session_id_is_na_and_starts_no_refresher(stop_env, payload):
    seed(stop_env)
    assert run_stop(stop_env, payload=payload) == "n/a"
    assert spawned(stop_env, wait_s=0.5) is None


def test_hot_path_imports_nothing_from_llm_router(stop_env, tmp_path):
    """A package that records its own import: the hook must print the line without ever loading it
    (the plan's 'no llm_router import on hot path'; the 300 ms budget is for a stat and a JSON read)."""
    pkg = tmp_path / "poison" / "llm_router"
    pkg.mkdir(parents=True)
    marker = tmp_path / "imported.txt"
    (pkg / "__init__.py").write_text(f'open({str(marker)!r}, "w").write("imported")\n')
    env = {**stop_env, "PYTHONPATH": str(tmp_path / "poison")}
    seed(env)
    assert run_stop(env) == LINE
    assert spawned(env) is not None, "the refresher still starts"
    assert not marker.exists(), "codex-stop imported llm_router on its hot path"
    for banned in ("asyncio", "sqlite3"):
        assert f"import {banned}" not in HOOK.read_text(), banned


def test_it_starts_one_detached_refresher_for_this_host_and_session(stop_env):
    seed(stop_env)
    assert run_stop(stop_env) == LINE
    assert spawned(stop_env) == ["--host", "codex", "--session", SESSION]
    Path(stop_env["HOME"], "spawned.txt").unlink()
    assert run_stop(stop_env) == LINE
    assert spawned(stop_env, wait_s=0.5) is None, "rate limit: one refresher per 10 s"
    old = time.time() - 60
    os.utime(Path(stop_env["LLM_ROUTER_HOME"]) / ".codex-stop-refresh", (old, old))
    assert run_stop(stop_env) == LINE
    assert spawned(stop_env) == ["--host", "codex", "--session", SESSION]


def test_a_refresher_that_cannot_start_costs_the_turn_nothing(stop_env):
    seed(stop_env)
    stop_env["LLM_ROUTER_STATUSLINE_REFRESH_CMD"] = "/nonexistent/refresher"
    stop_env["PATH"] = "/nonexistent"
    assert run_stop(stop_env) == LINE


def test_no_llm_router_on_path_says_so_on_stderr(stop_env):
    seed(stop_env)
    env = {k: v for k, v in stop_env.items() if k != "LLM_ROUTER_STATUSLINE_REFRESH_CMD"}
    env["PATH"] = "/nonexistent"
    result = subprocess.run([sys.executable, str(HOOK)], input=json.dumps({"session_id": SESSION}), text=True,
                            capture_output=True, env=env, timeout=20)
    assert json.loads(result.stdout)["systemMessage"] == LINE
    assert "llm-router is not on PATH" in result.stderr


def test_disabled_stop_is_silent_and_starts_nothing(stop_env):
    stop_env["LLM_ROUTER_STOP_HOOK"] = "disabled"
    seed(stop_env)
    assert run_stop(stop_env) is None
    assert spawned(stop_env, wait_s=0.5) is None


def test_stop_setting_comes_from_the_environment_then_dotenv(stop_env):
    """The hook cannot call the package's dotenv loader (no llm_router import), so it reads its one knob itself."""
    seed(stop_env)
    env_file = Path(stop_env["LLM_ROUTER_HOME"]) / ".env"
    env_file.write_text("# comment\nOTHER=1\nexport LLM_ROUTER_STOP_HOOK='Disabled'\n")
    env = {k: v for k, v in stop_env.items() if k != "LLM_ROUTER_STOP_HOOK"}
    assert run_stop(env) is None, ".env disables the hook"
    assert run_stop({**env, "LLM_ROUTER_STOP_HOOK": "condensed"}) == LINE, "the real environment wins"
    env_file.write_text("LLM_ROUTER_STOP_HOOK=condensed\n")
    assert run_stop(env) == LINE
    env_file.unlink()
    os.mkfifo(env_file)                                  # a FIFO must not hang the hook (F15)
    assert run_stop(env) == LINE


def test_the_refresher_imports_pending_savings_once(stop_env):
    """The import the hook used to do on every turn now happens in the detached refresher
    (``statusline --refresh --session``): same atomic importer, so a saving is counted once."""
    state = Path(stop_env["LLM_ROUTER_HOME"])
    (state / "savings_log.jsonl").write_text(json.dumps({
        "estimated_saved": 0.125, "external_cost": 0, "model": "ollama/qwen3",
        "host": "codex", "session_id": SESSION, "mode": "realized", "is_simulated": 0,
    }) + "\n")
    code = "from llm_router import statusline_refresh as r; r._flush_pending_ledgers()"
    for _ in range(2):
        done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=stop_env, timeout=60)
        assert done.returncode == 0, done.stderr
    assert not (state / "savings_log.jsonl").exists()
    with sqlite3.connect(stop_env["LLM_ROUTER_DB_PATH"]) as conn:
        rows = conn.execute("SELECT estimated_claude_cost_saved FROM savings_stats").fetchall()
    assert rows == [(0.125,)], rows


def test_the_hook_is_fast_enough_for_its_budget(stop_env):
    """Wall-clock budget check (HOOK_BUDGETS_MS['codex-stop'] = 300 ms): median of 5 whole-process runs."""
    from llm_router.hook_latency import HOOK_BUDGETS_MS

    seed(stop_env)
    run_stop(stop_env)           # warm the file cache
    times = []
    for _ in range(5):
        t = time.perf_counter()
        run_stop(stop_env)
        times.append((time.perf_counter() - t) * 1000)
    times.sort()
    assert times[2] < HOOK_BUDGETS_MS["codex-stop"], times


test_the_hook_is_fast_enough_for_its_budget = pytest.mark.timing(test_the_hook_is_fast_enough_for_its_budget)


def test_codex_plugin_uses_non_archiving_stop_reporter():
    hooks = json.loads((ROOT / ".codex-plugin/hooks.json").read_text())["hooks"]
    assert hooks["Stop"] == [{"hooks": [{
        "type": "command", "command": "${CODEX_PLUGIN_ROOT}/hooks/codex-stop.py",
    }]}]
    assert (ROOT / "hooks/codex-stop.py").read_bytes() == HOOK.read_bytes()
