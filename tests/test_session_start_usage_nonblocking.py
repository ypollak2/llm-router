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


def test_claim_unavailable_lock_primitive_never_breaks_the_hook(hook, monkeypatch, state):
    """exclusive_lock itself raising (e.g. file_lock unimportable) must also
    degrade to no-claim rather than breaking the hook."""
    popen = _PopenRecorder()
    monkeypatch.setattr(hook.subprocess, "Popen", popen)
    _write_usage(state, age_s=3600)

    import llm_router.file_lock as fl

    def _raise(*a, **k):
        raise RuntimeError("lock primitive unavailable")

    monkeypatch.setattr(fl, "exclusive_lock", _raise)

    out, _ = _run_main(hook, monkeypatch)

    assert json.loads(out)["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert popen.calls == []


# ── a cache that was never measured is not "0%" ─────────────────────────────

def _write_pending_seed(state: Path) -> None:
    """Exactly what install_hooks.seed_usage_json() writes on every install."""
    (state / "usage.json").write_text(json.dumps({
        "pending": True,
        "seeded_at": time.time(),
        "note": "placeholder written by `llm-router install`; "
                "replaced on the first usage refresh",
    }))


def test_pending_seed_is_not_measured_and_triggers_refresh(hook, monkeypatch, state):
    popen = _PopenRecorder()
    monkeypatch.setattr(hook.subprocess, "Popen", popen)
    _write_pending_seed(state)

    hint = hook._refresh_claude_usage_nonblocking()

    assert hint.startswith("\n⚠️")  # is_subscription = not startswith("\n⚠️")
    assert "0%" not in hint
    assert len(popen.calls) == 1


def test_pending_seed_through_main_never_shows_zero_percent(hook, monkeypatch, state):
    monkeypatch.setattr(hook.subprocess, "Popen", _PopenRecorder())
    _write_pending_seed(state)

    out, _ = _run_main(hook, monkeypatch)

    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "session=0%" not in ctx
    assert "✅ Usage" not in ctx


@pytest.mark.parametrize("missing", ["session_pct", "weekly_pct", "sonnet_pct",
                                     "highest_pressure"])
def test_cache_missing_pct_fields_is_not_measured(hook, monkeypatch, state, missing):
    popen = _PopenRecorder()
    monkeypatch.setattr(hook.subprocess, "Popen", popen)
    data = {"session_pct": 10.0, "weekly_pct": 20.0, "sonnet_pct": 0.0,
            "highest_pressure": 0.2, "updated_at": time.time(), "is_fallback": False}
    del data[missing]
    (state / "usage.json").write_text(json.dumps(data))

    hint = hook._refresh_claude_usage_nonblocking()

    assert hint.startswith("\n⚠️")
    assert len(popen.calls) == 1


def test_cache_with_non_numeric_pcts_is_not_measured(hook, monkeypatch, state):
    popen = _PopenRecorder()
    monkeypatch.setattr(hook.subprocess, "Popen", popen)
    (state / "usage.json").write_text(json.dumps({
        "session_pct": None, "weekly_pct": "n/a", "sonnet_pct": 0,
        "highest_pressure": 0.1, "updated_at": time.time()}))

    hint = hook._refresh_claude_usage_nonblocking()

    assert hint.startswith("\n⚠️")
    assert len(popen.calls) == 1


# ── standalone (frozen) builds launch the child through run-hook ────────────

def test_non_frozen_child_argv_is_interpreter_plus_script(hook, monkeypatch):
    import llm_router.install_hooks as ih

    monkeypatch.setattr(ih, "is_frozen", lambda: False)
    assert hook._background_usage_refresh_argv() == [
        sys.executable, hook.__file__, "--background-usage-refresh"]


def test_frozen_child_argv_goes_through_run_hook(hook, monkeypatch, state):
    """sys.executable IS the binary under PyInstaller: `<binary> script.py` is
    an unknown argument. The child must be `<binary> run-hook <script> <flag>`."""
    import llm_router.install_hooks as ih

    monkeypatch.setattr(ih, "is_frozen", lambda: True)
    monkeypatch.setattr(sys, "executable", "/opt/llm-router/llm-router")
    popen = _PopenRecorder()
    monkeypatch.setattr(hook.subprocess, "Popen", popen)
    _write_usage(state, age_s=3600)

    hook._refresh_claude_usage_nonblocking()

    assert [c[0] for c in popen.calls] == [[
        "/opt/llm-router/llm-router", "run-hook", hook.__file__,
        "--background-usage-refresh"]]


def test_run_hook_passes_the_background_flag_to_the_script(tmp_path):
    """cli.py run-hook must present the script the argv it would have seen
    launched directly, including --background-usage-refresh."""
    from llm_router.cli import main as cli_main

    out = tmp_path / "seen.json"
    script = tmp_path / "probe-hook.py"
    script.write_text(
        "import json, sys\n"
        f"json.dump({{'argv': sys.argv, 'file': __file__}}, open({str(out)!r}, 'w'))\n"
    )
    saved = sys.argv
    try:
        sys.argv = ["llm-router", "run-hook", str(script), "--background-usage-refresh"]
        cli_main()
    finally:
        sys.argv = saved

    seen = json.loads(out.read_text())
    assert seen["argv"] == [str(script), "--background-usage-refresh"]
    assert seen["file"] == str(script)


# ── session-end's delta baseline is still written on every start ────────────

def _snap(state: Path) -> dict:
    return json.loads((state / "session_start_cc_pct.json").read_text())


def test_baseline_written_from_fresh_cache_without_spawning(hook, monkeypatch, state):
    popen = _PopenRecorder()
    monkeypatch.setattr(hook.subprocess, "Popen", popen)
    _write_usage(state, age_s=30)

    hook._refresh_claude_usage_nonblocking()

    snap = _snap(state)
    assert (snap["session_pct"], snap["weekly_pct"]) == (12.0, 34.0)
    assert snap["is_fallback"] is False
    assert popen.calls == []


def test_baseline_written_from_stale_measured_cache(hook, monkeypatch, state):
    monkeypatch.setattr(hook.subprocess, "Popen", _PopenRecorder())
    _write_usage(state, age_s=3600)

    hook._refresh_claude_usage_nonblocking()

    snap = _snap(state)
    assert (snap["session_pct"], snap["weekly_pct"]) == (12.0, 34.0)
    assert snap["is_fallback"] is False


@pytest.mark.parametrize("shape", ["pending", "missing", "fallback", "corrupt"])
def test_baseline_is_the_fallback_marker_when_nothing_was_measured(
        hook, monkeypatch, state, shape):
    monkeypatch.setattr(hook.subprocess, "Popen", _PopenRecorder())
    if shape == "pending":
        _write_pending_seed(state)
    elif shape == "fallback":
        _write_usage(state, age_s=1, is_fallback=True, session_pct=50, weekly_pct=50,
                     highest_pressure=0.5)
    elif shape == "corrupt":
        (state / "usage.json").write_text("{nope")
    usage_before = (state / "usage.json").read_text() if shape != "missing" else None

    hook._refresh_claude_usage_nonblocking()

    snap = _snap(state)
    assert snap["is_fallback"] is True
    assert (snap["session_pct"], snap["weekly_pct"], snap["sonnet_pct"]) == (50, 50, 50)
    assert snap["highest_pressure"] == 0.5
    # Only the baseline is written; usage.json (e.g. the pending seed) is not.
    if usage_before is None:
        assert not (state / "usage.json").exists()
    else:
        assert (state / "usage.json").read_text() == usage_before


# ── the claim is exclusive under a real race ────────────────────────────────

def _race(hook, n=8) -> int:
    import threading

    barrier = threading.Barrier(n)
    results: list[bool] = []

    def worker():
        barrier.wait()
        results.append(hook._claim_usage_refresh_spawn(60.0))

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return sum(results)


def test_exactly_one_of_many_concurrent_claims_wins(hook, state):
    assert _race(hook) == 1


def test_exactly_one_concurrent_claim_wins_over_an_expired_marker(hook, state):
    marker = state / "usage_refresh_spawn.txt"
    marker.write_text("")
    old = time.time() - 3600
    os.utime(marker, (old, old))

    assert _race(hook) == 1


# ── the background child reuses the unchanged refresh ───────────────────────

def test_background_entrypoint_runs_the_existing_refresh(hook, monkeypatch):
    calls = []
    monkeypatch.setattr(hook, "_refresh_claude_usage", lambda: calls.append(1) or "")

    hook._run_background_usage_refresh_entrypoint()

    assert calls == [1]
