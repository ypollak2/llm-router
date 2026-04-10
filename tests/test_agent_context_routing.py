"""Tests for agent-context chain reordering (v3.4).

Validates _reorder_for_agent_context() and the state.py get/set_active_agent helpers.
"""

from __future__ import annotations

import pytest

from llm_router.router import _reorder_for_agent_context
from llm_router.state import get_active_agent, set_active_agent
from llm_router.types import Complexity


# ─────────────────────────────────────────────────────────────────────────────
# State helpers
# ─────────────────────────────────────────────────────────────────────────────

class TestActiveAgentState:
    def setup_method(self):
        set_active_agent(None)

    def test_default_is_none(self):
        assert get_active_agent() is None

    def test_set_and_get_claude_code(self):
        set_active_agent("claude_code")
        assert get_active_agent() == "claude_code"

    def test_set_and_get_codex(self):
        set_active_agent("codex")
        assert get_active_agent() == "codex"

    def test_clear_with_none(self):
        set_active_agent("codex")
        set_active_agent(None)
        assert get_active_agent() is None


# ─────────────────────────────────────────────────────────────────────────────
# Reorder function — no agent
# ─────────────────────────────────────────────────────────────────────────────

class TestReorderNoAgent:
    def test_no_agent_returns_unchanged(self):
        models = ["ollama/llama3", "anthropic/claude-haiku-4-5-20251001", "openai/gpt-4o"]
        result = _reorder_for_agent_context(models, None, Complexity.SIMPLE)
        assert result == models

    def test_empty_list_returns_empty(self):
        assert _reorder_for_agent_context([], None, Complexity.COMPLEX) == []


# ─────────────────────────────────────────────────────────────────────────────
# Codex session — simple/moderate
# ─────────────────────────────────────────────────────────────────────────────

_SAMPLE_CHAIN = [
    "ollama/llama3",
    "codex/gpt-5.4",
    "anthropic/claude-haiku-4-5-20251001",
    "openai/gpt-4o",
    "gemini/gemini-2.5-flash",
]


class TestCodexSessionSimple:
    """Codex + simple/moderate: Ollama → Codex → rest → Claude."""

    @pytest.mark.parametrize("complexity", [Complexity.SIMPLE, Complexity.MODERATE])
    def test_ollama_first(self, complexity):
        result = _reorder_for_agent_context(_SAMPLE_CHAIN, "codex", complexity)
        assert result[0].startswith("ollama/")

    @pytest.mark.parametrize("complexity", [Complexity.SIMPLE, Complexity.MODERATE])
    def test_codex_before_openai(self, complexity):
        result = _reorder_for_agent_context(_SAMPLE_CHAIN, "codex", complexity)
        codex_idx = next(i for i, m in enumerate(result) if m.startswith("codex/"))
        openai_idx = next(i for i, m in enumerate(result) if m.startswith("openai/"))
        assert codex_idx < openai_idx

    @pytest.mark.parametrize("complexity", [Complexity.SIMPLE, Complexity.MODERATE])
    def test_claude_last(self, complexity):
        result = _reorder_for_agent_context(_SAMPLE_CHAIN, "codex", complexity)
        claude_indices = [i for i, m in enumerate(result) if m.startswith("anthropic/")]
        non_claude_after = [m for m in result[max(claude_indices) + 1:] if not m.startswith("anthropic/")]
        assert non_claude_after == [], f"Non-Claude models appear after Claude: {non_claude_after}"

    @pytest.mark.parametrize("complexity", [Complexity.SIMPLE, Complexity.MODERATE])
    def test_all_models_preserved(self, complexity):
        result = _reorder_for_agent_context(_SAMPLE_CHAIN, "codex", complexity)
        assert sorted(result) == sorted(_SAMPLE_CHAIN)


# ─────────────────────────────────────────────────────────────────────────────
# Codex session — complex
# ─────────────────────────────────────────────────────────────────────────────

class TestCodexSessionComplex:
    """Codex + complex: Codex → Claude → rest → Ollama."""

    def test_codex_first(self):
        result = _reorder_for_agent_context(_SAMPLE_CHAIN, "codex", Complexity.COMPLEX)
        assert result[0].startswith("codex/")

    def test_claude_before_openai(self):
        result = _reorder_for_agent_context(_SAMPLE_CHAIN, "codex", Complexity.COMPLEX)
        claude_idx = next(i for i, m in enumerate(result) if m.startswith("anthropic/"))
        openai_idx = next(i for i, m in enumerate(result) if m.startswith("openai/"))
        assert claude_idx < openai_idx

    def test_ollama_last(self):
        result = _reorder_for_agent_context(_SAMPLE_CHAIN, "codex", Complexity.COMPLEX)
        ollama_indices = [i for i, m in enumerate(result) if m.startswith("ollama/")]
        non_ollama_after = [m for m in result[max(ollama_indices) + 1:] if not m.startswith("ollama/")]
        assert non_ollama_after == []

    def test_all_models_preserved(self):
        result = _reorder_for_agent_context(_SAMPLE_CHAIN, "codex", Complexity.COMPLEX)
        assert sorted(result) == sorted(_SAMPLE_CHAIN)


# ─────────────────────────────────────────────────────────────────────────────
# Claude Code session — simple/moderate
# ─────────────────────────────────────────────────────────────────────────────

class TestClaudeCodeSessionSimple:
    """Claude Code + simple/moderate: Ollama → Claude → rest → Codex."""

    @pytest.mark.parametrize("complexity", [Complexity.SIMPLE, Complexity.MODERATE])
    def test_ollama_first(self, complexity):
        result = _reorder_for_agent_context(_SAMPLE_CHAIN, "claude_code", complexity)
        assert result[0].startswith("ollama/")

    @pytest.mark.parametrize("complexity", [Complexity.SIMPLE, Complexity.MODERATE])
    def test_claude_before_openai(self, complexity):
        result = _reorder_for_agent_context(_SAMPLE_CHAIN, "claude_code", complexity)
        claude_idx = next(i for i, m in enumerate(result) if m.startswith("anthropic/"))
        openai_idx = next(i for i, m in enumerate(result) if m.startswith("openai/"))
        assert claude_idx < openai_idx

    @pytest.mark.parametrize("complexity", [Complexity.SIMPLE, Complexity.MODERATE])
    def test_codex_last(self, complexity):
        result = _reorder_for_agent_context(_SAMPLE_CHAIN, "claude_code", complexity)
        codex_indices = [i for i, m in enumerate(result) if m.startswith("codex/")]
        non_codex_after = [m for m in result[max(codex_indices) + 1:] if not m.startswith("codex/")]
        assert non_codex_after == []

    @pytest.mark.parametrize("complexity", [Complexity.SIMPLE, Complexity.MODERATE])
    def test_all_models_preserved(self, complexity):
        result = _reorder_for_agent_context(_SAMPLE_CHAIN, "claude_code", complexity)
        assert sorted(result) == sorted(_SAMPLE_CHAIN)


# ─────────────────────────────────────────────────────────────────────────────
# Claude Code session — complex
# ─────────────────────────────────────────────────────────────────────────────

class TestClaudeCodeSessionComplex:
    """Claude Code + complex: Claude → rest → Codex → Ollama."""

    def test_claude_first(self):
        result = _reorder_for_agent_context(_SAMPLE_CHAIN, "claude_code", Complexity.COMPLEX)
        assert result[0].startswith("anthropic/")

    def test_openai_before_codex(self):
        result = _reorder_for_agent_context(_SAMPLE_CHAIN, "claude_code", Complexity.COMPLEX)
        openai_idx = next(i for i, m in enumerate(result) if m.startswith("openai/"))
        codex_idx = next(i for i, m in enumerate(result) if m.startswith("codex/"))
        assert openai_idx < codex_idx

    def test_ollama_last(self):
        result = _reorder_for_agent_context(_SAMPLE_CHAIN, "claude_code", Complexity.COMPLEX)
        ollama_indices = [i for i, m in enumerate(result) if m.startswith("ollama/")]
        non_ollama_after = [m for m in result[max(ollama_indices) + 1:] if not m.startswith("ollama/")]
        assert non_ollama_after == []

    def test_all_models_preserved(self):
        result = _reorder_for_agent_context(_SAMPLE_CHAIN, "claude_code", Complexity.COMPLEX)
        assert sorted(result) == sorted(_SAMPLE_CHAIN)


# ─────────────────────────────────────────────────────────────────────────────
# Edge cases
# ─────────────────────────────────────────────────────────────────────────────

class TestReorderEdgeCases:
    def test_chain_without_ollama(self):
        models = ["codex/gpt-5.4", "anthropic/claude-haiku-4-5-20251001", "openai/gpt-4o"]
        result = _reorder_for_agent_context(models, "codex", Complexity.SIMPLE)
        # Still: Codex first (no Ollama), then rest, then Claude
        assert result[0].startswith("codex/")
        assert result[-1].startswith("anthropic/")

    def test_chain_without_codex(self):
        models = ["ollama/llama3", "anthropic/claude-haiku-4-5-20251001", "openai/gpt-4o"]
        result = _reorder_for_agent_context(models, "codex", Complexity.SIMPLE)
        # No Codex models → Ollama first, then rest, then Claude
        assert result[0].startswith("ollama/")
        assert result[-1].startswith("anthropic/")

    def test_chain_without_claude(self):
        models = ["ollama/llama3", "codex/gpt-5.4", "openai/gpt-4o"]
        result = _reorder_for_agent_context(models, "claude_code", Complexity.COMPLEX)
        # No Claude → complex order: (empty claude) + rest → Codex → Ollama
        # "rest" (openai) comes first, Ollama last
        assert result[-1].startswith("ollama/")
        assert sorted(result) == sorted(models)

    def test_deep_reasoning_treated_as_complex_for_codex(self):
        result_complex = _reorder_for_agent_context(_SAMPLE_CHAIN, "codex", Complexity.COMPLEX)
        result_deep = _reorder_for_agent_context(_SAMPLE_CHAIN, "codex", Complexity.DEEP_REASONING)
        assert result_complex == result_deep
