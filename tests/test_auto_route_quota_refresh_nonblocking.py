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


def _payload(tmp: Path) -> str:
    return json.dumps({
        "session_id": "t", "prompt": "what is the capital of france",
        "hook_event_name": "UserPromptSubmit", "cwd": str(tmp),
        "transcript_path": str(tmp / "t.jsonl"),
    })


def test_concurrent_invocations_start_one_refresh(sandbox):
    """6 hooks at once, 3 rounds, each round on a fresh marker. Weak alone (the
    check-then-touch window is tiny) - the flock test below is the
    deterministic guard; this one covers the real multi-process shape."""
    hooks, home, tmp = sandbox
    (tmp / "home").mkdir(exist_ok=True)
    for rnd in range(3):
        for f in ("usage_refresh_spawn.txt", "stub_started.log"):
            (home / f).unlink(missing_ok=True)
        _write_usage(home, age_s=900)
        procs = [
            subprocess.Popen([sys.executable, str(hooks / "auto-route.py")],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, env=_env(home, tmp))
            for _ in range(6)
        ]
        for p in procs:
            p.communicate(_payload(tmp), timeout=60)
        assert _wait_for(home / "stub_started.log")
        time.sleep(0.5)
        started = (home / "stub_started.log").read_text().splitlines()
        assert started == ["started"], f"round {rnd}: expected one refresh, got {started}"


def test_claim_is_serialized_by_the_flock(sandbox):
    """Deterministic: while another process holds the claim lock, a hook must
    not spawn - even though the marker is absent and usage.json is stale."""
    import fcntl
    hooks, home, tmp = sandbox
    _write_usage(home, age_s=900)
    with open(home / "usage_refresh_spawn.txt.lock", "w") as lockf:
        fcntl.flock(lockf, fcntl.LOCK_EX)
        _run_hook(hooks, home, tmp)
        time.sleep(1.0)
        assert not (home / "stub_started.log").exists(), "spawned despite held claim lock"
    assert not (home / "usage_refresh_spawn.txt").exists()


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


def test_future_dated_marker_does_not_block_refresh(sandbox):
    hooks, home, tmp = sandbox
    _write_usage(home, age_s=900)
    marker = home / "usage_refresh_spawn.txt"
    marker.write_text("0")
    future = time.time() + 30 * 86400
    os.utime(marker, (future, future))
    _run_hook(hooks, home, tmp)
    assert _wait_for(home / "stub_started.log"), "a future-dated marker blocked the refresh"


def test_spawn_is_detached_and_env_is_minimal(sandbox, monkeypatch):
    hooks, home, tmp = sandbox
    mod = _load_hook(hooks, monkeypatch, home)
    monkeypatch.setenv("FAKE_PROVIDER_API_KEY", "sk-secret")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy:1")
    seen = {}
    monkeypatch.setattr(mod.subprocess, "Popen", lambda argv, **kw: seen.update(kw))
    mod._spawn_background_usage_refresh()
    assert seen.get("start_new_session") is True
    env = seen["env"]
    assert env["LLM_ROUTER_HOME"] == str(home)
    assert "HOME" in env and "PATH" in env and env["HTTPS_PROXY"] == "http://proxy:1"
    assert "FAKE_PROVIDER_API_KEY" not in env


def test_child_env_excludes_router_secrets(sandbox, monkeypatch):
    hooks, home, tmp = sandbox
    mod = _load_hook(hooks, monkeypatch, home)
    secrets = ("LLM_ROUTER_TOKEN", "LLM_ROUTER_GATEWAY_TOKEN", "LLM_ROUTER_SCIM_TOKEN",
               "LLM_ROUTER_CP_SIDECAR_TOKEN", "LLM_ROUTER_BROKER_SECRET_FILE")
    for k in secrets:
        monkeypatch.setenv(k, "s3cret")
    monkeypatch.setenv("LLM_ROUTER_SLIM", "1")
    env = mod._refresh_child_env()
    assert not [k for k in secrets if k in env]
    assert env["LLM_ROUTER_HOME"] == str(home) and env["LLM_ROUTER_SLIM"] == "1"


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_updated_at_is_unknown(sandbox, monkeypatch, bad):
    hooks, home, tmp = sandbox
    (home / "usage.json").write_text(
        '{"session_pct": 99, "weekly_pct": 99, "sonnet_pct": 99, "updated_at": %s}' % bad)
    mod = _load_hook(hooks, monkeypatch, home)
    monkeypatch.setattr(mod, "_spawn_background_usage_refresh", lambda: None)
    p = mod._get_pressure()
    assert p == {"session": 0.0, "sonnet": 0.0, "weekly": 0.0}
    assert mod._apply_pressure_downgrade("complex", p) == ("complex", "")


def _age_pressure(sandbox, monkeypatch, age_s, pct=99.0, ttl=None):
    hooks, home, tmp = sandbox
    _write_usage(home, age_s=age_s, session_pct=pct)
    (home / "usage.json").write_text(json.dumps({
        "session_pct": pct, "weekly_pct": pct, "sonnet_pct": pct,
        "updated_at": time.time() - age_s,
    }))
    mod = _load_hook(hooks, monkeypatch, home)
    monkeypatch.setattr(mod, "_spawn_background_usage_refresh", lambda: None)
    monkeypatch.delenv("LLM_ROUTER_QUOTA_MAX_AGE", raising=False)
    return mod


def test_fresh_99_downgrades_without_stale_marker(sandbox, monkeypatch):
    mod = _age_pressure(sandbox, monkeypatch, age_s=10)
    p = mod._get_pressure()
    assert mod._apply_pressure_downgrade("complex", p) == ("moderate", " [⬇ sonnet-exhausted: complex→moderate]")
    assert mod._critical_pressure_reading(p) is not None


def test_two_hour_old_99_downgrades_and_carries_stale_marker(sandbox, monkeypatch):
    mod = _age_pressure(sandbox, monkeypatch, age_s=2 * 3600)
    p = mod._get_pressure()
    cx, suffix = mod._apply_pressure_downgrade("complex", p)
    assert cx == "moderate" and "STALE USAGE DATA" in suffix
    assert mod._critical_pressure_reading(p) is not None
    assert "STALE USAGE DATA" in mod._stale_pressure_note()


def test_seven_hour_old_99_is_unknown(sandbox, monkeypatch):
    mod = _age_pressure(sandbox, monkeypatch, age_s=7 * 3600)
    p = mod._get_pressure()
    assert p == {"session": 0.0, "sonnet": 0.0, "weekly": 0.0}
    assert mod._apply_pressure_downgrade("complex", p) == ("complex", "")
    assert mod._critical_pressure_reading(p) is None


def test_max_age_is_env_overridable(sandbox, monkeypatch):
    mod = _age_pressure(sandbox, monkeypatch, age_s=7 * 3600)
    monkeypatch.setenv("LLM_ROUTER_QUOTA_MAX_AGE", str(8 * 3600))
    assert mod._get_pressure()["session"] == pytest.approx(0.99)
