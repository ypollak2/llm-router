"""auto-route must never block a prompt on a quota refresh (KPI G1).

Evidence: hook_latency.jsonl (n=64/21h) showed auto-route p50 124 ms but p95
2910 ms; 13 of the 15 runs over 1 s came after a >300 s gap, i.e. when
usage.json had aged past LLM_ROUTER_QUOTA_TTL and `_get_pressure()` ran the
keychain read + OAuth HTTPS call inline.

These tests run the real hook script as a subprocess beside a FAKE sibling
`usage-refresh.py` that sleeps, so no keychain or network is ever touched.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOK_SRC = ROOT / "src" / "llm_router" / "hooks" / "auto-route.py"

STUB = """\
import os, sys, time
home = os.environ["LLM_ROUTER_HOME"]
with open(os.path.join(home, "stub_started.log"), "a") as f:
    f.write("started\\n")
print("STUB-STDOUT-MUST-NOT-REACH-HOOK")
print("STUB-STDERR-MUST-NOT-REACH-HOOK", file=sys.stderr)
time.sleep(float(os.environ.get("STUB_SLEEP", "5")))
open(os.path.join(home, "stub_done"), "w").write("done")
"""


def _write_usage(home: Path, age_s: float, session_pct: float = 42.0) -> None:
    (home / "usage.json").write_text(json.dumps({
        "session_pct": session_pct, "weekly_pct": 5.0, "sonnet_pct": 3.0,
        "updated_at": time.time() - age_s,
    }))


@pytest.fixture
def sandbox(tmp_path):
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    shutil.copy(HOOK_SRC, hooks / "auto-route.py")
    (hooks / "usage-refresh.py").write_text(STUB)
    home = tmp_path / "lr"
    home.mkdir()
    return hooks, home, tmp_path


def _env(home: Path, tmp: Path, **extra) -> dict:
    env = dict(os.environ)
    # A fake `security` that hangs: if the hook ever runs the keychain read
    # inline again it stalls here (and never touches the real keychain).
    shim = tmp / "bin"
    shim.mkdir(exist_ok=True)
    (shim / "security").write_text("#!/bin/sh\nsleep 10\nexit 1\n")
    (shim / "security").chmod(0o755)
    env["PATH"] = f"{shim}{os.pathsep}{env.get('PATH', '')}"
    env.update(
        HOME=str(tmp / "home"), LLM_ROUTER_HOME=str(home),
        PYTHONPATH=str(ROOT / "src"), OLLAMA_HOST="127.0.0.1:1",
        LLM_ROUTER_DISABLE_LLM_CLASSIFIERS="1",
        # _get_pressure() only runs in subscription mode (_CC_MODE).
        LLM_ROUTER_CLAUDE_SUBSCRIPTION="1",
    )
    env.pop("LLM_ROUTER_QUOTA_TTL", None)
    # The hook switches to a test-mode path when it sees pytest in its env.
    for k in [k for k in env if k.startswith("PYTEST")]:
        env.pop(k)
    env.update(extra)
    return env


def _run_hook(hooks: Path, home: Path, tmp: Path, **extra):
    payload = {
        "session_id": "t", "prompt": "what is the capital of france",
        "hook_event_name": "UserPromptSubmit", "cwd": str(tmp),
        "transcript_path": str(tmp / "t.jsonl"),
    }
    (tmp / "home").mkdir(exist_ok=True)
    t0 = time.perf_counter()
    r = subprocess.run(
        [sys.executable, str(hooks / "auto-route.py")],
        input=json.dumps(payload), text=True, capture_output=True,
        env=_env(home, tmp, **extra), timeout=60,
    )
    return time.perf_counter() - t0, r


def _wait_for(path: Path, timeout: float = 15.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if path.exists():
            return True
        time.sleep(0.1)
    return False


def _load_hook(hooks: Path, monkeypatch, home: Path):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home))
    monkeypatch.delenv("LLM_ROUTER_QUOTA_TTL", raising=False)
    spec = importlib.util.spec_from_file_location("ar_nonblocking", hooks / "auto-route.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_stale_cache_does_not_block_while_refresh_runs_in_background(sandbox):
    hooks, home, tmp = sandbox
    _write_usage(home, age_s=900)
    elapsed, r = _run_hook(hooks, home, tmp)
    assert r.returncode == 0
    # The stub sleeps 5 s; an inline refresh would cost at least that.
    assert elapsed < 3.0, f"hook took {elapsed:.2f}s with a stale cache"
    # ...and the refresh still happens, out of process.
    assert _wait_for(home / "stub_started.log"), "no background refresh started"
    assert _wait_for(home / "stub_done", 15), "background refresh never finished"


def test_stale_cache_is_no_slower_than_fresh_cache(sandbox):
    hooks, home, tmp = sandbox
    _write_usage(home, age_s=0)
    fresh, _ = _run_hook(hooks, home, tmp)
    assert not (home / "stub_started.log").exists(), "fresh cache must not refresh"
    _write_usage(home, age_s=900)
    stale, _ = _run_hook(hooks, home, tmp)
    assert stale < fresh + 2.0, f"stale {stale:.2f}s vs fresh {fresh:.2f}s"


def test_child_output_never_reaches_hook_stdout_or_stderr(sandbox):
    hooks, home, tmp = sandbox
    _write_usage(home, age_s=900)
    _, r = _run_hook(hooks, home, tmp)
    assert "STUB-STD" not in r.stdout and "STUB-STD" not in r.stderr
    assert _wait_for(home / "stub_started.log")


def test_stdout_and_exit_code_match_fresh_cache_run(sandbox):
    hooks, home, tmp = sandbox
    _write_usage(home, age_s=0)
    _, fresh = _run_hook(hooks, home, tmp)
    _write_usage(home, age_s=900)
    _, stale = _run_hook(hooks, home, tmp)
    assert stale.returncode == fresh.returncode == 0

    def decision(out: str):
        for line in out.splitlines():
            if line.startswith("{"):
                return json.loads(line)
        return out

    assert decision(stale.stdout) == decision(fresh.stdout)


def test_two_near_simultaneous_invocations_start_one_refresh(sandbox):
    hooks, home, tmp = sandbox
    _write_usage(home, age_s=900)
    (tmp / "home").mkdir(exist_ok=True)
    payload = json.dumps({
        "session_id": "t", "prompt": "what is the capital of france",
        "hook_event_name": "UserPromptSubmit", "cwd": str(tmp),
        "transcript_path": str(tmp / "t.jsonl"),
    })
    procs = [
        subprocess.Popen([sys.executable, str(hooks / "auto-route.py")],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, env=_env(home, tmp))
        for _ in range(2)
    ]
    for p in procs:
        p.communicate(payload, timeout=60)
    assert _wait_for(home / "stub_started.log")
    time.sleep(1.0)
    started = (home / "stub_started.log").read_text().splitlines()
    assert started == ["started"], f"expected exactly one refresh, got {started}"


def test_back_to_back_runs_do_not_respawn_within_cooldown(sandbox):
    hooks, home, tmp = sandbox
    _write_usage(home, age_s=900)
    _run_hook(hooks, home, tmp)
    assert _wait_for(home / "stub_started.log")
    _run_hook(hooks, home, tmp)  # still stale: stub has not rewritten usage.json
    time.sleep(1.0)
    assert (home / "stub_started.log").read_text().splitlines() == ["started"]


def test_stale_marker_does_not_wedge_future_refreshes(sandbox):
    hooks, home, tmp = sandbox
    _write_usage(home, age_s=900)
    marker = home / "usage_refresh_spawn.txt"
    marker.write_text("0")
    old = time.time() - 3600  # a hung child's marker from an hour ago
    os.utime(marker, (old, old))
    _run_hook(hooks, home, tmp)
    assert _wait_for(home / "stub_started.log"), "an hour-old marker blocked the refresh"


def test_stale_value_is_what_routing_sees(sandbox, monkeypatch):
    hooks, home, tmp = sandbox
    _write_usage(home, age_s=900, session_pct=42.0)
    mod = _load_hook(hooks, monkeypatch, home)
    monkeypatch.setattr(mod, "_spawn_background_usage_refresh", lambda: None)
    called = []
    monkeypatch.setattr(mod, "_fetch_usage_inline", lambda: called.append(1) or None)
    p = mod._get_pressure()
    assert p == {"session": pytest.approx(0.42), "sonnet": pytest.approx(0.03),
                 "weekly": pytest.approx(0.05)}
    assert not called, "_get_pressure must not run the inline fetch"


def test_missing_cache_still_falls_back_to_zero(sandbox, monkeypatch):
    hooks, home, tmp = sandbox
    mod = _load_hook(hooks, monkeypatch, home)
    spawned = []
    monkeypatch.setattr(mod, "_spawn_background_usage_refresh", lambda: spawned.append(1))
    assert mod._get_pressure() == {"session": 0.0, "sonnet": 0.0, "weekly": 0.0}


def test_missing_refresh_script_degrades_without_blocking(sandbox):
    hooks, home, tmp = sandbox
    (hooks / "usage-refresh.py").unlink()
    _write_usage(home, age_s=900)
    elapsed, r = _run_hook(hooks, home, tmp)
    assert r.returncode == 0 and elapsed < 3.0
