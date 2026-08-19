"""Regression test for CHZ-AUD-029.

Broad ``except Exception: pass/continue`` in the auto-route hook silently
swallowed failures, making "worked" indistinguishable from "silently failed".

The fail-open-to-caller contract is preserved (these paths still return None /
continue), but the swallowed error is now recorded via ``_debug_log`` so the
failure is diagnosable.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load_hook():
    src = (
        Path(__file__).resolve().parent.parent
        / "src"
        / "llm_router"
        / "hooks"
        / "auto-route.py"
    )
    spec = importlib.util.spec_from_file_location("auto_route_chz029", src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def hook():
    return _load_hook()


def test_ollama_classifier_logs_swallowed_error(hook, monkeypatch):
    """A failing Ollama call must still fail-open (return None) BUT log why."""
    logged: list[str] = []
    monkeypatch.setattr(hook, "_debug_log", lambda msg: logged.append(msg))
    # Force at least one model to be tried.
    monkeypatch.setattr(hook, "OLLAMA_MODELS", ["fake-model"], raising=False)
    monkeypatch.setattr(hook, "OLLAMA_CODE_MODELS", ["fake-model"], raising=False)

    def _boom(*args, **kwargs):
        raise ConnectionError("ollama unreachable")

    monkeypatch.setattr(hook.urllib.request, "urlopen", _boom)

    result = hook.classify_with_ollama("write a python function")

    # Fail-open contract preserved.
    assert result is None
    # But the failure is now observable.
    assert any("classify_with_ollama" in m and "ollama unreachable" in m for m in logged), logged


def test_get_active_policy_logs_swallowed_error(hook, monkeypatch):
    """A broken policy system must fail-open to None BUT log the reason."""
    logged: list[str] = []
    monkeypatch.setattr(hook, "_debug_log", lambda msg: logged.append(msg))
    monkeypatch.setattr(hook, "_ACTIVE_POLICY", None, raising=False)

    import llm_router.policy as policy_mod

    def _boom():
        raise RuntimeError("policy config corrupt")

    monkeypatch.setattr(policy_mod, "get_active_policy", _boom)

    result = hook._get_active_policy()

    assert result is None
    assert any("_get_active_policy" in m and "policy config corrupt" in m for m in logged), logged


def test_ollama_classifier_success_logs_nothing(hook, monkeypatch):
    """Happy path must not emit swallowed-error noise."""
    logged: list[str] = []
    monkeypatch.setattr(hook, "_debug_log", lambda msg: logged.append(msg))
    monkeypatch.setattr(hook, "OLLAMA_MODELS", ["m"], raising=False)
    monkeypatch.setattr(hook, "OLLAMA_CODE_MODELS", ["m"], raising=False)

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            import json

            return json.dumps({"message": {"content": "code"}}).encode()

    monkeypatch.setattr(hook.urllib.request, "urlopen", lambda *a, **k: _Resp())
    monkeypatch.setattr(hook, "_extract_category", lambda c: "code")

    result = hook.classify_with_ollama("write a python function")
    assert result == "code"
    assert not any("classify_with_ollama" in m for m in logged), logged
