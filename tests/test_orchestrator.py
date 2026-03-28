"""Tests for multi-step orchestration."""

from unittest.mock import AsyncMock, patch

import pytest

from llm_router.orchestrator import (
    PIPELINE_TEMPLATES,
    auto_orchestrate,
    run_pipeline,
)
from llm_router.types import PipelineStep, RoutingProfile, TaskType


@pytest.fixture
def mock_route(mock_litellm_response):
    """Mock route_and_call to return predictable responses."""
    call_count = 0

    async def _route(task_type, prompt, **kwargs):
        nonlocal call_count
        call_count += 1
        from llm_router.types import LLMResponse
        return LLMResponse(
            content=f"Step {call_count} result for {task_type.value}",
            model=f"openai/gpt-4o",
            input_tokens=100,
            output_tokens=50,
            cost_usd=0.001,
            latency_ms=500.0,
            provider="openai",
        )

    with patch("llm_router.orchestrator.route_and_call", side_effect=_route) as mock:
        yield mock


class TestRunPipeline:
    @pytest.mark.asyncio
    async def test_basic_pipeline(self, mock_env, mock_route):
        steps = [
            PipelineStep(task_type=TaskType.RESEARCH, prompt_template="Research: {input}"),
            PipelineStep(task_type=TaskType.GENERATE, prompt_template="Write about: {previous_result}"),
        ]
        result = await run_pipeline(steps, "AI trends")

        assert len(result.steps) == 2
        assert result.total_cost_usd == pytest.approx(0.002)
        assert "Step 2" in result.final_content

    @pytest.mark.asyncio
    async def test_pipeline_passes_previous_result(self, mock_env, mock_route):
        steps = [
            PipelineStep(task_type=TaskType.RESEARCH, prompt_template="Find: {input}"),
            PipelineStep(task_type=TaskType.ANALYZE, prompt_template="Analyze: {previous_result}"),
        ]
        result = await run_pipeline(steps, "competitors")

        # Second call should have received the first step's output in its prompt
        calls = mock_route.call_args_list
        assert "Step 1" in calls[1].args[1]  # prompt contains previous result

    @pytest.mark.asyncio
    async def test_step_n_references(self, mock_env, mock_route):
        steps = [
            PipelineStep(task_type=TaskType.RESEARCH, prompt_template="A: {input}"),
            PipelineStep(task_type=TaskType.RESEARCH, prompt_template="B: {input}"),
            PipelineStep(task_type=TaskType.GENERATE, prompt_template="Combine {step_0} and {step_1}"),
        ]
        result = await run_pipeline(steps, "topic")
        assert len(result.steps) == 3


class TestPipelineTemplates:
    def test_all_templates_valid(self):
        for name, steps in PIPELINE_TEMPLATES.items():
            assert len(steps) >= 2, f"Template {name} has too few steps"
            for step in steps:
                assert step.task_type in TaskType
                assert "{" in step.prompt_template  # has template vars

    def test_research_report_template(self):
        steps = PIPELINE_TEMPLATES["research_report"]
        types = [s.task_type for s in steps]
        assert types == [TaskType.RESEARCH, TaskType.ANALYZE, TaskType.GENERATE]

    def test_code_review_template(self):
        steps = PIPELINE_TEMPLATES["code_review_fix"]
        types = [s.task_type for s in steps]
        assert types == [TaskType.ANALYZE, TaskType.CODE, TaskType.CODE]


class TestAutoOrchestrate:
    @pytest.mark.asyncio
    async def test_auto_decompose(self, mock_env):
        """Test auto orchestration with mocked decomposition."""
        import json
        decompose_result = json.dumps([
            {"task_type": "research", "prompt": "Find info about {input}"},
            {"task_type": "generate", "prompt": "Write about {previous_result}"},
        ])

        call_count = 0

        async def _route(task_type, prompt, **kwargs):
            nonlocal call_count
            call_count += 1
            from llm_router.types import LLMResponse
            # First call is decomposition, return JSON
            content = decompose_result if call_count == 1 else f"Result {call_count}"
            return LLMResponse(
                content=content,
                model="openai/gpt-4o",
                input_tokens=100,
                output_tokens=50,
                cost_usd=0.001,
                latency_ms=300.0,
                provider="openai",
            )

        with patch("llm_router.orchestrator.route_and_call", side_effect=_route):
            result = await auto_orchestrate("Tell me about AI")

        # 1 decomposition + 2 pipeline steps = 3 total
        assert len(result.steps) == 3
        assert result.total_cost_usd == pytest.approx(0.003)
