"""P3.8-a: a fresh HOME with no config, offline.

Two explicit cases (HOME and cwd empty, every router/provider variable stripped, no config file):

A. ZERO CONFIG + ONE KEYLESS BACKEND INSTALLED -> the request routes exactly once to it.
   The Codex CLI is the keyless backend, faked as installed (``router.is_codex_available``)
   with a fake ``router.run_codex``. This is the row "a fresh HOME with no config routes one
   request". Codex, the Gemini CLI and Ollama are all detected with zero config.
B. ZERO CONFIG + NOTHING INSTALLED -> ``NoRoutableModelsError`` with a ``llm-router doctor`` hint,
   before any backend call.

The Claude subscription route is NOT zero-config: it needs ``llm_router_claude_subscription``,
which defaults to False and is set by ``llm-router install`` (NFR-CFG gap, config.py).

The availability checks are patched on ``llm_router.router`` explicitly (router.py binds the
names at import, so patching ``llm_router.codex_agent`` is a no-op), not left to the autouse
``_hermetic_host_state`` in tests/conftest.py. Files outside the tmp HOME are guarded by
conftest's ``_real_home_untouched`` audit; this file adds no tautological tmp-dir check.

The CLI test is a smoke test of the entry point on an empty HOME, not routing evidence.
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

from llm_router.codex_agent import CodexResult
from llm_router.env_registry import ENV_REGISTRY
from llm_router.types import LLMResponse, TaskType

_CATEGORIES = {"llm_router", "provider_credential", "external_tool"}
_EXTRA = {"OPENAI_API_BASE"}


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
    return reg | _config_env_names() | _EXTRA


@pytest.fixture
def bare(tmp_path, monkeypatch):
    """Empty HOME + empty cwd, every router/provider variable removed, all network refused."""
    names = _strip_names()
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
    # The strip deleted these; keep them explicit rather than relying on conftest's autouse.
    monkeypatch.setenv("LLM_ROUTER_SEATS_AUTO", "off")
    monkeypatch.setenv("LLM_ROUTER_ALLOW_STUBS", "1")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.chdir(cwd)

    attempts: list[str] = []

    def _refuse(name):
        def f(*a, **k):
            attempts.append(f"{name}{a[:2]!r}")
            raise OSError("network is disabled in the zero-config test")
        return f

    monkeypatch.setattr(socket, "create_connection", _refuse("create_connection"))
    monkeypatch.setattr(socket, "getaddrinfo", _refuse("getaddrinfo"))
    monkeypatch.setattr(socket.socket, "connect", _refuse("connect"))
    monkeypatch.setattr(socket.socket, "connect_ex", _refuse("connect_ex"))
    monkeypatch.setattr(asyncio.base_events.BaseEventLoop, "getaddrinfo", _refuse("loop.getaddrinfo"))

    import llm_router.config as config_module

    config_module._config = None
    yield attempts
    config_module._config = None


@pytest.fixture
def fakes(monkeypatch):
    """Fake every dispatch path; availability is set per case. Returns (api_call, codex_run)."""
    import llm_router.router as router_module

    api = AsyncMock(return_value=LLMResponse(content="api", model="x/y", input_tokens=1, output_tokens=1,
                                              cost_usd=0.0, latency_ms=1.0, provider="openai"))
    codex = AsyncMock(return_value=CodexResult(content="Mock codex answer", model="gpt-5.5",
                                               exit_code=0, duration_sec=0.01))
    tracker = MagicMock()
    tracker.is_healthy.return_value = True
    monkeypatch.setattr(router_module, "get_tracker", lambda *a, **k: tracker)
    monkeypatch.setattr(router_module, "run_codex", codex)
    monkeypatch.setattr(router_module, "is_gemini_cli_available", lambda: False)
    with patch("llm_router.providers.call_llm", api):
        yield api, codex


def _route(prompt="What is 2 + 2?"):
    from llm_router import router

    return asyncio.run(router.route_and_call(TaskType.QUERY, prompt))


def test_strip_list_is_derived_not_hand_written():
    names = _strip_names()
    assert len(names) > 100, len(names)  # an empty set would pass everything
    assert {"ANTHROPIC_API_KEY", "GEMINI_API_KEY", "LLM_ROUTER_ENFORCE", "OPENAI_API_BASE"} <= names


def test_A_zero_config_with_one_keyless_backend_routes_once(bare, fakes, monkeypatch):
    import llm_router.router as router_module

    api, codex = fakes
    monkeypatch.setattr(router_module, "is_codex_available", lambda: True)
    out = _route()
    assert codex.call_count == 1, "the keyless backend was called exactly once"
    assert api.call_count == 0, "no keyed provider was used"
    assert out.provider == "codex" and out.model.startswith("codex/"), out.model
    assert codex.call_args.kwargs["model"] in out.model  # the selected model is the one dispatched
    assert out.content == "Mock codex answer"
    assert bare == [], f"network touched: {bare}"


def test_B_zero_config_nothing_installed_fails_with_a_doctor_hint(bare, fakes, monkeypatch):
    import llm_router.router as router_module
    from llm_router.router import NoRoutableModelsError

    api, codex = fakes
    monkeypatch.setattr(router_module, "is_codex_available", lambda: False)
    with pytest.raises(NoRoutableModelsError) as ei:
        _route()
    msg = str(ei.value)
    assert "Configured providers: none" in msg and "llm-router doctor" in msg, msg
    assert api.call_count == 0 and codex.call_count == 0


def test_cli_smoke_config_on_fresh_home_exits_zero_offline(tmp_path):
    """CLI smoke test of the entry point on an empty HOME (not routing evidence)."""
    home = tmp_path / "home"
    cwd = tmp_path / "cwd"
    sc = tmp_path / "sc"
    for d in (home, cwd, sc):
        d.mkdir()
    (sc / "sitecustomize.py").write_text(
        "import socket\n"
        "def _b(*a, **k):\n    raise RuntimeError('NETBLOCK')\n"
        "socket.create_connection = _b\nsocket.socket.connect = _b\nsocket.socket.connect_ex = _b\n")
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
