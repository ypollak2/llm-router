"""
Unit tests for LiteLLM wrapper.

Tests LiteLLM multi-provider routing and fallback chains.
"""

import pytest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

from base_wrapper import ToolInput, ToolOutput
from litellm_wrapper import LiteLLMWrapper


class TestLiteLLMWrapper:
    """Test LiteLLM multi-provider routing."""

    @pytest.fixture
    async def wrapper(self):
        """Create and initialize LiteLLM wrapper."""
        config = {"default_provider": "openai"}
        wrapper = LiteLLMWrapper("litellm", config)
        await wrapper.initialize()
        yield wrapper
        await wrapper.cleanup()

    @pytest.mark.asyncio
    async def test_initialize_cleanup(self):
        """Test wrapper lifecycle."""
        wrapper = LiteLLMWrapper("litellm", {})
        assert not wrapper.initialized

        await wrapper.initialize()
        assert wrapper.initialized

        await wrapper.cleanup()
        assert not wrapper.initialized

    @pytest.mark.asyncio
    async def test_cost_optimized_variant(self, wrapper):
        """Test cost-optimized routing."""
        output = await wrapper.execute(
            ToolInput(prompt="Simple query"),
            "cost_optimized"
        )

        assert output.response
        assert output.compression_ratio is not None
        # Cost-optimized should route to cheap models
        assert 0.10 <= output.compression_ratio <= 1.0  # 0-90% savings
        assert output.technique_variant == "cost_optimized"

    @pytest.mark.asyncio
    async def test_latency_optimized_variant(self, wrapper):
        """Test latency-optimized routing."""
        output = await wrapper.execute(
            ToolInput(prompt="Test query"),
            "latency_optimized"
        )

        assert output.response
        assert output.latency_ms > 0
        # Latency optimized may use local/faster models
        assert output.technique_variant == "latency_optimized"

    @pytest.mark.asyncio
    async def test_quality_optimized_variant(self, wrapper):
        """Test quality-optimized routing."""
        output = await wrapper.execute(
            ToolInput(prompt="Complex reasoning task"),
            "quality_optimized"
        )

        assert output.response
        # Quality optimized uses better models, less compression
        assert 0.8 <= output.compression_ratio <= 1.0
        assert output.technique_variant == "quality_optimized"

    @pytest.mark.asyncio
    async def test_provider_abstraction(self, wrapper):
        """Test that LiteLLM abstracts multiple providers."""
        # Should work regardless of underlying provider
        output = await wrapper.execute(
            ToolInput(prompt="test"),
            "cost_optimized"
        )

        assert output.response is not None

    @pytest.mark.asyncio
    async def test_fallback_chain_execution(self, wrapper):
        """Test that fallback chain works."""
        output = await wrapper.execute(
            ToolInput(prompt="test query"),
            "cost_optimized"
        )

        # Should successfully execute (fallback chain handles provider issues)
        assert output.response is not None

    @pytest.mark.asyncio
    async def test_all_variants_work(self, wrapper):
        """Test all variants execute successfully."""
        variants = [
            "cost_optimized",
            "latency_optimized",
            "quality_optimized",
        ]

        for variant in variants:
            output = await wrapper.execute(
                ToolInput(prompt="test"),
                variant
            )
            assert output.technique_variant == variant
            assert output.response is not None
            assert output.error is None

    @pytest.mark.asyncio
    async def test_output_serialization(self):
        """Test output serialization."""
        wrapper = LiteLLMWrapper("litellm", {})
        await wrapper.initialize()

        output = await wrapper.execute(
            ToolInput(prompt="test"),
            "cost_optimized"
        )
        output_dict = output.to_dict()

        assert output_dict["tool_name"] == "litellm"
        assert "compression_ratio" in output_dict

        await wrapper.cleanup()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
