"""Behavioral regressions for map poller startup."""

import os
import subprocess
import sys
from types import SimpleNamespace

from llm_router import map_rediscovery, server


def test_import_does_not_start_map_poller(tmp_path):
    env = os.environ.copy()
    env.update(
        LLM_ROUTER_MAP_REDISCOVERY="1",
        HOME=str(tmp_path),
        LLM_ROUTER_HOME=str(tmp_path),
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import llm_router.server, llm_router.map_rediscovery as m; "
            "print(m._default_poller is None)",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.rstrip().endswith("True"), result.stdout


def test_main_starts_map_poller_before_run(monkeypatch):
    events = []
    sentinel = object()

    def fake_start_poller():
        events.append("start")
        return sentinel

    monkeypatch.setenv("LLM_ROUTER_MAP_REDISCOVERY", "1")
    monkeypatch.setattr(server, "_critical_modules_or_die", lambda: None)
    monkeypatch.setattr(server, "_startup_verify_or_die", lambda: None)
    monkeypatch.setattr(server, "mcp", SimpleNamespace(run=lambda: events.append("run")))
    monkeypatch.setattr(map_rediscovery, "_default_poller", None)
    monkeypatch.setattr(map_rediscovery, "start_poller", fake_start_poller)

    server.main()

    assert events == ["start", "run"]


def test_maybe_start_poller_obeys_switch(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(map_rediscovery, "_default_poller", None)
    monkeypatch.setattr(map_rediscovery, "start_poller", lambda: sentinel)
    monkeypatch.setenv("LLM_ROUTER_MAP_REDISCOVERY", "1")
    assert map_rediscovery.maybe_start_poller() is sentinel

    monkeypatch.setenv("LLM_ROUTER_MAP_REDISCOVERY", "0")
    assert map_rediscovery.maybe_start_poller() is None
