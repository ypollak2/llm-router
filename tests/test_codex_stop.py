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
    os.utime(Path(stop_env["LLM_ROUTER_HOME"]) / ".codex-stop-refresh.d" / f"s-{SESSION}", (old, old))
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


def test_home_comes_from_the_default_home_dotenv_when_the_environment_has_none(stop_env, tmp_path):
    """F111: ``LLM_ROUTER_HOME`` set only in ``~/.llm-router/.env`` (or ``~/.env``) is honoured, as the package does."""
    configured = tmp_path / "configured-home"
    default = tmp_path / ".llm-router"                  # HOME is tmp_path (stop_env)
    env = {k: v for k, v in stop_env.items() if k != "LLM_ROUTER_HOME"}
    (default / ".env").write_text(f"export LLM_ROUTER_HOME='{configured}'\n")
    configured.mkdir()
    seed({**env, "LLM_ROUTER_HOME": str(configured)})
    assert run_stop(env) == LINE, ".env home"
    assert (configured / ".codex-stop-refresh.d").is_dir(), "the stamp is in the configured home"
    assert run_stop({**env, "LLM_ROUTER_HOME": str(default)}) == "n/a", "the real environment wins"
    (default / ".env").unlink()
    (tmp_path / ".env").write_text(f"LLM_ROUTER_HOME={configured}\n")
    assert run_stop(env) == LINE, "~/.env is the fallback"
    (tmp_path / ".env").write_text("LLM_ROUTER_HOME=\n")
    assert run_stop(env) == "n/a", "a blank value is unset: the default home has no snapshot"


def test_sessions_do_not_starve_each_others_refresh(stop_env):
    seed(stop_env)
    seed(stop_env, session="other")
    assert run_stop(stop_env) == LINE
    assert spawned(stop_env) == ["--host", "codex", "--session", SESSION]
    Path(stop_env["HOME"], "spawned.txt").unlink()
    assert run_stop(stop_env, session="other") == LINE
    assert spawned(stop_env) == ["--host", "codex", "--session", "other"], "second session within 10 s still refreshes"
    Path(stop_env["HOME"], "spawned.txt").unlink()
    assert run_stop(stop_env, session="other") == LINE
    assert spawned(stop_env, wait_s=0.5) is None, "...but a session is still rate limited"


def test_stamps_are_cleaned_up_and_bounded(stop_env):
    seed(stop_env)
    stamps = Path(stop_env["LLM_ROUTER_HOME"]) / ".codex-stop-refresh.d"
    stamps.mkdir()
    old = time.time() - 7200
    for i in range(3):
        f = stamps / f"s-ancient{i}"
        f.write_text("")
        os.utime(f, (old, old))
    for i in range(70):
        f = stamps / f"s-recent{i:02d}"
        f.write_text("")
        t = time.time() - 100 - (70 - i)
        os.utime(f, (t, t))
    assert run_stop(stop_env) == LINE
    names = sorted(p.name for p in stamps.iterdir())
    assert len(names) == 64 and f"s-{SESSION}" in names, names
    assert not any(n.startswith("s-ancient") for n in names)
    assert "s-recent00" not in names and "s-recent69" in names, "the oldest go first"


def test_a_g1_latency_row_is_written_in_the_hook_latency_shape(stop_env):
    seed(stop_env)
    assert run_stop(stop_env) == LINE
    rows = [json.loads(x) for x in (Path(stop_env["LLM_ROUTER_HOME"]) / "hook_latency.jsonl").read_text().splitlines()]
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["hook"] == "codex-stop" and row["event"] == "Stop" and row["session_id"] == SESSION
    assert row["host"] == "codex" and row["timed_out"] is False
    assert isinstance(row["elapsed_ms"], float) and 0 <= row["elapsed_ms"] < 20_000
    assert abs(row["ts"] - time.time()) < 60 and "load1" in row
    # The package's own reader and recorder agree on the shape.
    from llm_router import hook_latency as hl
    written = {"hook", "event", "elapsed_ms", "timed_out", "ts", "session_id", "host", "load1"}
    assert set(row) == written
    assert hl.router_added_ms(row) == row["elapsed_ms"]


def test_latency_row_off_switch_and_cap(stop_env):
    seed(stop_env)
    log = Path(stop_env["LLM_ROUTER_HOME"]) / "hook_latency.jsonl"
    assert run_stop({**stop_env, "LLM_ROUTER_HOOK_LATENCY": "off"}) == LINE
    assert not log.exists()
    assert run_stop(stop_env) == LINE
    size = log.stat().st_size
    assert run_stop({**stop_env, "LLM_ROUTER_HOOK_LATENCY_MAX_BYTES": "10"}) == LINE
    assert log.stat().st_size == size, "a full log is left to the rotating writers: the hook adds nothing"
    assert run_stop({**stop_env, "LLM_ROUTER_STOP_HOOK": "disabled"}) is None
    assert len(log.read_text().splitlines()) == 2, "a disabled hook is still timed"


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
