"""Tests for input validation module."""

from __future__ import annotations

import pytest

from llm_router.input_validation import (
    ValidationError,
    validate_complexity,
    validate_max_tokens,
    validate_routing_parameters,
    validate_task_type,
    validate_temperature,
)
from llm_router.types import Complexity, TaskType


class TestTaskTypeValidation:
    """Test task type parameter validation."""

    def test_validates_valid_task_types(self) -> None:
        """Accepts all valid TaskType enum values."""
        for task_type in TaskType:
            result = validate_task_type(task_type.value)
            assert result == task_type

    def test_case_insensitive(self) -> None:
        """Task type validation is case-insensitive."""
        assert validate_task_type("QUERY") == TaskType.QUERY
        assert validate_task_type("Query") == TaskType.QUERY
        assert validate_task_type("query") == TaskType.QUERY
        assert validate_task_type("CODE") == TaskType.CODE

    def test_handles_none(self) -> None:
        """None input returns None."""
        assert validate_task_type(None) is None

    def test_rejects_invalid_task_type(self) -> None:
        """Rejects invalid task type strings."""
        with pytest.raises(ValidationError) as exc_info:
            validate_task_type("invalid_task")
        assert "Invalid task_type" in str(exc_info.value)
        assert "invalid_task" in str(exc_info.value)

    def test_rejects_non_string_task_type(self) -> None:
        """Rejects non-string task type."""
        with pytest.raises(ValidationError) as exc_info:
            validate_task_type(123)  # type: ignore
        assert "must be a string" in str(exc_info.value)

    def test_suggests_valid_options(self) -> None:
        """Error message includes valid task type options."""
        with pytest.raises(ValidationError) as exc_info:
            validate_task_type("bad_type")
        error_msg = str(exc_info.value)
        assert "query" in error_msg  # Should suggest valid options
        assert "code" in error_msg


class TestComplexityValidation:
    """Test complexity parameter validation."""

    def test_validates_valid_complexity(self) -> None:
        """Accepts all valid Complexity enum values."""
        for complexity in Complexity:
            result = validate_complexity(complexity.value)
            assert result == complexity

    def test_case_insensitive(self) -> None:
        """Complexity validation is case-insensitive."""
        assert validate_complexity("SIMPLE") == Complexity.SIMPLE
        assert validate_complexity("Simple") == Complexity.SIMPLE
        assert validate_complexity("simple") == Complexity.SIMPLE
        assert validate_complexity("COMPLEX") == Complexity.COMPLEX

    def test_handles_none(self) -> None:
        """None input returns None."""
        assert validate_complexity(None) is None

    def test_rejects_invalid_complexity(self) -> None:
        """Rejects invalid complexity strings."""
        with pytest.raises(ValidationError) as exc_info:
            validate_complexity("super_complex")
        assert "Invalid complexity" in str(exc_info.value)

    def test_rejects_non_string_complexity(self) -> None:
        """Rejects non-string complexity."""
        with pytest.raises(ValidationError) as exc_info:
            validate_complexity(99)  # type: ignore
        assert "must be a string" in str(exc_info.value)


class TestTemperatureValidation:
    """Test temperature parameter validation."""

    def test_validates_valid_temperatures(self) -> None:
        """Accepts valid temperature range (0.0-2.0)."""
        assert validate_temperature(0.0) == 0.0
        assert validate_temperature(0.5) == 0.5
        assert validate_temperature(1.0) == 1.0
        assert validate_temperature(1.5) == 1.5
        assert validate_temperature(2.0) == 2.0

    def test_handles_none(self) -> None:
        """None input returns None."""
        assert validate_temperature(None) is None

    def test_converts_int_to_float(self) -> None:
        """Converts integer input to float."""
        result = validate_temperature(1)  # type: ignore
        assert result == 1.0
        assert isinstance(result, float)

    def test_rejects_negative_temperature(self) -> None:
        """Rejects negative temperature."""
        with pytest.raises(ValidationError) as exc_info:
            validate_temperature(-0.1)
        assert "between 0.0 and 2.0" in str(exc_info.value)

    def test_rejects_temperature_above_2(self) -> None:
        """Rejects temperature above 2.0."""
        with pytest.raises(ValidationError) as exc_info:
            validate_temperature(2.1)
        assert "between 0.0 and 2.0" in str(exc_info.value)

    def test_rejects_non_numeric_temperature(self) -> None:
        """Rejects non-numeric temperature."""
        with pytest.raises(ValidationError) as exc_info:
            validate_temperature("hot")  # type: ignore
        assert "must be a number" in str(exc_info.value)


class TestMaxTokensValidation:
    """Test max_tokens parameter validation."""

    def test_validates_valid_max_tokens(self) -> None:
        """Accepts positive integer max_tokens."""
        assert validate_max_tokens(1) == 1
        assert validate_max_tokens(100) == 100
        assert validate_max_tokens(1000) == 1000
        assert validate_max_tokens(128000) == 128000

    def test_handles_none(self) -> None:
        """None input returns None."""
        assert validate_max_tokens(None) is None

    def test_rejects_zero_max_tokens(self) -> None:
        """Rejects zero max_tokens."""
        with pytest.raises(ValidationError) as exc_info:
            validate_max_tokens(0)
        assert "must be positive" in str(exc_info.value)

    def test_rejects_negative_max_tokens(self) -> None:
        """Rejects negative max_tokens."""
        with pytest.raises(ValidationError) as exc_info:
            validate_max_tokens(-100)
        assert "must be positive" in str(exc_info.value)

    def test_rejects_non_integer_max_tokens(self) -> None:
        """Rejects non-integer max_tokens."""
        with pytest.raises(ValidationError) as exc_info:
            validate_max_tokens(100.5)  # type: ignore
        assert "must be an integer" in str(exc_info.value)

    def test_rejects_string_max_tokens(self) -> None:
        """Rejects string max_tokens."""
        with pytest.raises(ValidationError) as exc_info:
            validate_max_tokens("1000")  # type: ignore
        assert "must be an integer" in str(exc_info.value)


class TestCombinedValidation:
    """Test validate_routing_parameters with multiple parameters."""

    def test_validates_all_valid_parameters(self) -> None:
        """Validates multiple valid parameters."""
        result = validate_routing_parameters(
            task_type="query",
            complexity="simple",
            temperature=0.7,
            max_tokens=100,
        )
        assert result["task_type"] == TaskType.QUERY
        assert result["complexity"] == Complexity.SIMPLE
        assert result["temperature"] == 0.7
        assert result["max_tokens"] == 100

    def test_validates_all_none_parameters(self) -> None:
        """Handles all None parameters."""
        result = validate_routing_parameters()
        assert result["task_type"] is None
        assert result["complexity"] is None
        assert result["temperature"] is None
        assert result["max_tokens"] is None

    def test_collects_multiple_errors(self) -> None:
        """Collects errors from multiple invalid parameters."""
        with pytest.raises(ValidationError) as exc_info:
            validate_routing_parameters(
                task_type="invalid_type",
                complexity="super_complex",
                temperature=3.0,
                max_tokens=-100,
            )
        error_msg = str(exc_info.value)
        # All errors should be reported
        assert "task_type" in error_msg
        assert "complexity" in error_msg
        assert "temperature" in error_msg
        assert "max_tokens" in error_msg

    def test_partial_validation(self) -> None:
        """Validates only provided parameters."""
        result = validate_routing_parameters(
            task_type="code",
            temperature=1.5,
        )
        assert result["task_type"] == TaskType.CODE
        assert result["temperature"] == 1.5
        assert result["complexity"] is None
        assert result["max_tokens"] is None

    def test_error_message_includes_parameter_name(self) -> None:
        """Error message identifies which parameter is invalid."""
        with pytest.raises(ValidationError) as exc_info:
            validate_routing_parameters(task_type="bad_type")
        assert "task_type:" in str(exc_info.value)
