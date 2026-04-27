"""
Unit tests for tool wrappers.

Tests the wrapper interface contract and validates all implementations.
"""

import pytest
import asyncio
from typing import Dict, Any
from pathlib import Path

# Import base classes
import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

from base_wrapper import (
    ToolInput,
    ToolOutput,
    BaseToolWrapper,
    ToolWrapperRegistry,
)


class TestToolWrapperInterface:
    """Test the base wrapper interface contract."""

    @pytest.fixture
    def mock_wrapper(self):
        """Create a simple mock wrapper for testing."""

        class MockToolWrapper(BaseToolWrapper):
            async def execute(self, task_input: ToolInput, technique_variant: str = "default") -> ToolOutput:
                return ToolOutput(
                    response="mock response",
                    input_tokens=10,
                    output_tokens=5,
                    tool_name="mock",
                    technique_variant=technique_variant,
                )

        return MockToolWrapper("mock", {})

    @pytest.mark.asyncio
    async def test_initialize_cleanup_contract(self, mock_wrapper):
        """Test that initialize/cleanup are called in order."""
        assert not mock_wrapper.initialized

        await mock_wrapper.initialize()
        assert mock_wrapper.initialized

        await mock_wrapper.cleanup()
        assert not mock_wrapper.initialized

    @pytest.mark.asyncio
    async def test_execute_returns_tool_output(self, mock_wrapper):
        """Test that execute returns properly formatted ToolOutput."""
        await mock_wrapper.initialize()

        task_input = ToolInput(prompt="test prompt")
        output = await mock_wrapper.execute(task_input)

        assert isinstance(output, ToolOutput)
        assert output.response == "mock response"
        assert output.input_tokens == 10
        assert output.output_tokens == 5
        assert output.tool_name == "mock"

        await mock_wrapper.cleanup()

    @pytest.mark.asyncio
    async def test_tool_output_to_dict_serializable(self, mock_wrapper):
        """Test that ToolOutput can be serialized to dict."""
        await mock_wrapper.initialize()

        task_input = ToolInput(prompt="test")
        output = await mock_wrapper.execute(task_input)

        # Should not raise
        result_dict = output.to_dict()
        assert isinstance(result_dict, dict)
        assert result_dict["response"] == "mock response"
        assert result_dict["tool_name"] == "mock"

        await mock_wrapper.cleanup()

    def test_token_estimation(self, mock_wrapper):
        """Test token estimation fallback."""
        tokens = mock_wrapper._estimate_tokens("hello world test")
        assert tokens > 0
        assert isinstance(tokens, int)

    def test_call_count_tracking(self, mock_wrapper):
        """Test that call count is incremented."""
        assert mock_wrapper._call_count == 0

        mock_wrapper._increment_call_count()
        assert mock_wrapper._call_count == 1

        mock_wrapper._increment_call_count()
        assert mock_wrapper._call_count == 2

    @pytest.mark.asyncio
    async def test_timeout_handling(self, mock_wrapper):
        """Test that timeout is enforced."""

        async def slow_operation():
            await asyncio.sleep(10)
            return "done"

        with pytest.raises(TimeoutError):
            await mock_wrapper._with_timeout(slow_operation(), timeout_sec=0.1)

    @pytest.mark.asyncio
    async def test_technique_variant_support(self, mock_wrapper):
        """Test that technique variants are passed through."""
        await mock_wrapper.initialize()

        variants = ["aggressive", "balanced", "conservative", "custom_variant"]

        for variant in variants:
            task_input = ToolInput(prompt="test")
            output = await mock_wrapper.execute(task_input, variant)
            assert output.technique_variant == variant

        await mock_wrapper.cleanup()


class TestToolWrapperRegistry:
    """Test the tool wrapper registry."""

    def test_register_and_get(self):
        """Test basic registration and retrieval."""

        class CustomTool(BaseToolWrapper):
            async def execute(self, task_input: ToolInput, technique_variant: str = "default") -> ToolOutput:
                return ToolOutput(
                    response="",
                    input_tokens=0,
                    output_tokens=0,
                )

        # Register
        ToolWrapperRegistry.register("custom", CustomTool)

        # Retrieve
        retrieved_class = ToolWrapperRegistry.get("custom")
        assert retrieved_class is CustomTool

    def test_instantiate(self):
        """Test tool instantiation."""

        class TestTool(BaseToolWrapper):
            async def execute(self, task_input: ToolInput, technique_variant: str = "default") -> ToolOutput:
                return ToolOutput(
                    response="",
                    input_tokens=0,
                    output_tokens=0,
                )

        ToolWrapperRegistry.register("test", TestTool)
        config = {"param1": "value1"}

        instance = ToolWrapperRegistry.instantiate("test", config)
        assert isinstance(instance, TestTool)
        assert instance.config == config

    def test_list_tools(self):
        """Test listing registered tools."""
        tools = ToolWrapperRegistry.list_tools()
        assert isinstance(tools, list)
        assert len(tools) > 0

    def test_unknown_tool_error(self):
        """Test error on unknown tool."""
        with pytest.raises(ValueError, match="Unknown tool"):
            ToolWrapperRegistry.get("nonexistent_tool_xyz")


class TestToolInput:
    """Test ToolInput standardization."""

    def test_tool_input_defaults(self):
        """Test ToolInput default values."""
        inp = ToolInput(prompt="test")

        assert inp.prompt == "test"
        assert inp.model == "gpt-3.5-turbo"
        assert inp.temperature == 0.7
        assert inp.metadata == {}

    def test_tool_input_custom_config(self):
        """Test ToolInput with custom config."""
        inp = ToolInput(
            prompt="test",
            model="gpt-4",
            temperature=0.2,
            max_tokens=500,
            metadata={"category": "code"},
        )

        assert inp.model == "gpt-4"
        assert inp.temperature == 0.2
        assert inp.max_tokens == 500
        assert inp.metadata["category"] == "code"


class TestToolOutput:
    """Test ToolOutput metrics tracking."""

    def test_output_error_handling(self):
        """Test ToolOutput with error."""
        output = ToolOutput(
            response="",
            input_tokens=0,
            output_tokens=0,
            error="Failed to execute",
        )

        assert output.error is not None
        dict_output = output.to_dict()
        assert dict_output["error"] == "Failed to execute"

    def test_compression_ratio_calculation(self):
        """Test compression ratio tracking."""
        output = ToolOutput(
            response="compressed",
            input_tokens=100,
            output_tokens=10,
            compressed_input_tokens=25,
            compression_ratio=0.25,  # 4x compression
        )

        assert output.compression_ratio == 0.25
        assert output.compressed_input_tokens == 25

    def test_timing_metrics(self):
        """Test timing metrics are tracked."""
        output = ToolOutput(
            response="test",
            input_tokens=5,
            output_tokens=3,
            latency_ms=150.5,
            preprocessing_ms=50.0,
            inference_ms=100.5,
        )

        assert output.latency_ms == 150.5
        assert output.preprocessing_ms == 50.0
        assert output.inference_ms == 100.5
        # Roughly verify timing breakdown
        assert abs(output.preprocessing_ms + output.inference_ms - output.latency_ms) < 1.0

    def test_quality_score_optional(self):
        """Test that quality score is optional."""
        output1 = ToolOutput(
            response="test",
            input_tokens=0,
            output_tokens=0,
        )
        assert output1.quality_score is None

        output2 = ToolOutput(
            response="test",
            input_tokens=0,
            output_tokens=0,
            quality_score=0.95,
        )
        assert output2.quality_score == 0.95


# Integration test
@pytest.mark.asyncio
async def test_full_wrapper_lifecycle():
    """Test complete wrapper lifecycle: init -> execute -> cleanup."""

    class IntegrationTestWrapper(BaseToolWrapper):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.executed_count = 0

        async def execute(self, task_input: ToolInput, technique_variant: str = "default") -> ToolOutput:
            self.executed_count += 1
            return ToolOutput(
                response=f"Response #{self.executed_count}",
                input_tokens=len(task_input.prompt),
                output_tokens=10,
                tool_name=self.tool_name,
                technique_variant=technique_variant,
            )

    wrapper = IntegrationTestWrapper("integration_test", {})

    # Lifecycle
    await wrapper.initialize()
    assert wrapper.initialized

    # Multiple executions
    for i in range(3):
        task_input = ToolInput(prompt=f"test {i}")
        output = await wrapper.execute(task_input)

        assert output.response == f"Response #{i+1}"
        assert output.tool_name == "integration_test"
        assert output.error is None

    assert wrapper.executed_count == 3

    await wrapper.cleanup()
    assert not wrapper.initialized


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
