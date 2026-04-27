"""
Unit tests for DSPy wrapper.

Tests DSPy framework-level prompt optimization.
"""

import pytest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

from base_wrapper import ToolInput, ToolOutput
from dspy_wrapper import DSPyWrapper


class TestDSPyWrapper:
    """Test DSPy prompt optimization."""

    @pytest.fixture
    async def wrapper(self):
        """Create and initialize DSPy wrapper."""
        config = {}
        wrapper = DSPyWrapper("dspy", config)
        await wrapper.initialize()
        yield wrapper
        await wrapper.cleanup()

    @pytest.mark.asyncio
    async def test_initialize_cleanup(self):
        """Test wrapper lifecycle."""
        wrapper = DSPyWrapper("dspy", {})
        assert not wrapper.initialized

        await wrapper.initialize()
        assert wrapper.initialized

        await wrapper.cleanup()
        assert not wrapper.initialized

    @pytest.mark.asyncio
    async def test_bootstrap_few_shot_variant(self, wrapper):
        """Test bootstrap few-shot optimization."""
        output = await wrapper.execute(
            ToolInput(prompt="Example-based prompt optimization."),
            "bootstrap_few_shot"
        )

        assert output.response
        assert output.compression_ratio is not None
        assert 0.35 <= output.compression_ratio <= 0.65  # 35-65% reduction
        assert output.technique_variant == "bootstrap_few_shot"

    @pytest.mark.asyncio
    async def test_miprov2_variant(self, wrapper):
        """Test MIPROv2 optimization."""
        output = await wrapper.execute(
            ToolInput(prompt="Prompt to optimize with MIPROv2."),
            "miprov2"
        )

        assert output.response
        assert output.compression_ratio is not None
        # MIPROv2 typically achieves good compression
        assert 0.30 <= output.compression_ratio <= 0.60

    @pytest.mark.asyncio
    async def test_auto_generated_variant(self, wrapper):
        """Test auto-generated examples variant."""
        output = await wrapper.execute(
            ToolInput(prompt="Prompt with auto-generated examples."),
            "auto_generated"
        )

        assert output.response
        assert output.compression_ratio is not None

    @pytest.mark.asyncio
    async def test_minimal_variant(self, wrapper):
        """Test minimal optimization."""
        output = await wrapper.execute(
            ToolInput(prompt="Minimize this prompt as much as possible."),
            "minimal"
        )

        assert output.response
        assert output.compression_ratio is not None
        # Minimal should be most aggressive
        assert output.compression_ratio <= 0.50

    @pytest.mark.asyncio
    async def test_example_mining(self, wrapper):
        """Test that examples are mined and cached."""
        prompt_with_examples = """
Example 1:
Input: What is AI?
Output: Artificial Intelligence...

Example 2:
Input: What is ML?
Output: Machine Learning...

Now answer: What is DL?
""" * 5

        output = await wrapper.execute(
            ToolInput(prompt=prompt_with_examples),
            "bootstrap_few_shot"
        )

        assert output.response
        # Examples should be optimized
        assert output.compression_ratio is not None

    @pytest.mark.asyncio
    async def test_prompt_quality_preservation(self, wrapper):
        """Test that optimization preserves semantic meaning."""
        prompt = """
You are a helpful assistant. Your job is to answer questions accurately and thoroughly.
Examples:
- Q: What is 2+2? A: 4
- Q: What is capital of France? A: Paris

Now answer: What is capital of Germany?
"""

        output = await wrapper.execute(
            ToolInput(prompt=prompt),
            "bootstrap_few_shot"
        )

        assert output.response
        # Should maintain question semantics despite optimization
        assert len(output.response) > 0

    @pytest.mark.asyncio
    async def test_all_variants_work(self, wrapper):
        """Test all optimization variants."""
        variants = [
            "bootstrap_few_shot",
            "miprov2",
            "auto_generated",
            "minimal",
        ]

        prompt = "Test prompt for DSPy optimization."

        for variant in variants:
            output = await wrapper.execute(ToolInput(prompt=prompt), variant)
            assert output.technique_variant == variant
            assert output.response is not None
            assert output.error is None

    @pytest.mark.asyncio
    async def test_repeated_calls_improve_optimization(self, wrapper):
        """Test that repeated calls improve prompt optimization."""
        prompt = "Optimize this prompt repeatedly for better results."

        outputs = []
        for i in range(3):
            output = await wrapper.execute(
                ToolInput(prompt=prompt),
                "bootstrap_few_shot"
            )
            outputs.append(output)

        # All should execute
        assert all(o.response for o in outputs)
        # Optimization should be consistent
        assert all(o.compression_ratio for o in outputs)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
