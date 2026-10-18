"""F44: a test run must not leave detached background children behind.

INCIDENT: hook-heavy test runs left real `llm_router.cli semantic index` (~48% CPU
each) and `llm_router.ollama_watchdog` processes alive after pytest exited, loading the
machine and blocking a benchmark's quiet gate (load1 < 4). Cause: the session-start
hook (run here as a real subprocess) and tests/test_semantic_autoindex.py started
detached children; nothing in the suite stopped them.

Mechanism: tests/conftest.py sets LLM_ROUTER_NO_DETACHED_SPAWN=1 for the session
(inherited by hook subprocesses); every spawn site honours it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_HOOK = _ROOT / "hooks" / "session-start.py"

_SPY = """
import os, runpy, subprocess, sys
log = os.environ["F44_SPY_LOG"]
real = subprocess.Popen.__init__
def spy(self, args, *a, **k):
    if k.get("start_new_session"):
        open(log, "a").write(repr(args) + "\\n")
        # record, never really spawn: swap the child for `true` in this process group
        return real(self, ["true"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return real(self, args, *a, **k)
subprocess.Popen.__init__ = spy
sys.argv = [sys.argv[1]]
runpy.run_path(sys.argv[0], run_name="__main__")
"""


def _run_session_start(tmp_path: Path, extra_env: dict[str, str]) -> list[str]:
    home = tmp_path / "home"
    router = tmp_path / "router"
    home.mkdir(exist_ok=True)
    router.mkdir(exist_ok=True)
    log = tmp_path / "spawns.log"
    env = {k: v for k, v in os.environ.items() if not k.startswith("LLM_ROUTER_")}
    env.update(
        HOME=str(home),
        LLM_ROUTER_HOME=str(router),
        OLLAMA_HOST="http://127.0.0.1:9",
        OLLAMA_BASE_URL="http://127.0.0.1:9",
        F44_SPY_LOG=str(log),
        CLAUDE_SESSION_ID="",
        **extra_env,
    )
    payload = json.dumps({"session_id": "f44", "cwd": str(tmp_path), "source": "startup"})
    subprocess.run([sys.executable, "-c", _SPY, str(_HOOK)], input=payload.encode(),
                   capture_output=True, env=env, cwd=str(tmp_path), timeout=90)
    return log.read_text().splitlines() if log.exists() else []


def test_suite_sets_the_flag():
    assert os.environ.get("LLM_ROUTER_NO_DETACHED_SPAWN") == "1"


def test_session_start_hook_starts_no_detached_child(tmp_path):
    spawned = _run_session_start(tmp_path, {"LLM_ROUTER_NO_DETACHED_SPAWN": "1"})
    assert spawned == [], f"detached children started under the flag: {spawned}"


def test_the_spy_sees_spawns_without_the_flag(tmp_path):
    """Non-vacuity: with the flag off the same path DOES attempt detached spawns
    (an empty set would pass the test above for any reason). The spy swaps every
    detached child for `true`, so nothing real is left running."""
    spawned = _run_session_start(tmp_path, {"LLM_ROUTER_NO_DETACHED_SPAWN": "0"})
    assert spawned, "spy recorded nothing: the guard test above proves nothing"


def test_watchdog_probe_and_autoindex_honour_the_flag(monkeypatch):
    from llm_router import ollama_watchdog
    from llm_router.semantic import autoindex, refresh

    def boom(*a, **k):
        raise AssertionError("Popen called under LLM_ROUTER_NO_DETACHED_SPAWN")

    monkeypatch.setattr(subprocess, "Popen", boom)
    assert ollama_watchdog._spawn_background_probe() is False
    assert autoindex.maybe_start_background_index() is False
    refresh._spawn_child(Path("."))


@pytest.mark.usefixtures("allow_detached_spawn")
def test_opt_out_fixture_fakes_the_child():
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                         start_new_session=True)
    assert p.poll() == 0 and p.pid == 0
