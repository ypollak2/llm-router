"""Tests for context_prep module — the prompt preparation pipeline."""

from llm_router.context_prep import PreparedPrompt, prepare_prompt
from llm_router.types import Complexity, TaskType


class TestPreparePrompt:
    """Test the full preparation pipeline."""

    def test_returns_prepared_prompt(self):
        result = prepare_prompt(
            user_prompt="What is Python?",
            task_type=TaskType.QUERY,
            complexity=Complexity.SIMPLE,
            target_model="ollama/gemma4:latest",
        )
        assert isinstance(result, PreparedPrompt)
        assert result.system
        assert result.user_prompt

    def test_system_prompt_is_task_specific(self):
        code = prepare_prompt(
            "Write a sort function",
            TaskType.CODE, Complexity.SIMPLE, "ollama/qwen3.5:latest",
        )
        query = prepare_prompt(
            "What is a sort function?",
            TaskType.QUERY, Complexity.SIMPLE, "ollama/qwen3.5:latest",
        )
        assert code.system != query.system

    def test_respects_existing_system_prompt(self):
        result = prepare_prompt(
            "Hello",
            TaskType.QUERY, Complexity.SIMPLE, "ollama/gemma4:latest",
            existing_system_prompt="You are a pirate. Talk like one.",
        )
        assert "pirate" in result.system

    def test_gemma4_budget_is_small(self):
        result = prepare_prompt(
            "Explain recursion",
            TaskType.QUERY, Complexity.SIMPLE, "ollama/gemma4:latest",
        )
        total = result.estimated_total_tokens
        # gemma4 has 8K context; simple task budget is capped at 4K
        # Prepared prompt should be well under that
        assert total < 4_000

    def test_full_system_with_no_context(self):
        result = prepare_prompt(
            "Hello",
            TaskType.QUERY, Complexity.SIMPLE, "openai/gpt-4o-mini",
        )
        # When no context, full_system should equal just the system prompt
        assert result.full_system == result.system
        assert result.context == ""
        assert result.context_source == "none"

    def test_user_prompt_preserved_when_short(self):
        original = "What is the meaning of life?"
        result = prepare_prompt(
            original,
            TaskType.QUERY, Complexity.SIMPLE, "openai/gpt-4o",
        )
        assert result.user_prompt == original

    def test_long_user_prompt_truncated_for_small_model(self):
        # Create a very long prompt that exceeds gemma4's budget
        long_prompt = "Explain this code:\n" + "x = 1\n" * 5000
        result = prepare_prompt(
            long_prompt,
            TaskType.CODE, Complexity.SIMPLE, "ollama/gemma4:latest",
        )
        assert len(result.user_prompt) < len(long_prompt)
        assert "[truncated]" in result.user_prompt

    def test_prepared_prompt_is_frozen(self):
        """PreparedPrompt is immutable."""
        result = prepare_prompt(
            "Hello", TaskType.QUERY, Complexity.SIMPLE, "openai/gpt-4o",
        )
        try:
            result.system = "new system"  # type: ignore
            assert False, "Should have raised"
        except (AttributeError, TypeError):
            pass  # Expected — frozen dataclass


class TestPreparedPromptProperties:
    """Test PreparedPrompt computed properties."""

    def test_estimated_total_tokens_reasonable(self):
        result = prepare_prompt(
            "Short question",
            TaskType.QUERY, Complexity.SIMPLE, "openai/gpt-4o-mini",
        )
        total = result.estimated_total_tokens
        # System prompt + empty context + short question
        assert 5 < total < 200

    def test_full_system_without_context(self):
        result = prepare_prompt(
            "Hello", TaskType.QUERY, Complexity.SIMPLE, "openai/gpt-4o",
        )
        assert result.full_system == result.system
        assert "---" not in result.full_system
