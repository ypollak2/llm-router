"""
Unit tests for LLMLingua wrapper.

Tests LLMLingua compression variants and technique behavior.
"""

import pytest
import asyncio
from pathlib import Path

# Import dependencies
import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

from base_wrapper import ToolInput, ToolOutput
from llmlingua_wrapper import LLMLinguaWrapper


class TestLLMLinguaWrapper:
    """Test LLMLingua compression wrapper."""

    @pytest.fixture
    async def wrapper(self):
        """Create and initialize LLMLingua wrapper."""
        config = {
            "model": "gpt-3.5-turbo",
            "compress_ratio": 0.5,
        }
        wrapper = LLMLinguaWrapper("llmlingua", config)
        await wrapper.initialize()
        yield wrapper
        await wrapper.cleanup()

    @pytest.mark.asyncio
    async def test_initialize_cleanup(self):
        """Test wrapper lifecycle."""
        wrapper = LLMLinguaWrapper("llmlingua", {})
        assert not wrapper.initialized

        await wrapper.initialize()
        assert wrapper.initialized

        await wrapper.cleanup()
        assert not wrapper.initialized

    @pytest.mark.asyncio
    async def test_llmlingua_20x_variant(self, wrapper):
        """Test 20x compression variant (most aggressive)."""
        task_input = ToolInput(
            prompt="This is a long prompt. " * 20,
        )

        output = await wrapper.execute(task_input, "llmlingua_20x")

        assert isinstance(output, ToolOutput)
        assert output.response
        assert output.input_tokens > 0
        assert output.output_tokens >= 0
        assert output.compressed_input_tokens is not None
        assert output.compression_ratio is not None
        assert output.compression_ratio < 0.15  # 20x = 0.05
        assert output.technique_variant == "llmlingua_20x"
        assert output.tool_name == "llmlingua"
        assert output.error is None

    @pytest.mark.asyncio
    async def test_llmlingua2_6x_variant(self, wrapper):
        """Test 6x compression variant (balanced)."""
        task_input = ToolInput(
            prompt="Long prompt content. " * 30,
        )

        output = await wrapper.execute(task_input, "llmlingua2_6x")

        assert output.compression_ratio is not None
        assert 0.10 < output.compression_ratio < 0.25  # 6x = 0.17
        assert output.technique_variant == "llmlingua2_6x"

    @pytest.mark.asyncio
    async def test_longllmlingua_rag_variant(self, wrapper):
        """Test RAG-optimized variant."""
        rag_context = "Document section: " * 50 + "Query: what is X?"
        task_input = ToolInput(prompt=rag_context)

        output = await wrapper.execute(task_input, "longllmlingua_rag")

        assert output.compression_ratio is not None
        assert 0.15 < output.compression_ratio < 0.35  # RAG = 0.25
        assert output.technique_variant == "longllmlingua_rag"

    @pytest.mark.asyncio
    async def test_compression_scales_with_input_size(self, wrapper):
        """Test that compression ratio remains consistent across input sizes."""
        short_prompt = "Hello world test prompt."
        long_prompt = "Content content content. " * 100

        short_output = await wrapper.execute(
            ToolInput(prompt=short_prompt), "llmlingua_20x"
        )
        long_output = await wrapper.execute(
            ToolInput(prompt=long_prompt), "llmlingua_20x"
        )

        # Both should have similar compression ratios
        assert short_output.compression_ratio is not None
        assert long_output.compression_ratio is not None
        # Allow some variance due to tokenization edge cases
        assert abs(short_output.compression_ratio - long_output.compression_ratio) < 0.1

    @pytest.mark.asyncio
    async def test_output_tokens_less_than_input(self, wrapper):
        """Test that compression always reduces tokens."""
        task_input = ToolInput(prompt="Test content. " * 50)

        output = await wrapper.execute(task_input, "llmlingua2_6x")

        if output.compressed_input_tokens is not None:
            assert output.compressed_input_tokens <= output.input_tokens

    @pytest.mark.asyncio
    async def test_latency_tracking(self, wrapper):
        """Test that latency metrics are recorded."""
        task_input = ToolInput(prompt="Test prompt. " * 20)

        output = await wrapper.execute(task_input, "llmlingua_20x")

        assert output.latency_ms > 0
        assert output.preprocessing_ms >= 0
        assert output.inference_ms >= 0
        # Preprocessing should be most of the time for compression
        assert output.preprocessing_ms > 0

    @pytest.mark.asyncio
    async def test_all_variants_return_output(self, wrapper):
        """Test that all documented variants work."""
        variants = [
            "llmlingua_20x",
            "llmlingua2_6x",
            "longllmlingua_rag",
        ]

        task_input = ToolInput(prompt="Test content. " * 30)

        for variant in variants:
            output = await wrapper.execute(task_input, variant)
            assert output.technique_variant == variant
            assert output.response is not None
            assert output.error is None

    @pytest.mark.asyncio
    async def test_empty_prompt_handling(self, wrapper):
        """Test handling of edge case: empty prompt."""
        task_input = ToolInput(prompt="")

        output = await wrapper.execute(task_input, "llmlingua_20x")

        # Should handle gracefully
        assert output.input_tokens == 0
        # Even empty input should have output format
        assert output.response is not None or output.error is not None

    @pytest.mark.asyncio
    async def test_compression_with_code(self, wrapper):
        """Test compression on code content."""
        code_prompt = """
def hello():
    '''Docstring.'''
    x = 1
    y = 2
    return x + y

# This is a long comment section
# with multiple lines
# explaining implementation details
""" * 5

        output = await wrapper.execute(ToolInput(prompt=code_prompt), "llmlingua_20x")

        assert output.compression_ratio is not None
        assert output.compressed_input_tokens is not None
        # Code typically compresses well
        assert output.compression_ratio < 0.15


class TestLLMLinguaMetrics:
    """Test LLMLingua metrics collection."""

    @pytest.mark.asyncio
    async def test_call_count_increment(self):
        """Test that wrapper tracks execution count."""
        wrapper = LLMLinguaWrapper("llmlingua", {})
        await wrapper.initialize()

        initial_count = wrapper._call_count
        task_input = ToolInput(prompt="test")

        await wrapper.execute(task_input, "llmlingua_20x")
        await wrapper.execute(task_input, "llmlingua2_6x")

        assert wrapper._call_count == initial_count + 2

        await wrapper.cleanup()

    @pytest.mark.asyncio
    async def test_token_estimation(self):
        """Test token estimation utility."""
        wrapper = LLMLinguaWrapper("llmlingua", {})

        tokens_short = wrapper._estimate_tokens("hello")
        tokens_long = wrapper._estimate_tokens("hello world " * 100)

        assert tokens_short < tokens_long
        assert tokens_short > 0

    @pytest.mark.asyncio
    async def test_output_serialization(self):
        """Test that output can be serialized to dict."""
        wrapper = LLMLinguaWrapper("llmlingua", {})
        await wrapper.initialize()

        task_input = ToolInput(prompt="test content" * 20)
        output = await wrapper.execute(task_input, "llmlingua_20x")

        # Should be serializable
        output_dict = output.to_dict()
        assert isinstance(output_dict, dict)
        assert output_dict["tool_name"] == "llmlingua"
        assert output_dict["technique_variant"] == "llmlingua_20x"
        assert "compression_ratio" in output_dict

        await wrapper.cleanup()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
