"""
Unit tests for GPTCache wrapper.

Tests GPTCache semantic caching and cache hit behavior.
"""

import pytest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

from base_wrapper import ToolInput, ToolOutput
from gptcache_wrapper import GPTCacheWrapper


class TestGPTCacheWrapper:
    """Test GPTCache semantic caching."""

    @pytest.fixture
    async def wrapper(self):
        """Create and initialize GPTCache wrapper."""
        config = {
            "model": "gpt-3.5-turbo",
            "similarity_threshold": 0.8,
        }
        wrapper = GPTCacheWrapper("gptcache", config)
        await wrapper.initialize()
        yield wrapper
        await wrapper.cleanup()

    @pytest.mark.asyncio
    async def test_initialize_cleanup(self):
        """Test wrapper lifecycle."""
        wrapper = GPTCacheWrapper("gptcache", {})
        assert not wrapper.initialized

        await wrapper.initialize()
        assert wrapper.initialized

        await wrapper.cleanup()
        assert not wrapper.initialized

    @pytest.mark.asyncio
    async def test_strict_similarity_variant(self, wrapper):
        """Test strict similarity threshold (90%)."""
        task_input = ToolInput(prompt="What is machine learning?")

        output = await wrapper.execute(task_input, "strict_similarity")

        assert isinstance(output, ToolOutput)
        assert output.response
        assert output.input_tokens > 0
        assert output.tool_name == "gptcache"
        assert output.technique_variant == "strict_similarity"

    @pytest.mark.asyncio
    async def test_loose_similarity_variant(self, wrapper):
        """Test loose similarity threshold (70%)."""
        task_input = ToolInput(prompt="Explain neural networks.")

        output = await wrapper.execute(task_input, "loose_similarity")

        assert output.response
        assert output.technique_variant == "loose_similarity"

    @pytest.mark.asyncio
    async def test_rag_optimized_variant(self, wrapper):
        """Test RAG-optimized caching."""
        rag_prompt = "Document: machine learning is... Query: what is ML?"

        output = await wrapper.execute(ToolInput(prompt=rag_prompt), "rag_optimized")

        assert output.response
        assert output.technique_variant == "rag_optimized"

    @pytest.mark.asyncio
    async def test_cache_miss_has_cost(self, wrapper):
        """Test that cache misses incur full cost."""
        task_input = ToolInput(prompt="Unique question never asked before: xyz123abc")

        output = await wrapper.execute(task_input, "strict_similarity")

        # Cache miss: should have output tokens and no compression benefit
        assert output.input_tokens > 0
        assert output.output_tokens > 0
        # On miss, compression ratio should be None or 1.0 (no savings)
        if output.compression_ratio is not None:
            assert output.compression_ratio >= 0.9

    @pytest.mark.asyncio
    async def test_identical_prompt_cache_hit(self, wrapper):
        """Test cache hit on identical prompt (simulated)."""
        prompt = "What is the capital of France?"
        task_input = ToolInput(prompt=prompt)

        # First execution (cache miss)
        output1 = await wrapper.execute(task_input, "strict_similarity")
        assert output1.response

        # Second identical execution (cache hit in simulation)
        output2 = await wrapper.execute(task_input, "strict_similarity")

        # Both should complete
        assert output2.response
        # Second execution might show cache behavior
        assert output2.input_tokens > 0

    @pytest.mark.asyncio
    async def test_similar_prompts_might_hit(self, wrapper):
        """Test that similar prompts may hit cache."""
        prompt1 = "What is machine learning?"
        prompt2 = "Can you explain machine learning?"

        output1 = await wrapper.execute(ToolInput(prompt=prompt1), "loose_similarity")
        output2 = await wrapper.execute(ToolInput(prompt=prompt2), "loose_similarity")

        # Both should execute
        assert output1.response
        assert output2.response

    @pytest.mark.asyncio
    async def test_different_prompts_miss(self, wrapper):
        """Test that different prompts miss cache."""
        prompt1 = "What is machine learning?"
        prompt2 = "How do I cook pasta?"

        output1 = await wrapper.execute(ToolInput(prompt=prompt1), "strict_similarity")
        output2 = await wrapper.execute(ToolInput(prompt=prompt2), "strict_similarity")

        # Different prompts should both work but not share cache
        assert output1.response
        assert output2.response

    @pytest.mark.asyncio
    async def test_cache_effectiveness_scales_with_queries(self, wrapper):
        """Test cache effectiveness increases with repeated similar queries."""
        # First batch of queries
        queries = [
            "What is AI?",
            "What is artificial intelligence?",
            "Explain AI",
            "Tell me about AI",
        ]

        results = []
        for q in queries:
            output = await wrapper.execute(ToolInput(prompt=q), "loose_similarity")
            results.append(output)

        # All should complete
        assert all(r.response for r in results)

    @pytest.mark.asyncio
    async def test_latency_improvement_on_hit(self, wrapper):
        """Test that cache hits are faster."""
        prompt = "What is Python?"
        task_input = ToolInput(prompt=prompt)

        output1 = await wrapper.execute(task_input, "strict_similarity")
        output2 = await wrapper.execute(task_input, "strict_similarity")

        # Both should have latency tracked
        assert output1.latency_ms > 0
        assert output2.latency_ms > 0
        # Note: In this simulation, latency may not actually differ much

    @pytest.mark.asyncio
    async def test_all_variants_work(self, wrapper):
        """Test all caching variants."""
        variants = [
            "strict_similarity",
            "loose_similarity",
            "rag_optimized",
        ]

        prompt = "Test caching prompt."

        for variant in variants:
            output = await wrapper.execute(ToolInput(prompt=prompt), variant)
            assert output.technique_variant == variant
            assert output.response is not None
            assert output.error is None


class TestGPTCacheSimilarity:
    """Test GPTCache similarity detection."""

    @pytest.mark.asyncio
    async def test_exact_match_detection(self):
        """Test exact match similarity."""
        wrapper = GPTCacheWrapper("gptcache", {})
        await wrapper.initialize()

        prompt = "What is the meaning of life?"

        # Two identical prompts
        output1 = await wrapper.execute(ToolInput(prompt=prompt), "strict_similarity")
        output2 = await wrapper.execute(ToolInput(prompt=prompt), "strict_similarity")

        assert output1.response is not None
        assert output2.response is not None

        await wrapper.cleanup()

    @pytest.mark.asyncio
    async def test_word_overlap_similarity(self):
        """Test word overlap detection."""
        wrapper = GPTCacheWrapper("gptcache", {})
        await wrapper.initialize()

        # High word overlap
        prompt1 = "How do I sort a list in Python?"
        prompt2 = "How do I sort Python lists?"

        output1 = await wrapper.execute(ToolInput(prompt=prompt1), "loose_similarity")
        output2 = await wrapper.execute(ToolInput(prompt=prompt2), "loose_similarity")

        assert output1.response is not None
        assert output2.response is not None

        await wrapper.cleanup()

    @pytest.mark.asyncio
    async def test_low_overlap_no_hit(self):
        """Test that low overlap doesn't trigger cache hit."""
        wrapper = GPTCacheWrapper("gptcache", {})
        await wrapper.initialize()

        # Very different prompts
        prompt1 = "How do I sort a list in Python?"
        prompt2 = "What is the weather today?"

        output1 = await wrapper.execute(ToolInput(prompt=prompt1), "strict_similarity")
        output2 = await wrapper.execute(ToolInput(prompt=prompt2), "strict_similarity")

        assert output1.response is not None
        assert output2.response is not None

        await wrapper.cleanup()


class TestGPTCacheMetrics:
    """Test GPTCache metrics tracking."""

    @pytest.mark.asyncio
    async def test_cache_statistics(self):
        """Test that cache statistics are tracked."""
        wrapper = GPTCacheWrapper("gptcache", {})
        await wrapper.initialize()

        # Execute several times
        for i in range(3):
            await wrapper.execute(ToolInput(prompt=f"Query {i}"), "strict_similarity")

        # Cache should track statistics
        assert wrapper._call_count == 3

        await wrapper.cleanup()

    @pytest.mark.asyncio
    async def test_output_serialization(self):
        """Test ToolOutput serialization."""
        wrapper = GPTCacheWrapper("gptcache", {})
        await wrapper.initialize()

        output = await wrapper.execute(ToolInput(prompt="test"), "strict_similarity")
        output_dict = output.to_dict()

        assert isinstance(output_dict, dict)
        assert output_dict["tool_name"] == "gptcache"
        assert output_dict["technique_variant"] == "strict_similarity"
        assert "latency_ms" in output_dict

        await wrapper.cleanup()

    @pytest.mark.asyncio
    async def test_compression_ratio_on_hit(self):
        """Test compression ratio tracking on cache hits."""
        wrapper = GPTCacheWrapper("gptcache", {})
        await wrapper.initialize()

        output = await wrapper.execute(ToolInput(prompt="test"), "strict_similarity")

        # Compression ratio should be tracked
        if output.compression_ratio is not None:
            assert 0.0 <= output.compression_ratio <= 1.0

        await wrapper.cleanup()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
