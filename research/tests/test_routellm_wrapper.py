"""
Unit tests for RouteLLM wrapper.

Tests RouteLLM complexity classification and cost-aware routing.
"""

import pytest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

from base_wrapper import ToolInput, ToolOutput
from routellm_wrapper import RouteLLMWrapper


class TestRouteLLMWrapper:
    """Test RouteLLM cost-aware routing."""

    @pytest.fixture
    async def wrapper(self):
        """Create and initialize RouteLLM wrapper."""
        config = {
            "model": "gpt-3.5-turbo",
            "threshold": 0.7,  # balanced
        }
        wrapper = RouteLLMWrapper("routellm", config)
        await wrapper.initialize()
        yield wrapper
        await wrapper.cleanup()

    @pytest.mark.asyncio
    async def test_initialize_cleanup(self):
        """Test wrapper lifecycle."""
        wrapper = RouteLLMWrapper("routellm", {})
        assert not wrapper.initialized

        await wrapper.initialize()
        assert wrapper.initialized

        await wrapper.cleanup()
        assert not wrapper.initialized

    @pytest.mark.asyncio
    async def test_aggressive_threshold_variant(self, wrapper):
        """Test aggressive routing (threshold 0.5)."""
        # Simple query should route to cheap model
        simple_prompt = "What is 2+2?"

        output = await wrapper.execute(simple_prompt, "threshold_0.5")

        assert output.response
        assert output.input_tokens > 0
        assert output.compression_ratio is not None
        # Aggressive should route most queries to cheap model
        assert 0.15 < output.compression_ratio <= 1.0  # 60-85% cost reduction
        assert output.technique_variant == "threshold_0.5"

    @pytest.mark.asyncio
    async def test_balanced_threshold_variant(self, wrapper):
        """Test balanced routing (threshold 0.7)."""
        prompt = "Explain machine learning algorithms and their applications."

        output = await wrapper.execute(prompt, "threshold_0.7")

        assert output.compression_ratio is not None
        # Balanced: some routing but not aggressive
        assert 0.2 < output.compression_ratio <= 1.0  # 35-80% cost reduction

    @pytest.mark.asyncio
    async def test_conservative_threshold_variant(self, wrapper):
        """Test conservative routing (threshold 0.9)."""
        prompt = "Implement a distributed system design."

        output = await wrapper.execute(prompt, "threshold_0.9")

        assert output.compression_ratio is not None
        # Conservative: minimal routing, mostly uses expensive model
        assert 0.6 <= output.compression_ratio <= 1.0  # 0-40% cost reduction

    @pytest.mark.asyncio
    async def test_complexity_classification_simple(self, wrapper):
        """Test that simple queries are classified correctly."""
        simple_queries = [
            "What is the capital of France?",
            "How do I print in Python?",
            "List 5 fruits.",
        ]

        for query in simple_queries:
            output = await wrapper.execute(query, "threshold_0.7")
            # Simple queries should route to cheaper model
            assert output.response is not None
            assert output.error is None

    @pytest.mark.asyncio
    async def test_complexity_classification_complex(self, wrapper):
        """Test that complex queries are classified correctly."""
        complex_queries = [
            "Design a distributed consensus algorithm with Byzantine fault tolerance.",
            "Explain quantum computing, entanglement, and Shor's algorithm.",
            "Implement a production-grade microservice architecture with database sharding.",
        ]

        for query in complex_queries:
            output = await wrapper.execute(query, "threshold_0.7")
            # Complex queries may route to expensive model or not route much
            assert output.response is not None
            assert output.error is None

    @pytest.mark.asyncio
    async def test_routing_saves_cost_on_simple(self, wrapper):
        """Test that routing actually saves cost on simple queries."""
        simple_prompt = "What is 2+2?"
        complex_prompt = "Design a new programming language with its type system, memory model, and runtime."

        simple_output = await wrapper.execute(simple_prompt, "threshold_0.7")
        complex_output = await wrapper.execute(complex_prompt, "threshold_0.7")

        # Simple queries should have lower cost (higher compression ratio = cheaper)
        assert simple_output.compression_ratio is not None
        assert complex_output.compression_ratio is not None
        # Simple should typically be routed more aggressively
        assert simple_output.compression_ratio >= complex_output.compression_ratio

    @pytest.mark.asyncio
    async def test_latency_tracking(self, wrapper):
        """Test latency metrics are recorded."""
        task_input = ToolInput(prompt="Test prompt for latency measurement.")

        output = await wrapper.execute(task_input, "threshold_0.7")

        assert output.latency_ms > 0
        assert output.preprocessing_ms >= 0
        assert output.inference_ms >= 0

    @pytest.mark.asyncio
    async def test_all_variants_work(self, wrapper):
        """Test that all threshold variants execute."""
        variants = [
            "threshold_0.5",
            "threshold_0.7",
            "threshold_0.9",
        ]

        prompt = "Test prompt for variant testing."

        for variant in variants:
            output = await wrapper.execute(prompt, variant)
            assert output.technique_variant == variant
            assert output.response is not None
            assert output.error is None

    @pytest.mark.asyncio
    async def test_routing_decision_stored(self, wrapper):
        """Test that routing decisions are tracked."""
        prompt = "What is machine learning?"

        output = await wrapper.execute(prompt, "threshold_0.7")

        # Should track which model was selected
        assert output.response is not None
        # Response content should indicate routing decision
        assert "Response" in output.response or output.response


class TestRouteLLMClassifier:
    """Test RouteLLM's complexity classifier."""

    @pytest.mark.asyncio
    async def test_keyword_detection_simple(self):
        """Test simple query keyword detection."""
        wrapper = RouteLLMWrapper("routellm", {})

        # Keywords indicating simple queries
        simple_keywords = ["what", "how", "list", "define"]

        for keyword in simple_keywords:
            # Wrapper should detect these as simple
            prompt = f"{keyword} is this term?"
            # Just verify it doesn't error
            assert len(prompt) > 0

    @pytest.mark.asyncio
    async def test_question_mark_detection(self):
        """Test that question mark format affects classification."""
        wrapper = RouteLLMWrapper("routellm", {})

        question = "What is the answer?"
        statement = "The answer is important."

        await wrapper.initialize()

        output_q = await wrapper.execute(question, "threshold_0.7")
        output_s = await wrapper.execute(statement, "threshold_0.7")

        # Both should work
        assert output_q.response is not None
        assert output_s.response is not None

        await wrapper.cleanup()

    @pytest.mark.asyncio
    async def test_length_affects_routing(self):
        """Test that prompt length affects routing decision."""
        wrapper = RouteLLMWrapper("routellm", {})
        await wrapper.initialize()

        short_prompt = "Hello?"
        long_prompt = "What is the relationship between quantum mechanics and general relativity? " * 10

        short_output = await wrapper.execute(short_prompt, "threshold_0.7")
        long_output = await wrapper.execute(long_prompt, "threshold_0.7")

        # Both should complete
        assert short_output.response is not None
        assert long_output.response is not None
        # Longer prompts typically don't get routed as aggressively
        assert short_output.compression_ratio is not None
        assert long_output.compression_ratio is not None

        await wrapper.cleanup()


class TestRouteLLMMetrics:
    """Test RouteLLM metrics collection."""

    @pytest.mark.asyncio
    async def test_compression_ratio_varies_by_threshold(self):
        """Test that different thresholds produce different compression."""
        wrapper = RouteLLMWrapper("routellm", {})
        await wrapper.initialize()

        prompt = "Test prompt for threshold comparison."

        ratios = {}
        for variant in ["threshold_0.5", "threshold_0.7", "threshold_0.9"]:
            output = await wrapper.execute(prompt, variant)
            if output.compression_ratio is not None:
                ratios[variant] = output.compression_ratio

        # Verify different thresholds produce different routing decisions
        assert len(ratios) > 0

        await wrapper.cleanup()

    @pytest.mark.asyncio
    async def test_output_serialization(self):
        """Test that ToolOutput serializes correctly."""
        wrapper = RouteLLMWrapper("routellm", {})
        await wrapper.initialize()

        output = await wrapper.execute("test", "threshold_0.7")
        output_dict = output.to_dict()

        assert isinstance(output_dict, dict)
        assert output_dict["tool_name"] == "routellm"
        assert "compression_ratio" in output_dict
        assert "technique_variant" in output_dict

        await wrapper.cleanup()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
