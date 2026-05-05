"""Tests for token_budget module — budget allocation for routed prompts."""

import pytest

from llm_router.token_budget import (
    calculate_budget,
    estimate_tokens,
    fits_budget,
    get_model_context_limit,
    truncate_to_budget,
)
from llm_router.types import Complexity, TaskType


class TestGetModelContextLimit:
    """Test model context window lookup."""

    def test_exact_match(self):
        assert get_model_context_limit("ollama/gemma4:latest") == 8_192

    def test_prefix_fallback(self):
        assert get_model_context_limit("openai/gpt-future-model") == 128_000

    def test_unknown_model_uses_default(self):
        assert get_model_context_limit("unknown/mystery") == 32_000

    def test_ollama_qwen(self):
        assert get_model_context_limit("ollama/qwen3.5:latest") == 32_768

    def test_gemini_flash(self):
        assert get_model_context_limit("gemini/gemini-2.5-flash") == 1_048_576

    def test_codex_models(self):
        assert get_model_context_limit("codex/gpt-5.4") == 200_000


class TestCalculateBudget:
    """Test budget allocation logic."""

    def test_small_model_respects_limit(self):
        budget = calculate_budget(
            "ollama/gemma4:latest", TaskType.QUERY, Complexity.SIMPLE
        )
        # Total usable should not exceed model limit
        total_allocated = (
            budget.system_tokens + budget.context_tokens
            + budget.user_tokens + budget.output_reserve
        )
        assert total_allocated <= 8_192

    def test_output_reserve_minimum(self):
        budget = calculate_budget(
            "ollama/gemma4:latest", TaskType.CODE, Complexity.SIMPLE
        )
        assert budget.output_reserve >= 1_000

    def test_simple_tasks_get_small_budgets(self):
        budget = calculate_budget(
            "gemini/gemini-2.5-flash", TaskType.QUERY, Complexity.SIMPLE
        )
        # Even with 1M context, simple tasks are capped
        assert budget.total <= 4_000

    def test_complex_tasks_get_larger_budgets(self):
        simple = calculate_budget(
            "openai/gpt-4o", TaskType.CODE, Complexity.SIMPLE
        )
        complex_ = calculate_budget(
            "openai/gpt-4o", TaskType.CODE, Complexity.COMPLEX
        )
        assert complex_.context_tokens > simple.context_tokens

    def test_user_prompt_tokens_respected(self):
        budget = calculate_budget(
            "openai/gpt-4o", TaskType.QUERY, Complexity.MODERATE,
            user_prompt_tokens=500,
        )
        assert budget.user_tokens >= 500 or budget.user_tokens > 0

    def test_system_tokens_capped_at_300(self):
        budget = calculate_budget(
            "gemini/gemini-2.5-flash", TaskType.ANALYZE, Complexity.COMPLEX
        )
        assert budget.system_tokens <= 300

    def test_budget_is_frozen(self):
        budget = calculate_budget(
            "openai/gpt-4o", TaskType.QUERY, Complexity.SIMPLE
        )
        with pytest.raises(Exception):
            budget.total = 999  # type: ignore


class TestEstimateTokens:
    """Test token estimation."""

    def test_empty_string(self):
        assert estimate_tokens("") == 1  # minimum 1

    def test_short_text(self):
        # "hello world" = 11 chars -> ~3 tokens
        result = estimate_tokens("hello world")
        assert 1 <= result <= 5

    def test_longer_text(self):
        text = "a" * 400
        result = estimate_tokens(text)
        assert 90 <= result <= 110  # ~100 tokens


class TestFitsBudget:
    """Test budget fit checking."""

    def test_short_text_fits(self):
        assert fits_budget("hello", 100) is True

    def test_long_text_doesnt_fit(self):
        assert fits_budget("a" * 1000, 10) is False


class TestTruncateToBudget:
    """Test budget-aware truncation."""

    def test_short_text_unchanged(self):
        text = "short text"
        assert truncate_to_budget(text, 100) == text

    def test_long_text_truncated(self):
        text = "line one\nline two\nline three\n" * 100
        result = truncate_to_budget(text, 20)
        assert len(result) < len(text)
        assert "[truncated]" in result

    def test_truncation_preserves_line_boundaries(self):
        text = "line 1\nline 2\nline 3\nline 4\nline 5\n" * 50
        result = truncate_to_budget(text, 30)
        # Should end at a newline (before [truncated])
        lines = result.split("\n")
        assert lines[-1] == "[truncated]"

    def test_zero_budget_returns_marker(self):
        result = truncate_to_budget("some text", 0)
        assert result == "[truncated]"
