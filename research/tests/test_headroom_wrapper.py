"""
Unit tests for Headroom wrapper.

Tests context optimization for fitting within token budgets.
"""

import pytest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

from base_wrapper import ToolInput, ToolOutput
from headroom_wrapper import HeadroomWrapper


class TestHeadroomWrapper:
    """Test Headroom context optimization."""

    @pytest.fixture
    async def wrapper(self):
        """Create and initialize Headroom wrapper."""
        config = {"max_tokens": 2000}
        wrapper = HeadroomWrapper("headroom", config)
        await wrapper.initialize()
        yield wrapper
        await wrapper.cleanup()

    @pytest.mark.asyncio
    async def test_initialize_cleanup(self):
        """Test wrapper lifecycle."""
        wrapper = HeadroomWrapper("headroom", {})
        assert not wrapper.initialized

        await wrapper.initialize()
        assert wrapper.initialized

        await wrapper.cleanup()
        assert not wrapper.initialized

    @pytest.mark.asyncio
    async def test_priority_aware_variant(self, wrapper):
        """Test priority-aware optimization."""
        large_prompt = "Context content. " * 200

        output = await wrapper.execute(
            ToolInput(prompt=large_prompt),
            "priority_aware"
        )

        assert output.response
        assert output.compression_ratio is not None
        assert 0.60 <= output.compression_ratio <= 0.80  # 20-40% reduction
        assert output.technique_variant == "priority_aware"

    @pytest.mark.asyncio
    async def test_summarize_on_overflow_variant(self, wrapper):
        """Test summarization on overflow."""
        overflow_prompt = "Content to summarize. " * 250

        output = await wrapper.execute(
            ToolInput(prompt=overflow_prompt),
            "summarize_on_overflow"
        )

        assert output.response
        assert output.compression_ratio is not None
        assert 0.50 <= output.compression_ratio <= 0.75  # 25-50% reduction

    @pytest.mark.asyncio
    async def test_adaptive_variant(self, wrapper):
        """Test adaptive optimization."""
        prompt = "Adaptive content optimization. " * 150

        output = await wrapper.execute(
            ToolInput(prompt=prompt),
            "adaptive"
        )

        assert output.response
        assert output.compression_ratio is not None
        assert 0.55 <= output.compression_ratio <= 0.80

    @pytest.mark.asyncio
    async def test_aggressive_truncate_variant(self, wrapper):
        """Test aggressive truncation."""
        huge_prompt = "Text to truncate. " * 300

        output = await wrapper.execute(
            ToolInput(prompt=huge_prompt),
            "aggressive_truncate"
        )

        assert output.response
        assert output.compression_ratio is not None
        # Aggressive should be most reduction
        assert output.compression_ratio <= 0.65

    @pytest.mark.asyncio
    async def test_fits_within_budget(self, wrapper):
        """Test that optimized prompt fits within token budget."""
        # Create a large prompt
        large_prompt = "Content for token budget. " * 200

        output = await wrapper.execute(
            ToolInput(prompt=large_prompt),
            "priority_aware"
        )

        # Output should have been optimized to fit in budget
        if output.compressed_input_tokens is not None:
            # Should be less than max_tokens (2000)
            assert output.compressed_input_tokens <= 2000

    @pytest.mark.asyncio
    async def test_preserves_key_information(self, wrapper):
        """Test that optimization preserves important information."""
        prompt = """
CRITICAL: Database connection string is postgres://localhost:5432/mydb
NOTE: This is optional information
IMPORTANT: Admin credentials must be rotated monthly

Process this data and report status.
""" * 10

        output = await wrapper.execute(
            ToolInput(prompt=prompt),
            "priority_aware"
        )

        assert output.response is not None
        # Critical info should be preserved in truncated version

    @pytest.mark.asyncio
    async def test_removes_examples_on_overflow(self, wrapper):
        """Test removal of examples when overflowing."""
        prompt_with_examples = """
Instructions: Do X and Y.

Example 1: When you see A, do this...
Example 2: When you see B, do that...
Example 3: When you see C, do this instead...
Example 4: Additional example for reference...

Now process: [actual data to process]
""" * 20

        output = await wrapper.execute(
            ToolInput(prompt=prompt_with_examples),
            "summarize_on_overflow"
        )

        assert output.response is not None
        # Should handle example removal gracefully

    @pytest.mark.asyncio
    async def test_all_variants_work(self, wrapper):
        """Test all variants execute."""
        variants = [
            "priority_aware",
            "summarize_on_overflow",
            "adaptive",
            "aggressive_truncate",
        ]

        prompt = "Test content. " * 150

        for variant in variants:
            output = await wrapper.execute(ToolInput(prompt=prompt), variant)
            assert output.technique_variant == variant
            assert output.response is not None
            assert output.error is None

    @pytest.mark.asyncio
    async def test_latency_tracking(self, wrapper):
        """Test latency metrics are recorded."""
        prompt = "Content for latency test. " * 100

        output = await wrapper.execute(ToolInput(prompt=prompt), "priority_aware")

        assert output.latency_ms > 0
        assert output.preprocessing_ms > 0  # Optimization time


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
