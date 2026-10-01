"""SessionStart must never block on the Claude usage refresh.

Owner decision 2026-10-01: `_refresh_claude_usage()` (keychain read, up to
3 attempts of an OAuth call, backoff between them) used to run inline in
`main()`, so a slow keychain or network stalled session start for up to ~79 s.
`main()` now reads the last known `usage.json`, and (only when it is stale or
missing, and outside a cooldown) spawns a detached background process.

Behavioural tests only: the hook runs in-process via importlib, the live
refresh function is monkeypatched to hang, and `subprocess.Popen` is replaced
so no real process is ever started. Mirrors tests/test_session_start_context_pointer.py.
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

# SessionStart must return within this bound even if the refresh would hang.
# The patched refresh sleeps for 30 s, so a regression to the inline call
# fails by an order of magnitude; the bound is generous for slow CI.
RETURN_BOUND_S = 10.0


def _load_hook_module():
    spec = importlib.util.spec_from_file_location("session_start_hook_usage_nb", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    saved = dict(os.environ)
    try:
        spec.loader.exec_module(mod)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return mod


class _PopenRecorder:
    """Stands in for subprocess.Popen; never starts a process. `calls` holds only
    the usage-refresh spawns (main() also launches unrelated background jobs)."""

    def __init__(self, raises: Exception | None = None):
        self.calls: list[tuple[list, dict]] = []  # usage-refresh spawns only
        self.all_calls: list[list] = []
        self.raises = raises

    def __call__(self, argv, **kwargs):
        self.all_calls.append(list(argv))
        if "--background-usage-refresh" not in argv:
            return object()  # unrelated warmers/indexers main() also spawns
        self.calls.append((list(argv), kwargs))
        if self.raises is not None:
            raise self.raises
        return object()


@pytest.fixture()
def hook(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "state"))
    (tmp_path / "state").mkdir()
    mod = _load_hook_module()

    # Neutralise every side effect of main() unrelated to the usage refresh.
    monkeypatch.setattr(mod, "_ensure_ollama_running", lambda: "")
    monkeypatch.setattr(mod, "_ensure_pxpipe_running", lambda: "")
    monkeypatch.setattr(mod, "_sync_pxpipe_anthropic_base_url", lambda: "")
    monkeypatch.setattr(mod, "_format_learned_memory", lambda: "")
    monkeypatch.setattr(mod, "_weekly_digest", lambda: "")
    monkeypatch.setattr(mod, "_latency_hint", lambda: "")
    monkeypatch.setattr(mod, "_preflight_check", lambda: "")
    monkeypatch.setattr(mod, "_maybe_refresh_benchmarks_bg", lambda: None)
    monkeypatch.setattr(mod, "_warm_ollama_bg", lambda: None)
    monkeypatch.setattr(mod, "_maybe_update_pull_routing_rules", lambda: None)
    return mod


@pytest.fixture()
def state(tmp_path) -> Path:
    return tmp_path / "state"


def _write_usage(state: Path, *, age_s: float, is_fallback: bool = False,
                 session_pct: float = 12.0, weekly_pct: float = 34.0,
                 highest_pressure: float = 0.34) -> None:
    (state / "usage.json").write_text(json.dumps({
        "session_pct": session_pct,
        "weekly_pct": weekly_pct,
        "sonnet_pct": 0.0,
        "highest_pressure": highest_pressure,
        "updated_at": time.time() - age_s,
        "is_fallback": is_fallback,
    }))


def _run_main(mod, monkeypatch) -> tuple[str, float]:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": "s-1"})))
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    t0 = time.monotonic()
    mod.main()
    return stdout.getvalue(), time.monotonic() - t0


# ── session start never waits on the refresh ────────────────────────────────

def test_main_returns_promptly_even_if_refresh_hangs(hook, monkeypatch, state):
    """The live refresh is patched to sleep 30 s; SessionStart must not call it."""
    popen = _PopenRecorder()
    monkeypatch.setattr(hook.subprocess, "Popen", popen)
    monkeypatch.setattr(hook, "_refresh_claude_usage", lambda: time.sleep(30) or "")
    monkeypatch.setattr(hook, "_refresh_claude_usage_attempt", lambda: time.sleep(30) or {})
    _write_usage(state, age_s=3600)  # stale: a refresh is wanted

    out, elapsed = _run_main(hook, monkeypatch)

    assert elapsed < RETURN_BOUND_S
    assert json.loads(out)["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert len(popen.calls) == 1  # the slow work was handed to a background process


def test_main_returns_promptly_with_no_cache_and_hanging_refresh(hook, monkeypatch):
    popen = _PopenRecorder()
    monkeypatch.setattr(hook.subprocess, "Popen", popen)
    monkeypatch.setattr(hook, "_refresh_claude_usage", lambda: time.sleep(30) or "")

    out, elapsed = _run_main(hook, monkeypatch)

    assert elapsed < RETURN_BOUND_S
    assert json.loads(out)["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert len(popen.calls) == 1


# ── spawn only when stale / missing ─────────────────────────────────────────

def test_spawns_background_refresh_when_cache_is_stale(hook, monkeypatch, state):
    popen = _PopenRecorder()
    monkeypatch.setattr(hook.subprocess, "Popen", popen)
    _write_usage(state, age_s=3600)

    hint = hook._refresh_claude_usage_nonblocking()

    assert len(popen.calls) == 1
    argv, kwargs = popen.calls[0]
    assert argv[-1] == "--background-usage-refresh"
    assert kwargs["start_new_session"] is True  # detached from the hook's session
    assert kwargs["stdout"] is hook.subprocess.DEVNULL
    assert kwargs["stderr"] is hook.subprocess.DEVNULL
    assert kwargs["stdin"] is hook.subprocess.DEVNULL
    # Last known values are still shown, labelled stale with their age.
    assert "session=12%" in hint and "weekly=34%" in hint
    assert "stale, 60m old" in hint


def test_spawns_background_refresh_when_no_cache(hook, monkeypatch):
    popen = _PopenRecorder()
    monkeypatch.setattr(hook.subprocess, "Popen", popen)

    hint = hook._refresh_claude_usage_nonblocking()

    assert len(popen.calls) == 1
    assert hint.startswith("\n⚠️")  # banner contract: no data => warning style


def test_does_not_spawn_when_cache_is_fresh(hook, monkeypatch, state):
    popen = _PopenRecorder()
    monkeypatch.setattr(hook.subprocess, "Popen", popen)
    _write_usage(state, age_s=30)

    hint = hook._refresh_claude_usage_nonblocking()

    assert popen.calls == []
    assert hint == "\n✅ Usage: session=12% weekly=34% sonnet=0%"  # no stale label


def test_fresh_threshold_is_tunable(hook, monkeypatch, state):
    popen = _PopenRecorder()
    monkeypatch.setattr(hook.subprocess, "Popen", popen)
    monkeypatch.setenv("LLM_ROUTER_SESSION_START_USAGE_FRESH_S", "10")
    _write_usage(state, age_s=30)  # fresh under the default, stale under 10 s

    hook._refresh_claude_usage_nonblocking()

    assert len(popen.calls) == 1


def test_recent_fallback_cache_still_triggers_a_retry(hook, monkeypatch, state):
    """A failed refresh writes is_fallback=True with a current timestamp; that
    placeholder must not count as fresh data."""
    popen = _PopenRecorder()
    monkeypatch.setattr(hook.subprocess, "Popen", popen)
    _write_usage(state, age_s=1, is_fallback=True, session_pct=50, weekly_pct=50,
                 highest_pressure=0.5)

    hint = hook._refresh_claude_usage_nonblocking()

    assert len(popen.calls) == 1
    assert hint.startswith("\n⚠️")
    assert "fallback" in hint


def test_corrupt_cache_is_treated_as_missing(hook, monkeypatch, state):
    popen = _PopenRecorder()
    monkeypatch.setattr(hook.subprocess, "Popen", popen)
    (state / "usage.json").write_text("{not json")

    hint = hook._refresh_claude_usage_nonblocking()

    assert len(popen.calls) == 1
    assert hint.startswith("\n⚠️")


def test_pressure_levels_keep_their_banner_markers(hook, monkeypatch, state):
    monkeypatch.setattr(hook.subprocess, "Popen", _PopenRecorder())
    _write_usage(state, age_s=10, highest_pressure=0.9)
    assert hook._refresh_claude_usage_nonblocking().startswith("\n🟡")
    _write_usage(state, age_s=10, highest_pressure=0.97)
    assert hook._refresh_claude_usage_nonblocking().startswith("\n🔴")


# ── cooldown: no pile-up ────────────────────────────────────────────────────

def test_no_second_spawn_within_cooldown(hook, monkeypatch, state):
    popen = _PopenRecorder()
    monkeypatch.setattr(hook.subprocess, "Popen", popen)
    _write_usage(state, age_s=3600)

    hook._refresh_claude_usage_nonblocking()
    hook._refresh_claude_usage_nonblocking()
    hook._refresh_claude_usage_nonblocking()

    assert len(popen.calls) == 1


def test_spawns_again_after_cooldown_expires(hook, monkeypatch, state):
    popen = _PopenRecorder()
    monkeypatch.setattr(hook.subprocess, "Popen", popen)
    _write_usage(state, age_s=3600)

    hook._refresh_claude_usage_nonblocking()
    assert len(popen.calls) == 1

    # Age the spawn marker past the default 60 s cooldown.
    marker = state / "usage_refresh_spawn.txt"
    old = time.time() - 120
    os.utime(marker, (old, old))

    hook._refresh_claude_usage_nonblocking()
    assert len(popen.calls) == 2


def test_cooldown_is_tunable(hook, monkeypatch, state):
    popen = _PopenRecorder()
    monkeypatch.setattr(hook.subprocess, "Popen", popen)
    monkeypatch.setenv("LLM_ROUTER_SESSION_START_USAGE_COOLDOWN_S", "0")
    _write_usage(state, age_s=3600)

    hook._refresh_claude_usage_nonblocking()
    hook._refresh_claude_usage_nonblocking()

    assert len(popen.calls) == 2


def test_main_twice_in_a_row_spawns_once(hook, monkeypatch, state):
    popen = _PopenRecorder()
    monkeypatch.setattr(hook.subprocess, "Popen", popen)
    _write_usage(state, age_s=3600)

    _run_main(hook, monkeypatch)
    _run_main(hook, monkeypatch)

    assert len(popen.calls) == 1


# ── spawn failure never breaks the hook ─────────────────────────────────────

@pytest.mark.parametrize("exc", [OSError("no fork"), FileNotFoundError("no python"),
                                 RuntimeError("boom")])
def test_spawn_failure_never_breaks_the_hook(hook, monkeypatch, state, exc):
    popen = _PopenRecorder(raises=exc)
    monkeypatch.setattr(hook.subprocess, "Popen", popen)
    _write_usage(state, age_s=3600)

    out, _ = _run_main(hook, monkeypatch)

    assert len(popen.calls) == 1
    assert json.loads(out)["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    # A failed spawn is not recorded as a spawn, so the next start retries.
    assert not (state / "usage_refresh_spawn.txt").exists()


def test_unwritable_state_dir_never_breaks_the_hook(hook, monkeypatch, state):
    """The cooldown marker write failing must not surface."""
    monkeypatch.setattr(hook.subprocess, "Popen", _PopenRecorder())
    _write_usage(state, age_s=3600)

    def _boom(*a, **k):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(hook.os, "replace", _boom)

    out, _ = _run_main(hook, monkeypatch)

    assert json.loads(out)["hookSpecificOutput"]["hookEventName"] == "SessionStart"


# ── the background child reuses the unchanged refresh ───────────────────────

def test_background_entrypoint_runs_the_existing_refresh(hook, monkeypatch):
    calls = []
    monkeypatch.setattr(hook, "_refresh_claude_usage", lambda: calls.append(1) or "")

    hook._run_background_usage_refresh_entrypoint()

    assert calls == [1]
