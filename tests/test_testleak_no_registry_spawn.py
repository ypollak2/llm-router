"""TESTLEAK: the test suite cannot start the background agentic-registry probe.

`install_hooks.install()` calls `agentic_registry.populate_in_background()`, which
starts a detached `python -m llm_router.agentic_registry`. That child has none of
the conftest guards and ran live Ollama inference after its test ended (PR #797
review: 5 left running; PR #796: 8). CI has no Ollama, so only a developer machine
with installed models shows it. These tests simulate that machine
(`list_installed_models` returns a model) and watch `subprocess.Popen` with a spy
that records every argv and never starts the registry child.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import llm_router.agentic_registry as reg
import llm_router.install_hooks as ih
import llm_router.install_manifest as im

_REGISTRY = "llm_router.agentic_registry"


class _NoChild:
    """Stand-in for the registry child: already exited, never ran."""

    returncode = 0
    pid = 0

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0


def _mentions_registry(argv) -> bool:
    parts = [argv] if isinstance(argv, (str, bytes)) else list(argv or ())
    return any(_REGISTRY in (p.decode() if isinstance(p, bytes) else str(p)) for p in parts)


@pytest.fixture
def popen_spy(monkeypatch):
    """Record every Popen argv; a registry spawn is recorded and faked, never started."""
    real = subprocess.Popen
    seen: list = []

    class _SpyPopen(real):  # type: ignore[misc, valid-type]
        def __new__(cls, args, *a, **k):
            seen.append(args)
            if _mentions_registry(args):
                return _NoChild()
            return super().__new__(cls)

    monkeypatch.setattr(subprocess, "Popen", _SpyPopen)
    return seen


@pytest.fixture
def models_installed(monkeypatch):
    """A developer machine: Ollama answers /api/tags with one model."""
    monkeypatch.setattr(reg, "list_installed_models", lambda: ["hermes3:8b"])


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Point every install surface at a throwaway HOME (same shape as test_gh42)."""
    claude = tmp_path / ".claude"
    claude.mkdir()
    monkeypatch.setattr(ih, "_CLAUDE_DIR", claude)
    monkeypatch.setattr(ih, "_HOOKS_DST", claude / "hooks")
    monkeypatch.setattr(ih, "_RULES_DST", claude / "rules")
    monkeypatch.setattr(ih, "_SETTINGS_PATH", claude / "settings.json")
    monkeypatch.setattr(ih, "_CLAUDE_JSON_PATH", tmp_path / ".claude.json")
    monkeypatch.setattr(ih, "claude_desktop_config_path", lambda: tmp_path / "claude_desktop_config.json")
    monkeypatch.setattr(im, "_manifest_path", lambda: tmp_path / ".llm-router" / "install-manifest.json")
    (tmp_path / ".llm-router").mkdir()
    import shutil as _sh

    real_which = _sh.which
    monkeypatch.setattr(ih.shutil, "which", lambda n: None if n == "claude" else real_which(n))
    return tmp_path


def test_install_spawns_no_registry_probe(sandbox, models_installed, popen_spy):
    actions = ih.install()

    # Non-vacuous: install ran to the step just before the registry block.
    assert any("quota" in a for a in actions), actions
    assert [a for a in popen_spy if _mentions_registry(a)] == []
    assert not any("Probing local models" in a for a in actions)


def test_real_populate_honours_no_detached_spawn(models_installed, popen_spy, real_populate_in_background):
    # conftest sets LLM_ROUTER_NO_DETACHED_SPAWN=1 for the session; child processes
    # inherit it, which is the only layer that reaches an installer run as a child.
    assert reg.populate_in_background() is False
    assert popen_spy == []


def test_real_populate_spawns_when_flag_cleared(models_installed, popen_spy, real_populate_in_background, monkeypatch):
    # The spy sees the spawn the guards above prevent (the check is not vacuous).
    monkeypatch.delenv("LLM_ROUTER_NO_DETACHED_SPAWN", raising=False)
    assert reg.populate_in_background() is True
    assert [a[1:] for a in popen_spy] == [["-m", _REGISTRY]]


@pytest.mark.parametrize(
    "argv, shell",
    [
        (["/nonexistent/python", "-m", _REGISTRY], False),
        (f"/nonexistent/python -m {_REGISTRY}", True),
    ],
)
def test_popen_of_registry_child_is_refused(argv, shell):
    # A nonexistent interpreter: if the refusal ever stopped working this fails with
    # FileNotFoundError (or a shell exit) instead of starting a real probe.
    with pytest.raises(RuntimeError, match=_REGISTRY):
        subprocess.Popen(argv, shell=shell, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def test_other_python_children_are_not_refused():
    out = subprocess.run([sys.executable, "-c", "print('ok')"], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "ok"


def test_hook_fires_through_populate_and_nothing_spawns(
    models_installed, real_populate_in_background, registry_spawn_refusals, monkeypatch
):
    # Popen is NOT faked: the audit hook alone must stop the spawn, inside the
    # `except Exception` that turns it into a silent False. The interpreter path is
    # nonexistent, so if the hook ever stopped firing this records no refusal and
    # fails on FileNotFoundError instead of starting a real probe.
    monkeypatch.delenv("LLM_ROUTER_NO_DETACHED_SPAWN", raising=False)
    monkeypatch.setattr(sys, "executable", "/nonexistent/python")
    assert reg.populate_in_background() is False
    assert registry_spawn_refusals == [["/nonexistent/python", "-m", _REGISTRY]]


# The child has none of the conftest guards; only the environment reaches it.
# Popen is faked inside the child, so neither case can start a real probe.
_CHILD = """
import json, subprocess
from llm_router import agentic_registry as reg
reg.list_installed_models = lambda: ["hermes3:8b"]
seen = []
class _Fake:
    def __init__(self, args, *a, **k):
        seen.append([str(x) for x in args])
subprocess.Popen = _Fake
print(json.dumps({"spawned": reg.populate_in_background(), "argv": seen}))
"""


def _run_child(env: dict) -> dict:
    src = str(Path(reg.__file__).resolve().parents[1])
    env = {**env, "PYTHONPATH": os.pathsep.join(filter(None, [src, env.get("PYTHONPATH")]))}
    out = subprocess.run([sys.executable, "-c", _CHILD], env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_child_process_inherits_no_detached_spawn():
    assert os.environ.get("LLM_ROUTER_NO_DETACHED_SPAWN") == "1"  # set by conftest for the session
    inherited = _run_child(dict(os.environ))
    assert inherited == {"spawned": False, "argv": []}

    # Control: the same child without the flag reaches the spawn path.
    cleared = _run_child({k: v for k, v in os.environ.items() if k != "LLM_ROUTER_NO_DETACHED_SPAWN"})
    assert cleared["spawned"] is True
    assert [a[1:] for a in cleared["argv"]] == [["-m", _REGISTRY]]
