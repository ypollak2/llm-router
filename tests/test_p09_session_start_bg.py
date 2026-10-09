"""PLAN v16 P0.9 task 3: session-start returns without waiting on work the first
prompt does not need.

Live session-start p95 was 16,178 ms (n = 65, [HL7]) against the PRD's 2 s. The
sync path started Ollama (start-ollama.sh waits up to 10 s), ran `ollama list`,
re-detected seats (up to 2 s), queried usage.db twice, probed Ollama for resident
models, synced pxpipe and ran git for the OKF index. main() now keeps the session
tag, the stale-state reset, the proxy health line, the banner from cached usage and
the additionalContext, and spawns ONE detached child for the rest.
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

HOOK_PATH = Path(__file__).parent.parent / "src" / "llm_router" / "hooks" / "session-start.py"

#: Steps that used to run inline in main() and now belong to the child.
MOVED = ("_ensure_ollama_running", "_ensure_pxpipe_running", "_sync_pxpipe_anthropic_base_url",
         "_seats_hint", "_format_learned_memory", "_weekly_digest", "_latency_hint",
         "_preflight_check", "_ollama_contention_hint", "_ollama_watchdog_hint",
         "_maybe_refresh_benchmarks_bg", "_maybe_reindex_okf_bg", "_warm_edit_model_bg",
         "_warm_ollama_bg", "_drain_judge_queue_bg", "_ollama_watchdog_bg",
         "_maybe_update_pull_routing_rules")


def _load():
    spec = importlib.util.spec_from_file_location("session_start_p09", HOOK_PATH)
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
    mod = _load()
    monkeypatch.setattr(mod, "_refresh_claude_usage_nonblocking", lambda: "\n✅ Usage: cached")
    monkeypatch.setattr(mod, "_check_proxy_default_health", lambda: "")
    return mod


def _run_main(mod, monkeypatch, payload=None):
    payload = payload or {"session_id": "sess-p09", "cwd": "/tmp/project"}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    t = time.monotonic()
    mod.main()
    elapsed = time.monotonic() - t
    return elapsed, json.loads(out.getvalue())


def test_main_does_not_run_any_moved_step_inline(hook, monkeypatch):
    called = []

    def slow(name):
        def fn(*a, **k):
            called.append(name)
            if name == "_ensure_ollama_running":
                time.sleep(5)  # start-ollama.sh waits up to 10 s for Ollama
            return ""
        return fn

    for name in MOVED:
        monkeypatch.setattr(hook, name, slow(name))
    spawned = []
    monkeypatch.setattr(hook, "_spawn_background_session_work", lambda cwd, *_: spawned.append(cwd),
                        raising=False)  # absent before P0.9: the test then fails on the timing
    # Asserted directly via the recorders, not by a wall-clock bound (P09-FLAKE-1).
    _elapsed, out = _run_main(hook, monkeypatch)
    assert called == [], f"main() ran {called} inline"
    assert spawned == ["/tmp/project"], "exactly one background child, given the session cwd"
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"


def test_main_returns_while_a_5s_background_phase_still_runs(hook, monkeypatch, tmp_path):
    """The real spawn path: the child is detached, so main() returns first."""
    marker = tmp_path / "child_done"
    release = tmp_path / "child_release"
    # The child blocks until the test releases it (60 s cap), so "main() returned
    # first" holds however slow the runner is (P09-FLAKE-1).
    monkeypatch.setattr(hook, "_background_session_work_argv", lambda cwd, *_: [
        sys.executable, "-c",
        "import time, pathlib\n"
        f"r = pathlib.Path({str(release)!r}); t = time.monotonic()\n"
        "while not r.exists() and time.monotonic() - t < 60: time.sleep(0.05)\n"
        f"pathlib.Path({str(marker)!r}).write_text('done')"])
    _elapsed, _out = _run_main(hook, monkeypatch)
    assert not marker.exists(), "main() waited for the child"
    assert not marker.exists(), "main() waited for the detached child"
    release.write_text("go")
    deadline = time.monotonic() + 90
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert marker.read_text() == "done", "the detached child never ran"


def test_the_child_runs_every_moved_step_and_caches_its_hints(hook, monkeypatch):
    ran = []
    for name in MOVED:
        monkeypatch.setattr(hook, name, (lambda n: lambda *a, **k: ran.append(n) or f"\n{n}-line")(name))
    hook._run_background_session_work("/tmp/project")
    assert ran == list(MOVED)
    cached = hook._read_cached_hints()
    assert "_ensure_ollama_running-line" in cached and "_seats_hint-line" in cached


def test_one_failing_step_does_not_skip_the_rest(hook, monkeypatch):
    ran = []
    for name in MOVED:
        monkeypatch.setattr(hook, name, (lambda n: lambda *a, **k: ran.append(n) or "")(name))

    def boom():
        raise RuntimeError("ollama start exploded")

    monkeypatch.setattr(hook, "_ensure_ollama_running", boom)
    hook._run_background_session_work("/tmp/project")
    assert ran == [n for n in MOVED if n != "_ensure_ollama_running"]


def test_the_next_session_start_shows_the_cached_hints(hook, monkeypatch):
    monkeypatch.setattr(hook, "_spawn_background_session_work", lambda cwd, *_: None)
    hook._write_cached_hints("\n💺 Seats: claude-max")
    _elapsed, out = _run_main(hook, monkeypatch)
    assert "💺 Seats: claude-max" in out["hookSpecificOutput"]["additionalContext"]


def test_stale_or_broken_hint_cache_shows_nothing(hook, monkeypatch):
    hook._write_cached_hints("\nold line")
    assert hook._read_cached_hints(now=time.time() + hook._HINTS_MAX_AGE_S + 1) == ""
    Path(hook._hints_cache_path()).write_text("{not json")
    assert hook._read_cached_hints() == ""


def test_a_background_child_writes_no_session_start_latency_row(hook, monkeypatch):
    """BUGS P09-3: the child re-runs this file, so the latency stanza armed a
    session-start row for it; the entry point turns the recorder off first."""
    monkeypatch.delenv("LLM_ROUTER_HOOK_LATENCY", raising=False)
    for name in MOVED:
        monkeypatch.setattr(hook, name, lambda *a, **k: "")
    monkeypatch.setattr(hook, "_run_background_usage_refresh_entrypoint", lambda: None)
    hook._entry(["--background-session-work", "/tmp/project"])
    assert os.environ.get("LLM_ROUTER_HOOK_LATENCY") == "off"
    from llm_router import hook_latency

    assert hook_latency.record("session-start", "SessionStart", 9000.0) is False
    monkeypatch.delenv("LLM_ROUTER_HOOK_LATENCY")
    hook._entry(["--background-usage-refresh"])
    assert os.environ.get("LLM_ROUTER_HOOK_LATENCY") == "off"


def test_the_hook_itself_keeps_the_recorder_on(hook, monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_HOOK_LATENCY", raising=False)
    monkeypatch.setattr(hook, "_spawn_background_session_work", lambda cwd, *_: None)
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    hook._entry([])
    assert os.environ.get("LLM_ROUTER_HOOK_LATENCY") is None
