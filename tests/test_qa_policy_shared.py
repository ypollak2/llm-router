"""P0.3 (PLAN v16, D-14 = A at every door): Q&A is never served by a local provider.

M3.0 (#297) applied the rule to MCP ``route_and_call`` only. The hook DIRECT path and the
in-process SDK (``llm_router.route``) build their chains with
``llm_router.hooks.chain_builder.build_chain``, which put Ollama first for every simple and
moderate Q&A prompt. The rule now lives in ``llm_router.qa_policy`` (one copy, light to
import) and both doors apply it.

Pinned here:
  1. ``build_chain``: 9 ``QA_TASK_TYPES`` x {simple, moderate} contain none of the five
     local providers (18 cases, MUST P0.3-a), in every pressure zone.
  2. ``code`` keeps local first and its chain is unchanged.
  3. The SDK: ``route("what is X", task_type="query")`` makes 0 Ollama calls.
  4. The router still uses the same object (behaviour unchanged for MCP).
  5. ``qa_policy`` does not import ``llm_router.router`` (hook import cost, [M41]).
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from llm_router.hooks import chain_builder, direct_executor
from llm_router.hooks.direct_executor import ModelSpec
from llm_router.northstar import QA_TASK_TYPES

LOCAL_FIVE = ("ollama", "lm_studio", "vllm", "llamacpp", "openai_compat")
ZONES = ("green", "yellow", "orange", "red", "critical")


def _fake_local() -> list[ModelSpec]:
    return [ModelSpec(p, f"{p}-model") for p in LOCAL_FIVE]


@pytest.fixture
def all_providers(monkeypatch):
    """Every local provider is 'installed' and both external keys are set."""
    monkeypatch.setattr(chain_builder, "_ollama_models", _fake_local)
    monkeypatch.setenv("GEMINI_API_KEY", "test-not-a-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-not-a-key")


def test_the_qa_set_is_the_nine_types_the_plan_names():
    """Guard the guard: the 18-case loop below is only as wide as this set."""
    assert len(QA_TASK_TYPES) == 9
    assert "code" not in QA_TASK_TYPES


@pytest.mark.parametrize("complexity", ["simple", "moderate"])
@pytest.mark.parametrize("task_type", sorted(QA_TASK_TYPES))
def test_hook_chain_has_no_local_provider_for_qa(all_providers, task_type, complexity):
    for zone in ZONES:
        chain = chain_builder.build_chain(complexity, zone, task_type)
        local = [f"{m.provider}/{m.model}" for m in chain if m.provider in LOCAL_FIVE]
        assert local == [], (task_type, complexity, zone, local)


def test_qa_chain_with_only_local_installed_is_empty_so_the_hook_falls_through(monkeypatch):
    """An Ollama-only install: no external key. The hook must not serve Q&A locally;
    an empty chain makes execute_chain return None and the turn falls through to Claude
    (the same mechanism the research branch already uses)."""
    monkeypatch.setattr(chain_builder, "_ollama_models", _fake_local)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert chain_builder.build_chain("simple", "green", "query") == []
    moderate = chain_builder.build_chain("moderate", "green", "query")
    assert [m.provider for m in moderate] == ["claude"]


@pytest.mark.parametrize("complexity", ["simple", "moderate"])
def test_code_keeps_local_first_and_unchanged(all_providers, complexity):
    chain = chain_builder.build_chain(complexity, "green", "code")
    assert chain[0].provider == "ollama"
    assert chain[: len(LOCAL_FIVE)] == _fake_local()


def test_sdk_query_never_calls_ollama(monkeypatch, all_providers):
    """``route("what is X", task_type="query")`` must not reach Ollama even when Ollama is
    installed, pulled, healthy and first in the unfiltered chain."""
    calls: dict[str, int] = {"ollama": 0, "gemini": 0}

    def fake_ollama(prompt, model, timeout, history, system_prompt):
        calls["ollama"] += 1
        return "Local answer about X that would pass the quality gate.", {}

    def fake_gemini(prompt, model, timeout, history, system_prompt):
        calls["gemini"] += 1
        return "External answer about X that passes the quality gate.", {}

    monkeypatch.setitem(direct_executor._PROVIDER_CALLS, "ollama", fake_ollama)
    monkeypatch.setitem(direct_executor._PROVIDER_CALLS, "gemini", fake_gemini)
    # Pre-flight would otherwise skip Ollama for "not pulled" and hide the bug.
    monkeypatch.setattr(direct_executor, "available_ollama_models",
                        lambda timeout=0.5: {m.model for m in _fake_local()})
    from llm_router import ollama_watchdog
    monkeypatch.setattr(ollama_watchdog, "gate", lambda *a, **k: None)
    monkeypatch.setattr(direct_executor, "_paid_budget_exhausted", lambda provider: False)
    monkeypatch.setattr(direct_executor, "_provider_reset_blocked", lambda provider: False)

    from llm_router.sdk import route

    result = route("what is X", task_type="query")
    assert calls["ollama"] == 0
    assert calls["gemini"] == 1
    assert result.provider == "gemini"


def test_router_uses_the_shared_policy_object():
    """MCP behaviour unchanged: router's names are the shared module's objects."""
    from llm_router import qa_policy, router

    assert router._QA_STRIP_PROVIDERS is qa_policy.QA_STRIP_PROVIDERS
    assert router._strip_local_for_qa is qa_policy.strip_local_for_qa
    assert set(LOCAL_FIVE) == set(qa_policy.QA_STRIP_PROVIDERS)


def test_strip_handles_model_specs_and_strings():
    from llm_router.qa_policy import strip_local_for_qa

    specs = [ModelSpec("ollama", "q"), ModelSpec("gemini", "g")]
    assert strip_local_for_qa(specs, "query") == [ModelSpec("gemini", "g")]
    assert strip_local_for_qa(specs, "code") == specs
    assert strip_local_for_qa(["ollama/q", "openai/o"], "query") == ["openai/o"]
    only_local = [ModelSpec("ollama", "q")]
    # MCP default: an Ollama-only chain is kept (an empty chain would fail the call).
    assert strip_local_for_qa(only_local, "query") == only_local
    # Hook/SDK: empty is the fall-through to Claude.
    assert strip_local_for_qa(only_local, "query", keep_if_only_local=False) == []


def test_qa_policy_import_does_not_load_the_router():
    """The hook path imports qa_policy; router import is ~3.6 s cold and was ~77% of the
    slow hook tail [M41]. northstar is not needed either."""
    code = (
        "import sys, llm_router.qa_policy as q; "
        "assert 'llm_router.router' not in sys.modules, 'router'; "
        "assert 'llm_router.northstar' not in sys.modules, 'northstar'; "
        "print(len(q.QA_TASK_TYPES))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         timeout=60)
    assert out.returncode == 0, out.stderr[-500:]
    assert out.stdout.strip() == "9"
