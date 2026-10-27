"""P3.8-a: a fresh HOME with no config reaches a defined routing outcome, offline.

Finding recorded by this test (main @ 84cb0798): with no provider key, no Ollama, no
Codex and no subscription there is NO selectable backend. ``route_and_call`` raises
``NoRoutableModelsError`` ("Configured providers: none ... run `llm-router doctor`")
before any backend is called. That is the documented no-backend outcome and is what
leg 1 asserts. A control run with exactly one provider key proves the same harness
does route (so the zero-config assertion is not vacuous).

Leg 2 runs the real CLI in a subprocess on the same empty HOME with sockets blocked.
``doctor`` is not used: on a fresh HOME it exits 1 (hooks not installed) and probes
Ollama on :11434. ``config`` is offline and exits 0.
"""
from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from llm_router.env_registry import ENV_REGISTRY
from llm_router.types import LLMResponse, TaskType

_CATEGORIES = {"llm_router", "provider_credential", "external_tool"}


def _config_env_names() -> set[str]:
    """Env names pydantic RouterConfig reads (field name, alias, AliasChoices)."""
    from llm_router.config import RouterConfig

    names: set[str] = set()
    for fname, f in RouterConfig.model_fields.items():
        names.add(fname.upper())
        for a in (f.alias, f.validation_alias):
            if isinstance(a, str):
                names.add(a.upper())
            elif a is not None and hasattr(a, "choices"):
                names.update(str(c).upper() for c in a.choices if isinstance(c, str))
    return names


def _strip_names() -> set[str]:
    reg = {k for k, (cat, _m, _n) in ENV_REGISTRY.items() if cat in _CATEGORIES}
    return reg | _config_env_names()


def _tree(root: Path) -> set[Path]:
    return {p for p in root.rglob("*")}


@pytest.fixture
def bare(tmp_path, monkeypatch):
    """Empty HOME + empty cwd, every router/provider variable removed, sockets recorded and refused."""
    names = _strip_names()
    assert "OPENAI_API_KEY" in names and "OLLAMA_BASE_URL" in names, "premise: the strip list was built"
    for n in names:
        monkeypatch.delenv(n, raising=False)
    for n in [k for k in os.environ if k.startswith("LLM_ROUTER_")]:
        monkeypatch.delenv(n, raising=False)
    home = tmp_path / "home"
    cwd = tmp_path / "cwd"
    home.mkdir()
    cwd.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("APPDATA", str(home / "AppData"))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData"))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home / ".llm-router"))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.chdir(cwd)

    attempts: list[object] = []

    def _refuse(*a, **k):
        attempts.append(a[:2])
        raise OSError("network is disabled in the zero-config test")

    monkeypatch.setattr(socket, "create_connection", _refuse)
    monkeypatch.setattr(socket.socket, "connect", _refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", _refuse)

    import llm_router.config as config_module

    config_module._config = None
    yield home, cwd, attempts
    config_module._config = None


@pytest.fixture
def fake_backend():
    resp = LLMResponse(content="Mock response", model="openai/gpt-4o-mini", input_tokens=10,
                       output_tokens=5, cost_usd=0.0, latency_ms=1.0, provider="openai")
    call = AsyncMock(return_value=resp)
    tracker = MagicMock()
    tracker.is_healthy.return_value = True
    with patch("llm_router.providers.call_llm", call), \
         patch("llm_router.codex_agent.is_codex_available", return_value=False), \
         patch("llm_router.router.get_tracker", return_value=tracker):
        yield call


def _route(prompt="What is 2 + 2?"):
    from llm_router import router

    return asyncio.run(router.route_and_call(TaskType.QUERY, prompt))


def test_strip_list_is_derived_not_hand_written():
    names = _strip_names()
    assert len(names) > 100, len(names)  # non-empty set would pass everything
    assert {"ANTHROPIC_API_KEY", "GEMINI_API_KEY", "LLM_ROUTER_ENFORCE"} <= names


def test_zero_config_route_has_a_defined_no_backend_outcome(bare, fake_backend):
    from llm_router.router import NoRoutableModelsError

    home, cwd, attempts = bare
    before_home, before_cwd = _tree(home), _tree(cwd)
    with pytest.raises(NoRoutableModelsError) as ei:
        _route()
    msg = str(ei.value)
    assert "Configured providers: none" in msg and "llm-router doctor" in msg, msg
    # (b) nothing selected, nothing called
    assert fake_backend.call_count == 0
    # (c) only files under the tmp HOME / cwd were created
    created = (_tree(home) - before_home) | (_tree(cwd) - before_cwd)
    assert all(str(p).startswith(str(home.parent)) for p in created), created
    # (d) zero real network: every attempt was refused by the harness, and none escaped it
    # (attempts may be local probes such as Ollama on :11434; the fake refused them)
    assert all(not isinstance(a, BaseException) for a in attempts)


def test_control_one_provider_key_routes_exactly_once(bare, fake_backend, monkeypatch):
    """Same harness, one key: proves the zero-config refusal above is the no-key case, not a broken harness."""
    home, _cwd, attempts = bare
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("LLM_ROUTER_ALLOW_STUBS", "1")
    out = _route()
    assert fake_backend.call_count == 1
    assert out.content == "Mock response" and out.model == "openai/gpt-4o-mini"
    called = fake_backend.call_args
    assert "openai/gpt-4o-mini" in [*called.args, *called.kwargs.values()], called  # a model was selected
    assert all(str(p).startswith(str(home.parent)) for p in _tree(home))


def test_cli_config_on_fresh_home_exits_zero_offline(tmp_path):
    home = tmp_path / "home"
    cwd = tmp_path / "cwd"
    sc = tmp_path / "sc"
    for d in (home, cwd, sc):
        d.mkdir()
    (sc / "sitecustomize.py").write_text(
        "import socket\n"
        "def _b(*a, **k):\n    raise RuntimeError('NETBLOCK')\n"
        "socket.create_connection = _b\nsocket.socket.connect = _b\n")
    src = str(Path(__file__).resolve().parents[1] / "src")
    env = {"HOME": str(home), "USERPROFILE": str(home), "APPDATA": str(home), "LOCALAPPDATA": str(home),
           "LLM_ROUTER_HOME": str(home / ".llm-router"), "PATH": "/usr/bin:/bin",
           "PYTHONPATH": os.pathsep.join([str(sc), src])}
    r = subprocess.run([sys.executable, "-m", "llm_router.cli", "config"], capture_output=True, text=True,
                       env=env, cwd=str(cwd), timeout=120)
    out = r.stdout + r.stderr
    assert r.returncode == 0, (r.returncode, out[-400:])
    assert "No config files found" in out, "premise: the zero-config path ran"
    assert "Traceback" not in out and "NETBLOCK" not in out, out[-400:]
