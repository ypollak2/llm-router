"""Tests for direct model execution and quality gates.

These tests verify that the direct executor:
  - Calls models in chain order
  - Skips Claude models (can't call from hook)
  - Applies quality gates to reject bad responses
  - Returns None when all models fail (falls through to Claude)
"""

from __future__ import annotations

from unittest.mock import patch

from llm_router.hooks.direct_executor import (
    ModelSpec,
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
    def test_skips_claude_models(self):
        """Claude models in chain should be skipped (can't call from hook)."""
        chain = [
            ModelSpec("claude", "claude-opus-4-6", quota_cost=3.0),
            ModelSpec("ollama", "qwen3.5"),
        ]
        with patch("llm_router.hooks.direct_executor.call_ollama", return_value="test response"):
            result = execute_chain("hello", chain, "query")
        assert result is not None
        assert result.model.provider == "ollama"

    def test_returns_none_when_all_fail(self):
        """When all non-Claude models fail, returns None for Claude fallthrough."""
        chain = [
            ModelSpec("ollama", "qwen3.5"),
            ModelSpec("gemini", "gemini-2.5-flash"),
        ]
        with patch("llm_router.hooks.direct_executor.call_ollama", return_value=None), \
             patch("llm_router.hooks.direct_executor.call_gemini", return_value=None):
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
        with patch("llm_router.hooks.direct_executor.call_ollama", return_value="ollama says hi"), \
             patch("llm_router.hooks.direct_executor.call_gemini", return_value="gemini says hi"):
            result = execute_chain("hello", chain, "query")
        assert result.model.provider == "ollama"
        assert result.text == "ollama says hi"

    def test_falls_through_on_quality_failure(self):
        """If first model returns garbage, try next."""
        chain = [
            ModelSpec("ollama", "qwen3.5"),
            ModelSpec("gemini", "gemini-2.5-flash"),
        ]
        with patch("llm_router.hooks.direct_executor.call_ollama", return_value="ok"), \
             patch("llm_router.hooks.direct_executor.call_gemini", return_value="Berlin is the capital of Germany."):
            result = execute_chain("hello", chain, "query")
        assert result.model.provider == "gemini"

    def test_result_has_latency(self):
        chain = [ModelSpec("ollama", "qwen3.5")]
        with patch("llm_router.hooks.direct_executor.call_ollama", return_value="test response here"):
            result = execute_chain("hello", chain, "query")
        assert result.latency_ms >= 0

    def test_empty_chain_returns_none(self):
        result = execute_chain("hello", [], "query")
        assert result is None

    def test_unknown_provider_skipped(self):
        chain = [ModelSpec("unknown_provider", "some-model")]
        result = execute_chain("hello", chain, "query")
        assert result is None
