"""Tests for prompts module — task-specific system prompt registry."""

from llm_router.system_prompts import get_system_prompt
from llm_router.token_budget import estimate_tokens
from llm_router.types import Complexity, TaskType


class TestGetSystemPrompt:
    """Test system prompt selection logic."""

    def test_exact_match_query_simple(self):
        prompt = get_system_prompt(TaskType.QUERY, Complexity.SIMPLE)
        assert "directly" in prompt.lower() or "answer" in prompt.lower()
        assert len(prompt) > 10

    def test_exact_match_code_moderate(self):
        prompt = get_system_prompt(TaskType.CODE, Complexity.MODERATE)
        assert "code" in prompt.lower()

    def test_fallback_for_research_any_complexity(self):
        # Research has a (TaskType.RESEARCH, None) fallback
        for complexity in Complexity:
            prompt = get_system_prompt(TaskType.RESEARCH, complexity)
            assert "research" in prompt.lower() or "cite" in prompt.lower()

    def test_all_task_types_have_prompts(self):
        """Every text task type should return a non-empty prompt."""
        text_tasks = [TaskType.QUERY, TaskType.CODE, TaskType.ANALYZE,
                      TaskType.RESEARCH, TaskType.GENERATE]
        for task_type in text_tasks:
            for complexity in Complexity:
                prompt = get_system_prompt(task_type, complexity)
                assert prompt, f"No prompt for {task_type}/{complexity}"
                assert len(prompt) > 20

    def test_prompts_fit_within_budget(self):
        """All prompts should be under 200 tokens (our design constraint)."""
        text_tasks = [TaskType.QUERY, TaskType.CODE, TaskType.ANALYZE,
                      TaskType.RESEARCH, TaskType.GENERATE]
        for task_type in text_tasks:
            for complexity in Complexity:
                prompt = get_system_prompt(task_type, complexity)
                tokens = estimate_tokens(prompt)
                assert tokens <= 200, (
                    f"Prompt for {task_type}/{complexity} is {tokens} tokens "
                    f"(max 200): {prompt[:80]}..."
                )

    def test_no_preamble_patterns(self):
        """Prompts should not contain filler patterns they're meant to prevent."""
        filler = ["sure!", "great question", "i'd be happy", "certainly"]
        text_tasks = [TaskType.QUERY, TaskType.CODE, TaskType.ANALYZE,
                      TaskType.RESEARCH, TaskType.GENERATE]
        for task_type in text_tasks:
            for complexity in Complexity:
                prompt = get_system_prompt(task_type, complexity).lower()
                for f in filler:
                    assert f not in prompt, (
                        f"Prompt for {task_type}/{complexity} contains filler: {f}"
                    )

    def test_media_tasks_get_generic_fallback(self):
        """Media tasks (IMAGE, VIDEO, AUDIO) get the generic fallback."""
        for task_type in [TaskType.IMAGE, TaskType.VIDEO, TaskType.AUDIO]:
            prompt = get_system_prompt(task_type, Complexity.SIMPLE)
            # Should return the generic fallback (not crash)
            assert prompt
            assert len(prompt) > 10
