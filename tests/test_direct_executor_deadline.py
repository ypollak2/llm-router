"""The chain must finish inside the hook's wall-clock budget.

Regression for 2026-09-14: the per-model timeout (45s) was set without reference
to the hook timeout (60s), so a slow first model left no room for a fallback, the
process was killed, and Claude Code discarded the output. The user saw that as
"no routing", never as an error, and two runs of one configuration scored 17% and
53% purely on whether the first model happened to be fast.
"""
from __future__ import annotations

import time

import pytest

from llm_router.hooks.direct_executor import ModelSpec, execute_chain


def _spec(model: str) -> ModelSpec:
    return ModelSpec(provider="ollama", model=model)


@pytest.fixture(autouse=True)
def _no_preflight(monkeypatch):
    monkeypatch.setattr(
        "llm_router.hooks.direct_executor.available_ollama_models",
        lambda timeout=0.5: {"slow:latest", "fast:latest"},
    )


def test_first_model_cannot_consume_the_fallbacks_budget(monkeypatch):
    seen: list[float] = []

    def _call(prompt, model, timeout, history, system_prompt):
        seen.append(timeout)
        return ("x" * 400, {}) if model == "fast:latest" else (None, {})

    monkeypatch.setitem(
        __import__("llm_router.hooks.direct_executor", fromlist=["x"])._PROVIDER_CALLS,
        "ollama", _call,
    )
    result = execute_chain(
        "q", [_spec("slow:latest"), _spec("fast:latest")], "query",
        timeout=45, deadline_s=time.monotonic() + 30.0,
    )
    assert result is not None, "the fallback never got a chance to answer"
    assert seen[0] < 45, f"first model was handed its full {seen[0]}s inside a 30s budget"
    assert seen[0] <= 30 - 18 + 0.5, (
        f"first model got {seen[0]:.1f}s, leaving under the 18s a fallback needs"
    )


def test_no_call_is_started_with_no_budget_left(monkeypatch):
    called: list[str] = []

    def _call(prompt, model, timeout, history, system_prompt):
        called.append(model)
        return ("x" * 400, {})

    monkeypatch.setitem(
        __import__("llm_router.hooks.direct_executor", fromlist=["x"])._PROVIDER_CALLS,
        "ollama", _call,
    )
    result = execute_chain(
        "q", [_spec("slow:latest")], "query",
        timeout=45, deadline_s=time.monotonic() - 1.0,
    )
    assert called == [], "a call was started after the hook budget was already gone"
    assert result is None


def test_absent_deadline_keeps_the_old_behaviour(monkeypatch):
    seen: list[float] = []

    def _call(prompt, model, timeout, history, system_prompt):
        seen.append(timeout)
        return ("x" * 400, {})

    monkeypatch.setitem(
        __import__("llm_router.hooks.direct_executor", fromlist=["x"])._PROVIDER_CALLS,
        "ollama", _call,
    )
    execute_chain("q", [_spec("fast:latest")], "query", timeout=45)
    assert seen == [45.0]
