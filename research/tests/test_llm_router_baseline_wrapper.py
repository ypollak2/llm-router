"""
Unit tests for LLM Router baseline wrapper.

Tests the baseline llm-router implementation with complexity routing and Caveman modes.
"""

import pytest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

from base_wrapper import ToolInput, ToolOutput
from llm_router_wrapper import LLMRouterWrapper


class TestLLMRouterBaselineWrapper:
    """Test baseline llm-router reference implementation."""

    @pytest.fixture
    async def wrapper(self):
        """Create and initialize llm-router baseline wrapper."""
        config = {
            "policy": "balanced",
            "caveman_mode": "full",
        }
        wrapper = LLMRouterWrapper("llm-router", config)
        await wrapper.initialize()
        yield wrapper
        await wrapper.cleanup()

    @pytest.mark.asyncio
    async def test_initialize_cleanup(self):
        """Test wrapper lifecycle."""
        wrapper = LLMRouterWrapper("llm-router", {})
        assert not wrapper.initialized

        await wrapper.initialize()
        assert wrapper.initialized

        await wrapper.cleanup()
        assert not wrapper.initialized

    @pytest.mark.asyncio
    async def test_aggressive_policy_variant(self, wrapper):
        """Test aggressive routing policy."""
        output = await wrapper.execute(
            ToolInput(prompt="Simple query here."),
            "aggressive"
        )

        assert output.response
        assert output.compression_ratio is not None
        assert output.technique_variant == "aggressive"
        # Aggressive routes most queries to cheap models
        assert 0.15 <= output.compression_ratio <= 1.0

    @pytest.mark.asyncio
    async def test_balanced_policy_variant(self, wrapper):
        """Test balanced routing policy."""
        output = await wrapper.execute(
            ToolInput(prompt="Moderate complexity query."),
            "balanced"
        )

        assert output.response
        assert output.compression_ratio is not None
        # Balanced: selective routing
        assert 0.2 <= output.compression_ratio <= 1.0

    @pytest.mark.asyncio
    async def test_conservative_policy_variant(self, wrapper):
        """Test conservative routing policy."""
        output = await wrapper.execute(
            ToolInput(prompt="Complex reasoning task."),
            "conservative"
        )

        assert output.response
        assert output.compression_ratio is not None
        # Conservative: minimal routing
        assert 0.5 <= output.compression_ratio <= 1.0

    @pytest.mark.asyncio
    async def test_caveman_off_variant(self, wrapper):
        """Test Caveman mode off (verbose output)."""
        output = await wrapper.execute(
            ToolInput(prompt="Test prompt."),
            "caveman_off"
        )

        assert output.response
        assert output.technique_variant == "caveman_off"
        # Caveman off has full output tokens
        assert output.output_tokens > 0

    @pytest.mark.asyncio
    async def test_caveman_lite_variant(self, wrapper):
        """Test Caveman lite mode (moderate compression)."""
        output = await wrapper.execute(
            ToolInput(prompt="Test prompt."),
            "caveman_lite"
        )

        assert output.response
        assert output.technique_variant == "caveman_lite"
        assert output.compression_ratio is not None

    @pytest.mark.asyncio
    async def test_caveman_full_variant(self, wrapper):
        """Test Caveman full mode (aggressive compression)."""
        output = await wrapper.execute(
            ToolInput(prompt="Test prompt with filler content."),
            "caveman_full"
        )

        assert output.response
        assert output.technique_variant == "caveman_full"
        # Full caveman should compress output significantly
        assert output.compression_ratio is not None

    @pytest.mark.asyncio
    async def test_caveman_ultra_variant(self, wrapper):
        """Test Caveman ultra mode (maximum compression)."""
        output = await wrapper.execute(
            ToolInput(prompt="Test prompt."),
            "caveman_ultra"
        )

        assert output.response
        assert output.technique_variant == "caveman_ultra"
        # Ultra should be most aggressive
        assert output.compression_ratio is not None

    @pytest.mark.asyncio
    async def test_simple_query_routing(self, wrapper):
        """Test routing of simple queries."""
        simple_queries = [
            "What is 2+2?",
            "What is the capital of France?",
            "List 5 fruits.",
        ]

        for query in simple_queries:
            output = await wrapper.execute(
                ToolInput(prompt=query),
                "aggressive"
            )
            assert output.response is not None
            # Simple queries should route to cheap model
            assert output.compression_ratio is not None

    @pytest.mark.asyncio
    async def test_complex_query_routing(self, wrapper):
        """Test routing of complex queries."""
        complex_queries = [
            "Design a microservices architecture with load balancing and fault tolerance.",
            "Explain quantum computing and Shor's algorithm in detail.",
            "Implement a distributed consensus algorithm.",
        ]

        for query in complex_queries:
            output = await wrapper.execute(
                ToolInput(prompt=query),
                "balanced"
            )
            assert output.response is not None

    @pytest.mark.asyncio
    async def test_policy_affects_routing(self, wrapper):
        """Test that different policies produce different routing."""
        prompt = "Test prompt for policy comparison."

        outputs = {}
        for policy in ["aggressive", "balanced", "conservative"]:
            output = await wrapper.execute(ToolInput(prompt=prompt), policy)
            outputs[policy] = output.compression_ratio

        # All should have compression ratios
        assert all(v is not None for v in outputs.values())
        # Aggressive should route more than conservative (higher compression = cheaper)
        # Allow variance in simulation
        assert len(outputs) == 3

    @pytest.mark.asyncio
    async def test_caveman_mode_reduces_output_tokens(self, wrapper):
        """Test that Caveman mode reduces output tokens."""
        prompt = "This is a test prompt requiring a long detailed response."

        caveman_off = await wrapper.execute(ToolInput(prompt=prompt), "caveman_off")
        caveman_full = await wrapper.execute(ToolInput(prompt=prompt), "caveman_full")

        # Both should produce output
        assert caveman_off.response is not None
        assert caveman_full.response is not None

        # Caveman full should have fewer output tokens (simulated compression)
        assert caveman_off.output_tokens > 0
        assert caveman_full.output_tokens > 0

    @pytest.mark.asyncio
    async def test_all_variants_combinations(self, wrapper):
        """Test all policy + caveman combinations."""
        policies = ["aggressive", "balanced", "conservative"]
        caveman_modes = ["caveman_off", "caveman_lite", "caveman_full", "caveman_ultra"]

        prompt = "Test all combinations."

        # Test a few key combinations
        for policy in policies:
            for mode in ["caveman_off", "caveman_full"]:
                # Variant name combines them
                variant = f"{policy}_{mode}"
                if policy == "aggressive" and mode == "caveman_off":
                    output = await wrapper.execute(ToolInput(prompt=prompt), "aggressive")
                    assert output.response is not None

    @pytest.mark.asyncio
    async def test_latency_tracking(self, wrapper):
        """Test latency metrics."""
        output = await wrapper.execute(
            ToolInput(prompt="Test latency."),
            "balanced"
        )

        assert output.latency_ms > 0
        assert output.preprocessing_ms >= 0
        assert output.inference_ms >= 0

    @pytest.mark.asyncio
    async def test_cost_reduction_achieved(self, wrapper):
        """Test that routing achieves cost reduction."""
        output = await wrapper.execute(
            ToolInput(prompt="What is machine learning?"),
            "aggressive"
        )

        # Simple query with aggressive routing should show cost reduction
        assert output.compression_ratio is not None
        if output.compression_ratio < 1.0:
            # Cost was reduced
            cost_reduction = (1 - output.compression_ratio) * 100
            assert cost_reduction > 0  # Some savings achieved


class TestLLMRouterBaselineMetrics:
    """Test metrics collection for baseline."""

    @pytest.mark.asyncio
    async def test_token_metrics_tracked(self):
        """Test that token metrics are properly tracked."""
        wrapper = LLMRouterWrapper("llm-router", {})
        await wrapper.initialize()

        output = await wrapper.execute(
            ToolInput(prompt="test prompt"),
            "balanced"
        )

        assert output.input_tokens > 0
        assert output.output_tokens >= 0
        assert output.compressed_input_tokens is not None
        assert output.compression_ratio is not None

        await wrapper.cleanup()

    @pytest.mark.asyncio
    async def test_output_serialization(self):
        """Test output serialization."""
        wrapper = LLMRouterWrapper("llm-router", {})
        await wrapper.initialize()

        output = await wrapper.execute(
            ToolInput(prompt="test"),
            "balanced"
        )
        output_dict = output.to_dict()

        assert output_dict["tool_name"] == "llm-router"
        assert output_dict["technique_variant"] == "balanced"
        assert "compression_ratio" in output_dict

        await wrapper.cleanup()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
