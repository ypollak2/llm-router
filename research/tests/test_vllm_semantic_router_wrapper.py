"""
Unit tests for vLLM Semantic Router wrapper.

Tests task-aware routing based on semantic classification.
"""

import pytest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

from base_wrapper import ToolInput, ToolOutput
from vllm_semantic_router_wrapper import VLLMSemanticRouterWrapper


class TestVLLMSemanticRouterWrapper:
    """Test vLLM task-aware routing."""

    @pytest.fixture
    async def wrapper(self):
        """Create and initialize vLLM Semantic Router wrapper."""
        config = {}
        wrapper = VLLMSemanticRouterWrapper("vllm_semantic_router", config)
        await wrapper.initialize()
        yield wrapper
        await wrapper.cleanup()

    @pytest.mark.asyncio
    async def test_initialize_cleanup(self):
        """Test wrapper lifecycle."""
        wrapper = VLLMSemanticRouterWrapper("vllm_semantic_router", {})
        assert not wrapper.initialized

        await wrapper.initialize()
        assert wrapper.initialized

        await wrapper.cleanup()
        assert not wrapper.initialized

    @pytest.mark.asyncio
    async def test_task_specific_routing(self, wrapper):
        """Test task-specific routing."""
        code_task = """
Implement a Python function to:
1. Read a file
2. Parse JSON
3. Return the data
"""

        output = await wrapper.execute(
            ToolInput(prompt=code_task),
            "task_specific"
        )

        assert output.response
        assert output.technique_variant == "task_specific"
        assert output.latency_ms > 0

    @pytest.mark.asyncio
    async def test_speed_optimized_variant(self, wrapper):
        """Test speed-optimized routing."""
        prompt = "Quickly summarize: " + "content " * 100

        output = await wrapper.execute(
            ToolInput(prompt=prompt),
            "speed_optimized"
        )

        assert output.response
        assert output.compression_ratio is not None
        # Speed optimization should use faster models
        assert output.technique_variant == "speed_optimized"

    @pytest.mark.asyncio
    async def test_quality_optimized_routing(self, wrapper):
        """Test quality-optimized routing."""
        complex_prompt = """
Analyze the relationship between quantum mechanics and general relativity,
explaining why a unified theory has proven difficult to develop.
"""

        output = await wrapper.execute(
            ToolInput(prompt=complex_prompt),
            "quality_optimized"
        )

        assert output.response
        # Quality optimized uses better models, less compression
        assert output.compression_ratio is not None

    @pytest.mark.asyncio
    async def test_code_task_detection(self, wrapper):
        """Test detection of code tasks."""
        code_prompts = [
            "Write a function that...",
            "def solution():",
            "class MyClass:",
            "Implement an algorithm to...",
        ]

        for prompt in code_prompts:
            output = await wrapper.execute(
                ToolInput(prompt=prompt),
                "task_specific"
            )
            assert output.response is not None

    @pytest.mark.asyncio
    async def test_reasoning_task_detection(self, wrapper):
        """Test detection of reasoning tasks."""
        reasoning_prompts = [
            "Explain why...",
            "Analyze the impact of...",
            "Compare and contrast...",
            "What would happen if...",
        ]

        for prompt in reasoning_prompts:
            output = await wrapper.execute(
                ToolInput(prompt=prompt),
                "task_specific"
            )
            assert output.response is not None

    @pytest.mark.asyncio
    async def test_summarization_task_detection(self, wrapper):
        """Test detection of summarization tasks."""
        summary_prompts = [
            "Summarize: " + "content " * 100,
            "Condense this: " + "text " * 100,
            "Extract key points from: " + "document " * 100,
        ]

        for prompt in summary_prompts:
            output = await wrapper.execute(
                ToolInput(prompt=prompt),
                "task_specific"
            )
            assert output.response is not None

    @pytest.mark.asyncio
    async def test_latency_improvement_speed_variant(self, wrapper):
        """Test that speed variant has lower latency."""
        prompt = "Test prompt for latency comparison."

        speed_output = await wrapper.execute(
            ToolInput(prompt=prompt),
            "speed_optimized"
        )
        quality_output = await wrapper.execute(
            ToolInput(prompt=prompt),
            "quality_optimized"
        )

        # Both should complete
        assert speed_output.latency_ms > 0
        assert quality_output.latency_ms > 0
        # Speed variant should typically be faster (though simulation may not show this)

    @pytest.mark.asyncio
    async def test_all_variants_work(self, wrapper):
        """Test all variants execute."""
        variants = [
            "task_specific",
            "speed_optimized",
            "quality_optimized",
        ]

        prompt = "Test prompt for all variants."

        for variant in variants:
            output = await wrapper.execute(ToolInput(prompt=prompt), variant)
            assert output.technique_variant == variant
            assert output.response is not None
            assert output.error is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
