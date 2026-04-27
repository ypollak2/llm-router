"""
Unit tests for Claw wrapper.

Tests Claw content-aware compression.
"""

import pytest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

from base_wrapper import ToolInput, ToolOutput
from claw_wrapper import ClawWrapper


class TestClawWrapper:
    """Test Claw content-aware compression."""

    @pytest.fixture
    async def wrapper(self):
        """Create and initialize Claw wrapper."""
        config = {}
        wrapper = ClawWrapper("claw", config)
        await wrapper.initialize()
        yield wrapper
        await wrapper.cleanup()

    @pytest.mark.asyncio
    async def test_initialize_cleanup(self):
        """Test wrapper lifecycle."""
        wrapper = ClawWrapper("claw", {})
        assert not wrapper.initialized

        await wrapper.initialize()
        assert wrapper.initialized

        await wrapper.cleanup()
        assert not wrapper.initialized

    @pytest.mark.asyncio
    async def test_code_compression_variant(self, wrapper):
        """Test code-optimized compression."""
        code_prompt = """
def fibonacci(n):
    '''Calculate fibonacci number.'''
    if n <= 1:
        return n
    return fibonacci(n-1) + fibonacci(n-2)
""" * 5

        output = await wrapper.execute(ToolInput(prompt=code_prompt), "code_only")

        assert output.response
        assert output.compression_ratio is not None
        assert 0.35 <= output.compression_ratio <= 0.65  # Code compression
        assert output.technique_variant == "code_only"

    @pytest.mark.asyncio
    async def test_json_compression_variant(self, wrapper):
        """Test JSON-optimized compression."""
        json_prompt = """
{"name": "John", "age": 30, "city": "New York", "data": {"key1": "value1", "key2": "value2"}}
""" * 10

        output = await wrapper.execute(ToolInput(prompt=json_prompt), "json_only")

        assert output.response
        assert output.compression_ratio is not None
        assert 0.30 <= output.compression_ratio <= 0.60  # JSON compression
        assert output.technique_variant == "json_only"

    @pytest.mark.asyncio
    async def test_text_compression_variant(self, wrapper):
        """Test text-optimized compression."""
        text_prompt = "This is a long text document with lots of words and filler content. " * 50

        output = await wrapper.execute(ToolInput(prompt=text_prompt), "text_only")

        assert output.response
        assert output.compression_ratio is not None
        assert 0.40 <= output.compression_ratio <= 0.70  # Text compression

    @pytest.mark.asyncio
    async def test_balanced_compression(self, wrapper):
        """Test balanced compression across content types."""
        mixed_content = """
def process_data(data):
    # Process JSON data
    result = {"status": "ok", "items": [1, 2, 3]}
    return result

Mixed text content explaining the function.
""" * 5

        output = await wrapper.execute(ToolInput(prompt=mixed_content), "balanced")

        assert output.response
        assert output.compression_ratio is not None
        assert 0.35 <= output.compression_ratio <= 0.70

    @pytest.mark.asyncio
    async def test_aggressive_compression(self, wrapper):
        """Test aggressive compression."""
        prompt = "Content that needs compression. " * 100

        output = await wrapper.execute(ToolInput(prompt=prompt), "aggressive")

        assert output.response
        assert output.compression_ratio is not None
        # Aggressive should be strongest compression
        assert output.compression_ratio <= 0.50

    @pytest.mark.asyncio
    async def test_content_type_detection_code(self, wrapper):
        """Test detection of code content."""
        code = "def x(): pass"
        output = await wrapper.execute(ToolInput(prompt=code), "balanced")
        assert output.response is not None

    @pytest.mark.asyncio
    async def test_content_type_detection_json(self, wrapper):
        """Test detection of JSON content."""
        json_str = '{"key": "value"}'
        output = await wrapper.execute(ToolInput(prompt=json_str), "balanced")
        assert output.response is not None

    @pytest.mark.asyncio
    async def test_all_variants_work(self, wrapper):
        """Test all compression variants."""
        variants = [
            "code_only",
            "json_only",
            "text_only",
            "balanced",
            "aggressive",
        ]

        prompt = "Test content. " * 30

        for variant in variants:
            output = await wrapper.execute(ToolInput(prompt=prompt), variant)
            assert output.technique_variant == variant
            assert output.response is not None
            assert output.error is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
