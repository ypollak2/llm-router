"""RTE-004: the direct-execution chain is governed like the router path.

Audit 2026-09-24 (02_runtime_routing.md RTE-004, confirmed in
13_verify_routing_config.md): `direct_executor.execute_chain` — used by the
auto-route hook, agent-route.py and sdk.py — never consulted the provider
budget and never scrubbed secrets before the outbound HTTP call, while paid
providers (gemini, openai) are reachable through it. The hook's own scrubber
only ran before LOCAL disk writes.

Draft of these tests offloaded to llm-router (qwen3-coder:30b) and corrected:
patches use create=True so they fail for the right reason before the fix, the
OKF write is stubbed (test writes leaked into the real knowledge store before,
audit CTX-04), and the system prompt is checked too.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from llm_router.hooks import direct_executor as de
from llm_router.hooks.direct_executor import ModelSpec, execute_chain

SECRET_TAIL = "A" * 90
SECRET = "my key is sk-ant-api03-" + SECRET_TAIL
LONG = "a sufficiently long and plain answer to the question. " * 5


@pytest.fixture(autouse=True)
def _no_okf_writes(monkeypatch):
    monkeypatch.setattr(de, "_okf_enrich", lambda *a, **k: None, raising=False)


def test_secrets_are_scrubbed_before_the_outbound_call():
    seen = []

    def fake(prompt, model, timeout, history, system_prompt):
        seen.append((prompt, history, system_prompt))
        return LONG, {}

    with patch.dict(de._PROVIDER_CALLS, {"openai": fake}), \
         patch.object(de, "_paid_budget_exhausted", return_value=False, create=True):
        execute_chain(f"please use {SECRET} to call the API",
                      [ModelSpec("openai", "gpt-4o-mini")], "query",
                      history=[{"role": "user", "content": SECRET}],
                      context=f"context mentions {SECRET}")
    assert len(seen) == 1, "premise: the provider was called"
    prompt, history, system_prompt = seen[0]
    assert SECRET_TAIL not in prompt and "please use" in prompt
    assert all(SECRET_TAIL not in m["content"] for m in history)
    assert SECRET_TAIL not in (system_prompt or "")


def test_budget_exhausted_paid_provider_is_skipped():
    fake = MagicMock(return_value=(LONG, {}))
    with patch.dict(de._PROVIDER_CALLS, {"openai": fake}), \
         patch.object(de, "_paid_budget_exhausted", return_value=True, create=True):
        result = execute_chain("hello there", [ModelSpec("openai", "gpt-4o-mini")], "query")
    assert result is None
    fake.assert_not_called()


def test_free_providers_are_not_budget_gated():
    fake = MagicMock(return_value=(LONG, {}))
    with patch.dict(de._PROVIDER_CALLS, {"ollama": fake}), \
         patch.object(de, "_paid_budget_exhausted", return_value=True, create=True), \
         patch.object(de, "available_ollama_models", return_value=None), \
         patch.object(de, "ollama_is_alive", return_value=True):
        result = execute_chain("hello there", [ModelSpec("ollama", "llama3")], "query")
    assert result is not None
    fake.assert_called_once()


def test_an_unreadable_budget_fails_closed_for_paid_providers(monkeypatch):
    """Unknown must not render as the favourable answer (CLAUDE.md S9): if the
    budget cannot be read, a paid call is skipped, not made."""
    def boom(provider):
        raise RuntimeError("budget store unavailable")
    monkeypatch.setattr("llm_router.budget.get_budget_state", boom, raising=False)
    assert de._paid_budget_exhausted("openai") is True


def test_text_injected_after_assembly_is_scrubbed_too(monkeypatch):
    """OKF injection rewrites the prompt inside execute_chain; repository text
    it adds must be scrubbed like the user's own prompt."""
    seen = []

    def fake(prompt, model, timeout, history, system_prompt):
        seen.append(prompt)
        return LONG, {}

    monkeypatch.setattr(de, "_okf_inject", lambda p, **kw: p + "\n\nREPO NOTE: " + SECRET)
    with patch.dict(de._PROVIDER_CALLS, {"openai": fake}), \
         patch.object(de, "_paid_budget_exhausted", return_value=False, create=True):
        execute_chain("plain question", [ModelSpec("openai", "gpt-4o-mini")], "query")
    assert seen and "REPO NOTE" in seen[0], "premise: the injected text reached the call"
    assert SECRET_TAIL not in seen[0]
