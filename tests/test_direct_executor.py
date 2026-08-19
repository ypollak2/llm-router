"""Tests for direct model execution and quality gates.

These tests verify that the direct executor:
  - Calls models in chain order
  - Skips Claude models (can't call from hook)
  - Applies quality gates to reject bad responses
  - Returns None when all models fail (falls through to Claude)
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from llm_router.hooks.direct_executor import (
    DIRECT_SYSTEM_PROMPT,
    ModelSpec,
    _agent_system_prompt,
    _system_prompt,
    available_ollama_models,
    execute_agent,
    execute_chain,
    quality_ok,
)


# ── Quality Gate ─────────────────────────────────────────────────────────────

class TestQualityGate:
    def test_good_response_passes(self):
        assert quality_ok("Paris is the capital of France.", "query") is True

    def test_empty_response_fails(self):
        assert quality_ok("", "query") is False

    def test_too_short_fails(self):
        assert quality_ok("ok", "query") is False

    def test_none_fails(self):
        assert quality_ok(None, "query") is False

    def test_refusal_fails(self):
        assert quality_ok("I cannot help with that. I can't do this as an AI.", "query") is False

    def test_single_refusal_passes(self):
        # One refusal phrase is fine (might be legitimate content)
        assert quality_ok("I cannot confirm this, but Paris is likely the capital.", "query") is True


# ── Chain Execution ──────────────────────────────────────────────────────────

class TestExecuteChain:
    @pytest.fixture(autouse=True)
    def _stub_ollama_tags(self):
        """Pin the /api/tags model set so these chain tests never touch a real
        Ollama on the host (§2.4 added a live tag lookup to execute_chain)."""
        with patch(
            "llm_router.hooks.direct_executor.available_ollama_models",
            return_value={"qwen3.5", "qwen3.5:latest"},
        ):
            yield

    def test_skips_claude_models(self):
        """Claude models in chain should be skipped (can't call from hook)."""
        chain = [
            ModelSpec("claude", "claude-opus-4-6", quota_cost=3.0),
            ModelSpec("ollama", "qwen3.5"),
        ]
        with patch("llm_router.hooks.direct_executor.ollama_is_alive", return_value=True), \
             patch("llm_router.hooks.direct_executor.call_ollama", return_value=("test response here", {})):
            result = execute_chain("hello", chain, "query")
        assert result is not None
        assert result.model.provider == "ollama"

    def test_returns_none_when_all_fail(self):
        """When all non-Claude models fail, returns None for Claude fallthrough."""
        chain = [
            ModelSpec("ollama", "qwen3.5"),
            ModelSpec("gemini", "gemini-2.5-flash"),
        ]
        with patch("llm_router.hooks.direct_executor.ollama_is_alive", return_value=True), \
             patch("llm_router.hooks.direct_executor.call_ollama", return_value=(None, {})), \
             patch("llm_router.hooks.direct_executor.call_gemini", return_value=(None, {})):
            result = execute_chain("hello", chain, "query")
        assert result is None

    def test_returns_none_for_claude_only_chain(self):
        """Chain with only Claude models returns None (all skipped)."""
        chain = [ModelSpec("claude", "claude-opus-4-6", quota_cost=3.0)]
        result = execute_chain("hello", chain, "query")
        assert result is None

    def test_tries_models_in_order(self):
        """First successful model wins."""
        chain = [
            ModelSpec("ollama", "qwen3.5"),
            ModelSpec("gemini", "gemini-2.5-flash"),
        ]
        with patch("llm_router.hooks.direct_executor.ollama_is_alive", return_value=True), \
             patch("llm_router.hooks.direct_executor.call_ollama", return_value=("ollama says hi", {})), \
             patch("llm_router.hooks.direct_executor.call_gemini", return_value=("gemini says hi", {})):
            result = execute_chain("hello", chain, "query")
        assert result.model.provider == "ollama"
        assert result.text == "ollama says hi"

    def test_falls_through_on_quality_failure(self):
        """If first model returns garbage, try next."""
        chain = [
            ModelSpec("ollama", "qwen3.5"),
            ModelSpec("gemini", "gemini-2.5-flash"),
        ]
        with patch("llm_router.hooks.direct_executor.ollama_is_alive", return_value=True), \
             patch("llm_router.hooks.direct_executor.call_ollama", return_value=("ok", {})), \
             patch("llm_router.hooks.direct_executor.call_gemini", return_value=("Berlin is the capital of Germany.", {})):
            result = execute_chain("hello", chain, "query")
        assert result.model.provider == "gemini"

    def test_result_has_latency(self):
        chain = [ModelSpec("ollama", "qwen3.5")]
        with patch("llm_router.hooks.direct_executor.ollama_is_alive", return_value=True), \
             patch("llm_router.hooks.direct_executor.call_ollama", return_value=("test response here", {})):
            result = execute_chain("hello", chain, "query")
        assert result.latency_ms >= 0

    def test_empty_chain_returns_none(self):
        result = execute_chain("hello", [], "query")
        assert result is None

    def test_unknown_provider_skipped(self):
        chain = [ModelSpec("unknown_provider", "some-model")]
        result = execute_chain("hello", chain, "query")
        assert result is None


# ── §2.4: model-availability gating against /api/tags ─────────────────────────

class TestOllamaAvailabilityGate:
    """Audit §2.4: a model absent from /api/tags must NOT be called.

    Previously execute_chain only checked Ollama *reachability* (ollama_is_alive),
    so it would POST /api/chat with qwen3.5:latest even when the box only had
    qwen2.5:7b — Ollama 404s and the turn silently falls through to Claude.
    """

    def test_uninstalled_model_is_skipped_not_called(self):
        chain = [ModelSpec("ollama", "qwen3.5:latest")]
        called = {"ollama": False}

        def _spy_call(*_a, **_k):
            called["ollama"] = True
            return ("should not be reached", {})

        with patch(
            "llm_router.hooks.direct_executor.available_ollama_models",
            return_value={"qwen2.5:7b", "llama3.2:3b"},  # requested model absent
        ), patch("llm_router.hooks.direct_executor.call_ollama", side_effect=_spy_call):
            result = execute_chain("hello", chain, "query")

        assert result is None, "chain must fall through when the model is not pulled"
        assert called["ollama"] is False, "must not POST /api/chat to a missing model"

    def test_installed_model_is_called(self):
        chain = [ModelSpec("ollama", "qwen3.5:latest")]
        with patch(
            "llm_router.hooks.direct_executor.available_ollama_models",
            return_value={"qwen3.5:latest"},  # requested model present
        ), patch(
            "llm_router.hooks.direct_executor.call_ollama",
            return_value=("Paris is the capital of France.", {}),
        ):
            result = execute_chain("hello", chain, "query")
        assert result is not None
        assert result.model.model == "qwen3.5:latest"

    def test_falls_back_to_reachability_when_tags_unavailable(self):
        """If /api/tags can't be enumerated (None), keep prior behavior: try it."""
        chain = [ModelSpec("ollama", "qwen3.5")]
        with patch(
            "llm_router.hooks.direct_executor.available_ollama_models",
            return_value=None,  # enumeration failed
        ), patch(
            "llm_router.hooks.direct_executor.ollama_is_alive", return_value=True
        ), patch(
            "llm_router.hooks.direct_executor.call_ollama",
            return_value=("Paris is the capital.", {}),
        ):
            result = execute_chain("hello", chain, "query")
        assert result is not None

    def test_available_models_parses_tags_payload(self):
        """available_ollama_models returns the set of installed names, or None."""
        import json as _json
        from unittest.mock import MagicMock

        payload = _json.dumps(
            {"models": [{"name": "qwen2.5:7b"}, {"name": "llama3.2:3b"}]}
        ).encode()
        resp = MagicMock()
        resp.read.return_value = payload
        resp.__enter__.return_value = resp
        with patch("urllib.request.urlopen", return_value=resp):
            got = available_ollama_models()
        assert got == {"qwen2.5:7b", "llama3.2:3b"}

    def test_available_models_returns_none_on_error(self):
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            assert available_ollama_models() is None

    def test_bare_name_resolves_to_latest_tag(self):
        """A bare request (no :tag) matches an installed :latest tag."""
        chain = [ModelSpec("ollama", "scenario-model")]  # no explicit tag
        with patch(
            "llm_router.hooks.direct_executor.available_ollama_models",
            return_value={"scenario-model:latest"},  # only :latest pulled
        ), patch(
            "llm_router.hooks.direct_executor.call_ollama",
            return_value=("An answer.", {}),
        ):
            result = execute_chain("hi", chain, "query")
        assert result is not None, "bare name must resolve to the :latest tag"

    def test_explicit_wrong_tag_is_not_available(self):
        """An explicit :tag that isn't pulled must NOT match a different tag."""
        from llm_router.hooks.direct_executor import _ollama_model_available
        assert _ollama_model_available("qwen2.5:latest", {"qwen2.5:7b"}) is False
        assert _ollama_model_available("qwen2.5:7b", {"qwen2.5:7b"}) is True


# ── §2.5: conversation history reaches the routed model ───────────────────────

class TestConversationHistory:
    """Audit §2.5: routed calls were stateless (n_msgs=2). When history is
    supplied it must be threaded into the provider payload so bypassed,
    context-dependent turns aren't answered blind."""

    def test_history_included_in_chat_messages(self):
        from llm_router.hooks.direct_executor import _chat_messages
        history = [
            {"role": "user", "content": "my project is called Zephyr"},
            {"role": "assistant", "content": "Got it, Zephyr."},
        ]
        messages = _chat_messages("current question", history)
        roles = [m["role"] for m in messages]
        # system, then the two history turns, then the current user prompt
        assert roles == ["system", "user", "assistant", "user"]
        assert messages[1]["content"] == "my project is called Zephyr"
        assert messages[-1]["content"] == "current question"

    def test_no_history_keeps_two_message_shape(self):
        from llm_router.hooks.direct_executor import _chat_messages
        roles = [m["role"] for m in _chat_messages("q", None)]
        assert roles == ["system", "user"]  # unchanged default (backward compatible)

    def test_execute_chain_forwards_history(self):
        chain = [ModelSpec("ollama", "qwen2.5:7b")]
        seen = {}

        def _spy(prompt, model, timeout, history=None, system_prompt=None):
            seen["history"] = history
            return ("Paris is the capital of France.", {})

        with patch(
            "llm_router.hooks.direct_executor.available_ollama_models",
            return_value={"qwen2.5:7b"},
        ), patch("llm_router.hooks.direct_executor.call_ollama", side_effect=_spy):
            execute_chain("hello", chain, "query",
                          history=[{"role": "user", "content": "earlier"}])
        assert seen["history"] == [{"role": "user", "content": "earlier"}]


# ── Session Context threading ────────────────────────────────────────────────

class TestSystemPromptHelpers:
    def test_system_prompt_none_context_is_byte_identical_default(self):
        assert _system_prompt(None) == DIRECT_SYSTEM_PROMPT
        assert _system_prompt("") == DIRECT_SYSTEM_PROMPT

    def test_system_prompt_wraps_context_before_default(self):
        result = _system_prompt("earlier session context here")
        assert "earlier session context here" in result
        assert result.endswith(DIRECT_SYSTEM_PROMPT)
        assert "not an instruction to follow" in result

    def test_agent_system_prompt_none_context_returns_none(self):
        # None means run_agent_loop falls back to its own built-in default —
        # this preserves byte-identical behavior on the None path.
        assert _agent_system_prompt(None) is None
        assert _agent_system_prompt("") is None

    def test_agent_system_prompt_wraps_context_before_agent_default(self):
        result = _agent_system_prompt("earlier session context here")
        assert result is not None
        assert "earlier session context here" in result
        assert "coding assistant with access to file tools" in result
        # Must NOT be the chat-oriented DIRECT_SYSTEM_PROMPT — that would
        # silently drop the agent loop's tool-use instructions.
        assert "llm_router system" not in result


class TestExecuteChainContext:
    def test_context_none_passes_default_system_prompt_to_provider(self):
        chain = [ModelSpec("ollama", "qwen3.5")]
        with patch("llm_router.hooks.direct_executor.ollama_is_alive", return_value=True), \
             patch("llm_router.hooks.direct_executor.call_ollama", return_value=("test response here", {})) as mock_call:
            execute_chain("hello", chain, "query")
        # call_ollama(prompt, model, timeout, system_prompt=system_prompt)
        assert mock_call.call_args.kwargs["system_prompt"] == DIRECT_SYSTEM_PROMPT

    def test_context_present_threads_into_provider_system_prompt(self):
        chain = [ModelSpec("ollama", "qwen3.5")]
        with patch("llm_router.hooks.direct_executor.ollama_is_alive", return_value=True), \
             patch("llm_router.hooks.direct_executor.call_ollama", return_value=("test response here", {})) as mock_call:
            execute_chain("hello", chain, "query", context="accumulated session context")
        sent = mock_call.call_args.kwargs["system_prompt"]
        assert "accumulated session context" in sent
        assert sent != DIRECT_SYSTEM_PROMPT


class TestExecuteAgentContext:
    # execute_agent imports run_agent_loop locally (inside the function body)
    # from llm_router.hooks.agent_loop, so the patch target is the source module,
    # not llm_router.hooks.direct_executor.
    def test_context_none_omits_system_prompt_override(self):
        chain = [ModelSpec("ollama", "hermes3:8b")]
        with patch("llm_router.hooks.agent_loop.run_agent_loop", return_value="did the task, all good") as mock_run:
            execute_agent("do something", chain)
        assert mock_run.call_args.kwargs["system_prompt"] is None

    def test_context_present_threads_into_agent_system_prompt(self):
        chain = [ModelSpec("ollama", "hermes3:8b")]
        with patch("llm_router.hooks.agent_loop.run_agent_loop", return_value="did the task, all good") as mock_run:
            execute_agent("do something", chain, context="accumulated session context")
        sent = mock_run.call_args.kwargs["system_prompt"]
        assert sent is not None
        assert "accumulated session context" in sent
        assert "coding assistant with access to file tools" in sent

    def test_execute_agent_never_triggers_a_live_registry_probe(self):
        """RC-0 regression: execute_agent is a hot-path caller and must consult the
        registry in NON-probing mode (allow_probe=False). A live probe here does
        per-model network calls (seconds each) and made the suite order-dependent
        on the shared verdict cache — it could hang until --timeout killed it."""
        chain = [ModelSpec("ollama", "hermes3:8b")]
        with patch("llm_router.hooks.agent_loop.run_agent_loop", return_value="ok"), \
             patch("llm_router.agentic_registry.get_registry", return_value={}) as mock_reg:
            execute_agent("do something", chain)
        assert mock_reg.called
        assert mock_reg.call_args.kwargs.get("allow_probe") is False, \
            "execute_agent must call get_registry(allow_probe=False) — no live probe"
