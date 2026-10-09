"""PLAN v16 P0.9 task 4: the status-bar hook only reads a cached line.

Live status-bar p95 was 4,488 ms (n = 344, [HL7]) against the PRD's 300 ms. The
hook opened usage.db twice per prompt with ``sqlite3.connect(..., timeout=2)``
(waiting out a writer's lock costs up to 2 s each) and read the Gemini quota, all
before the prompt could go on. The line is now computed by a detached refresher
into ``status_bar_cache.json`` (TTL 30 s); the hook reads that file.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import time
from pathlib import Path

import pytest

HOOK_PATH = Path(__file__).parent.parent / "src" / "llm_router" / "hooks" / "status-bar.py"


def _load():
    spec = importlib.util.spec_from_file_location("status_bar_p09", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    saved = dict(os.environ)
    try:
        spec.loader.exec_module(mod)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return mod


@pytest.fixture()
def hook(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "state"))
    (tmp_path / "state").mkdir()
    return _load()


def _main(mod, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": "s1", "prompt": "hi"})))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    t = time.perf_counter()
    try:
        mod.main()
    except SystemExit as exc:
        assert exc.code in (0, None)
    return (time.perf_counter() - t) * 1000.0, out.getvalue()


def _put_cache(mod, line, age_s):
    path = Path(mod._cache_path())
    path.write_text(json.dumps({"ts": time.time() - age_s, "key": mod._cache_key(), "status": line}))


def _no_sqlite(monkeypatch, mod):
    def boom(*a, **k):
        raise AssertionError("the hook opened sqlite on the prompt's path")
    monkeypatch.setattr(mod.sqlite3, "connect", boom)


def _forbid_sync_work(monkeypatch, mod):
    """Make every synchronous-refresh route raise if the hot path touches it.

    The regression this file guards (HL7: p95 4,488 ms) was usage fetch + sqlite
    lock wait + subprocess wait on the prompt's path. Those calls, not the clock,
    are the risk, so the test detects them by call. A wall-clock bound flaked on
    CI Python 3.13 (69.3 ms vs 50 ms, PR #343) with nothing wrong in the hook.
    """
    import subprocess

    def forbid(name):
        def boom(*a, **k):
            raise AssertionError(f"{name} ran synchronously on the status-bar hot path")
        return boom

    for fn in ("_format_status", "_refresh_cache", "_read_savings", "_read_session_calls",
               "_read_claude_credits", "_read_provider_health"):
        monkeypatch.setattr(mod, fn, forbid(fn))
    for fn in ("run", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, fn, forbid(f"subprocess.{fn}"))
    monkeypatch.setattr(subprocess.Popen, "wait", forbid("Popen.wait"))
    monkeypatch.setattr(subprocess.Popen, "communicate", forbid("Popen.communicate"))
    monkeypatch.setattr(os, "waitpid", forbid("os.waitpid"))
    monkeypatch.setattr(time, "sleep", forbid("time.sleep"))


def test_a_stale_cache_returns_the_line_and_spawns_one_detached_refresher(hook, monkeypatch):
    """No wall-clock bound: a synchronous refresh on the hot path fails by call."""
    import llm_router.statusline_tick as tick

    _put_cache(hook, "📊 CC 40%s·70%w", age_s=60)
    detached = []
    monkeypatch.setattr(tick, "_spawn_detached", lambda argv: detached.append(list(argv)))
    _no_sqlite(monkeypatch, hook)
    _forbid_sync_work(monkeypatch, hook)
    _ms, out = _main(hook, monkeypatch)
    assert json.loads(out)["hookSpecificOutput"]["systemMessage"] == "📊 CC 40%s·70%w"
    assert len(detached) == 1 and detached[0][-1] == "--refresh-cache", detached
    # A second prompt within the claim window starts no second refresher.
    _main(hook, monkeypatch)
    assert len(detached) == 1


def test_a_fresh_cache_spawns_nothing(hook, monkeypatch):
    _put_cache(hook, "line", age_s=1)
    spawned = []
    monkeypatch.setattr(hook, "_spawn_refresh", lambda: spawned.append(1))
    _no_sqlite(monkeypatch, hook)
    _ms, out = _main(hook, monkeypatch)
    assert json.loads(out)["hookSpecificOutput"]["systemMessage"] == "line"
    assert spawned == []


def test_no_cache_prints_nothing_and_spawns_a_refresher(hook, monkeypatch):
    spawned = []
    monkeypatch.setattr(hook, "_spawn_refresh", lambda: spawned.append(1))
    _no_sqlite(monkeypatch, hook)
    _ms, out = _main(hook, monkeypatch)
    assert out == "" and spawned == [1]


def test_a_cache_too_old_to_show_is_not_shown(hook, monkeypatch):
    _put_cache(hook, "yesterday's line", age_s=hook._CACHE_SERVE_MAX_AGE_S + 5)
    monkeypatch.setattr(hook, "_spawn_refresh", lambda: None)
    _ms, out = _main(hook, monkeypatch)
    assert out == ""


def test_a_cache_written_under_other_settings_is_not_shown(hook, monkeypatch):
    Path(hook._cache_path()).write_text(json.dumps({"ts": time.time(), "key": "full|off", "status": "x"}))
    monkeypatch.setattr(hook, "_spawn_refresh", lambda: None)
    monkeypatch.setattr(hook, "STATUS_MODE", "compact")
    _ms, out = _main(hook, monkeypatch)
    assert out == ""


def test_the_refresher_survives_sqlite_raising_and_the_hook_still_shows_a_line(hook, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(hook.sqlite3, "connect", boom)
    hook._refresh_cache()
    line, age = hook._read_cache()
    assert isinstance(line, str) and line.startswith("📊") and age is not None and age < 5
    monkeypatch.setattr(hook, "_spawn_refresh", lambda: None)
    _ms, out = _main(hook, monkeypatch)
    assert json.loads(out)["hookSpecificOutput"]["systemMessage"] == line


def test_the_refresher_writes_no_status_bar_latency_row(hook, monkeypatch):
    """BUGS P09-3: the refresher re-runs this file; the entry point turns the
    recorder off so its sqlite time is not counted as the hook's."""
    monkeypatch.delenv("LLM_ROUTER_HOOK_LATENCY", raising=False)
    monkeypatch.setattr(hook, "_refresh_cache", lambda: None)
    hook._entry(["--refresh-cache"])
    assert os.environ.get("LLM_ROUTER_HOOK_LATENCY") == "off"
    monkeypatch.delenv("LLM_ROUTER_HOOK_LATENCY")
    monkeypatch.setattr(hook, "_spawn_refresh", lambda: None)
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    with pytest.raises(SystemExit):
        hook._entry([])
    assert os.environ.get("LLM_ROUTER_HOOK_LATENCY") is None


def test_the_real_refresher_process_fills_the_cache(hook, tmp_path):
    """End to end: the argv the hook spawns computes and writes the line."""
    import subprocess

    env = dict(os.environ, HOME=str(tmp_path), LLM_ROUTER_HOME=str(tmp_path / "state"),
               LLM_ROUTER_HOOK_LATENCY="off")
    r = subprocess.run(hook._refresh_argv(), env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    line, _age = hook._read_cache()
    assert isinstance(line, str) and line.startswith("📊")


@pytest.mark.timing
def test_the_prompt_path_never_waits_on_a_locked_usage_db(hook, monkeypatch):
    """The live tail, reproduced: each ``sqlite3.connect(timeout=2)`` waits out a
    writer's lock. Two of them per prompt made ~4 s (live p95 4,488 ms)."""
    import sqlite3

    def locked(*a, **k):
        time.sleep(2.0)
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(hook.sqlite3, "connect", locked)
    monkeypatch.setattr(hook, "_spawn_refresh", lambda: None, raising=False)
    ms, _out = _main(hook, monkeypatch)
    assert ms < 300.0, f"status-bar took {ms:.0f} ms behind a locked usage.db (PRD bar 300 ms)"
