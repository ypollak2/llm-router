"""Regression: RED2-5-03 — the SessionStart pre-flight banner must not tell the
agent to "fix" a missing OPTIONAL provider when routing still works.

The banner injects `additionalContext` (an imperative addressed at the agent).
Previously ANY missing cloud key produced "✗ {KEY} missing / Fix before starting
implementation." even when OpenAI + Ollama + Claude-subscription were all
available. It must only emit an actionable imperative when ZERO routing paths
exist; otherwise stay informational.
"""
from __future__ import annotations

import importlib.util
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HOOK_PATH = ROOT / "src" / "llm_router" / "hooks" / "session-start.py"

FORBIDDEN_IMPERATIVE = "Fix before starting implementation"


def _load():
    spec = importlib.util.spec_from_file_location("llm_router_session_start_r2503", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _mock_ollama(mod, monkeypatch, running: bool):
    def fake_run(*a, **k):
        return types.SimpleNamespace(returncode=0 if running else 1, stdout=b"", stderr=b"")
    monkeypatch.setattr(mod.subprocess, "run", fake_run, raising=False)
    # session-start imports subprocess lazily inside the function; patch the global too.
    import subprocess as _sp
    monkeypatch.setattr(_sp, "run", fake_run)


def _clear_keys(monkeypatch):
    for k in ("OPENAI_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY", "LLM_ROUTER_ENFORCE"):
        monkeypatch.delenv(k, raising=False)


def test_optional_missing_key_is_not_actionable(monkeypatch):
    """The exact live case: OpenAI + Ollama up, Gemini missing → informational."""
    mod = _load()
    _clear_keys(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    _mock_ollama(mod, monkeypatch, running=True)
    out = mod._preflight_check()
    assert FORBIDDEN_IMPERATIVE not in out
    assert "No routing paths" not in out
    assert "Gemini" in out and "Optional providers not configured" in out


def test_only_ollama_available_is_not_actionable(monkeypatch):
    mod = _load()
    _clear_keys(monkeypatch)
    _mock_ollama(mod, monkeypatch, running=True)
    out = mod._preflight_check()
    assert FORBIDDEN_IMPERATIVE not in out
    assert "No routing paths" not in out


def test_zero_paths_is_actionable(monkeypatch):
    """No keys, Ollama down, not CC subscription → genuinely nothing can route."""
    mod = _load()
    _clear_keys(monkeypatch)
    monkeypatch.setattr(mod, "_CC_MODE", False, raising=False)
    _mock_ollama(mod, monkeypatch, running=False)
    out = mod._preflight_check()
    assert "No routing paths available" in out


def test_enforce_hard_is_a_heads_up_not_a_fix(monkeypatch):
    mod = _load()
    _clear_keys(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_ROUTER_ENFORCE", "hard")
    _mock_ollama(mod, monkeypatch, running=True)
    out = mod._preflight_check()
    assert FORBIDDEN_IMPERATIVE not in out
    assert "LLM_ROUTER_ENFORCE=hard" in out
