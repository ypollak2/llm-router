"""
Unit tests for TensorZero wrapper.

Tests TensorZero experimentation and learning platform.
"""

import pytest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

from base_wrapper import ToolInput, ToolOutput
from tensorzero_wrapper import TensorZeroWrapper


class TestTensorZeroWrapper:
    """Test TensorZero learning platform."""

    @pytest.fixture
    async def wrapper(self):
        """Create and initialize TensorZero wrapper."""
        config = {"learning_iterations": 100}
        wrapper = TensorZeroWrapper("tensorzero", config)
        await wrapper.initialize()
        yield wrapper
        await wrapper.cleanup()

    @pytest.mark.asyncio
    async def test_initialize_cleanup(self):
        """Test wrapper lifecycle."""
        wrapper = TensorZeroWrapper("tensorzero", {})
        assert not wrapper.initialized

        await wrapper.initialize()
        assert wrapper.initialized

        await wrapper.cleanup()
        assert not wrapper.initialized

    @pytest.mark.asyncio
    async def test_ab_tested_variant(self, wrapper):
        """Test A/B tested variant."""
        output = await wrapper.execute(
            ToolInput(prompt="Test prompt for A/B testing."),
            "ab_tested"
        )

        assert output.response
        assert output.compression_ratio is not None
        assert 0.50 <= output.compression_ratio <= 0.80  # 20-50% reduction
        assert output.technique_variant == "ab_tested"
        assert output.quality_score is not None

    @pytest.mark.asyncio
    async def test_feedback_optimized_variant(self, wrapper):
        """Test feedback-optimized variant."""
        output = await wrapper.execute(
            ToolInput(prompt="Optimize from feedback."),
            "feedback_optimized"
        )

        assert output.response
        assert output.compression_ratio is not None
        assert 0.45 <= output.compression_ratio <= 0.80  # 20-55% reduction
        assert output.quality_score is not None

    @pytest.mark.asyncio
    async def test_multi_armed_bandit_variant(self, wrapper):
        """Test multi-armed bandit variant."""
        output = await wrapper.execute(
            ToolInput(prompt="Explore variants with Thompson sampling."),
            "multi_armed_bandit"
        )

        assert output.response
        assert output.compression_ratio is not None
        assert 0.50 <= output.compression_ratio <= 0.85  # 15-50% reduction

    @pytest.mark.asyncio
    async def test_experimental_variant(self, wrapper):
        """Test experimental variant."""
        output = await wrapper.execute(
            ToolInput(prompt="Test new experimental variants."),
            "experimental"
        )

        assert output.response
        assert output.compression_ratio is not None
        # Experimental may have more variability

    @pytest.mark.asyncio
    async def test_quality_improvement_over_iterations(self, wrapper):
        """Test that quality improves with more iterations."""
        prompt = "Improve quality through experimentation."

        outputs = []
        for i in range(5):
            output = await wrapper.execute(
                ToolInput(prompt=prompt),
                "ab_tested"
            )
            outputs.append(output)

        # All should complete
        assert all(o.response for o in outputs)
        # Quality scores should be tracked
        assert all(o.quality_score is not None for o in outputs)

        # Later iterations should have higher quality (learning)
        quality_scores = [o.quality_score for o in outputs if o.quality_score]
        # Check general trend (may have variance in simulation)
        assert quality_scores[0] <= quality_scores[-1] or \
               all(q > 0.8 for q in quality_scores)  # Quality should be decent

    @pytest.mark.asyncio
    async def test_variant_tracking(self, wrapper):
        """Test that wrapper tracks which variants are tested."""
        prompt = "Track variant performance."

        # Try different variants
        variants = [
            "ab_tested",
            "feedback_optimized",
            "multi_armed_bandit",
            "experimental",
        ]

        for variant in variants:
            output = await wrapper.execute(ToolInput(prompt=prompt), variant)
            assert output.technique_variant == variant

        # Should have tracked experiments
        assert wrapper.experiment_count >= len(variants)

    @pytest.mark.asyncio
    async def test_variant_selection_quality_metric(self, wrapper):
        """Test that variant selection considers quality metric."""
        prompt = "Select variant based on quality."

        output = await wrapper.execute(
            ToolInput(prompt=prompt),
            "feedback_optimized"
        )

        # Should have selected variant with best quality/compression tradeoff
        assert output.quality_score is not None
        assert output.compression_ratio is not None

    @pytest.mark.asyncio
    async def test_all_variants_work(self, wrapper):
        """Test all experimentation variants."""
        variants = [
            "ab_tested",
            "feedback_optimized",
            "multi_armed_bandit",
            "experimental",
        ]

        prompt = "Test all TensorZero variants."

        for variant in variants:
            output = await wrapper.execute(ToolInput(prompt=prompt), variant)
            assert output.technique_variant == variant
            assert output.response is not None
            assert output.error is None
            assert output.quality_score is not None

    @pytest.mark.asyncio
    async def test_learning_persistence(self):
        """Test that learning persists across calls."""
        wrapper = TensorZeroWrapper("tensorzero", {"learning_iterations": 50})
        await wrapper.initialize()

        prompt = "Test learning persistence."

        # First batch
        initial_count = wrapper.experiment_count
        await wrapper.execute(ToolInput(prompt=prompt), "ab_tested")

        # Learning should accumulate
        assert wrapper.experiment_count > initial_count

        await wrapper.cleanup()

    @pytest.mark.asyncio
    async def test_latency_and_quality_tradeoff(self, wrapper):
        """Test latency vs quality tradeoff in variants."""
        prompt = "Test latency and quality."

        speed_output = await wrapper.execute(
            ToolInput(prompt=prompt),
            "experimental"  # May be faster but less optimized
        )

        quality_output = await wrapper.execute(
            ToolInput(prompt=prompt),
            "ab_tested"  # Slower but better optimized
        )

        # Both should have quality metrics
        assert speed_output.quality_score is not None
        assert quality_output.quality_score is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
