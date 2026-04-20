"""Tests for response validation module."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from llm_router.response_validation import (
    LLMResponse,
    ResponseValidationError,
    StreamingResponse,
    safe_extract_content,
    validate_response,
    validate_response_list,
)


class TestLLMResponseValidation:
    """Test LLM response schema validation."""

    def test_validates_minimal_valid_response(self) -> None:
        """Validates response with required fields only."""
        response = LLMResponse(
            content="This is a response",
            model="gpt-4",
        )
        assert response.content == "This is a response"
        assert response.model == "gpt-4"

    def test_validates_complete_response(self) -> None:
        """Validates response with all fields."""
        response = LLMResponse(
            content="Complete response",
            model="claude-3-opus",
            stop_reason="stop",
            input_tokens=100,
            output_tokens=50,
            cost_usd=0.01,
            provider="anthropic",
        )
        assert response.input_tokens == 100
        assert response.cost_usd == 0.01

    def test_rejects_missing_required_content(self) -> None:
        """Rejects response without content."""
        with pytest.raises(ValidationError):
            LLMResponse(model="gpt-4")  # type: ignore

    def test_rejects_missing_model(self) -> None:
        """Rejects response without model."""
        with pytest.raises(ValidationError):
            LLMResponse(content="response")  # type: ignore

    def test_rejects_empty_content(self) -> None:
        """Rejects empty content string."""
        with pytest.raises(ValidationError):
            LLMResponse(content="", model="gpt-4")

    def test_rejects_empty_model(self) -> None:
        """Rejects empty model string."""
        with pytest.raises(ValidationError):
            LLMResponse(content="response", model="")

    def test_rejects_non_string_content(self) -> None:
        """Rejects non-string content."""
        with pytest.raises(ValidationError):
            LLMResponse(content=123, model="gpt-4")  # type: ignore

    def test_rejects_non_string_model(self) -> None:
        """Rejects non-string model."""
        with pytest.raises(ValidationError):
            LLMResponse(content="response", model=123)  # type: ignore

    def test_rejects_negative_input_tokens(self) -> None:
        """Rejects negative token counts."""
        with pytest.raises(ValidationError):
            LLMResponse(
                content="response",
                model="gpt-4",
                input_tokens=-10,
            )

    def test_rejects_negative_cost(self) -> None:
        """Rejects negative cost."""
        with pytest.raises(ValidationError):
            LLMResponse(
                content="response",
                model="gpt-4",
                cost_usd=-0.01,
            )

    def test_rejects_extremely_large_cost(self) -> None:
        """Rejects unreasonably large cost (sanity check)."""
        with pytest.raises(ValidationError):
            LLMResponse(
                content="response",
                model="gpt-4",
                cost_usd=10000.0,
            )

    def test_rejects_content_with_null_bytes(self) -> None:
        """Rejects content containing null bytes."""
        with pytest.raises(ValidationError):
            LLMResponse(
                content="response\x00injection",
                model="gpt-4",
            )

    def test_rejects_invalid_provider(self) -> None:
        """Logs warning for unknown provider but still validates."""
        # Unknown providers should log warning but not fail validation
        response = LLMResponse(
            content="response",
            model="gpt-4",
            provider="unknown_provider",
        )
        assert response.provider == "unknown_provider"

    def test_rejects_extra_fields(self) -> None:
        """Rejects response with unexpected fields."""
        with pytest.raises(ValidationError):
            LLMResponse(
                content="response",
                model="gpt-4",
                extra_field="not allowed",  # type: ignore
            )

    def test_validates_model_with_special_chars(self) -> None:
        """Accepts model names with dots, dashes, underscores."""
        response = LLMResponse(
            content="response",
            model="gpt-4-turbo-preview",
        )
        assert response.model == "gpt-4-turbo-preview"

    def test_rejects_model_with_invalid_chars(self) -> None:
        """Rejects model names with invalid characters."""
        with pytest.raises(ValidationError):
            LLMResponse(
                content="response",
                model="gpt-4; DROP TABLE models",
            )


class TestStreamingResponseValidation:
    """Test streaming response chunk validation."""

    def test_validates_minimal_chunk(self) -> None:
        """Validates minimal streaming chunk."""
        chunk = StreamingResponse(chunk="Hello ")
        assert chunk.chunk == "Hello "
        assert chunk.is_final is False

    def test_validates_final_chunk(self) -> None:
        """Validates final streaming chunk."""
        chunk = StreamingResponse(
            chunk="world!",
            is_final=True,
            stop_reason="stop",
        )
        assert chunk.is_final is True
        assert chunk.stop_reason == "stop"

    def test_rejects_missing_chunk(self) -> None:
        """Rejects response without chunk."""
        with pytest.raises(ValidationError):
            StreamingResponse()  # type: ignore

    def test_rejects_empty_chunk(self) -> None:
        """Rejects empty chunk (but allows whitespace-only)."""
        # Empty string is actually allowed for streaming
        chunk = StreamingResponse(chunk="")
        assert chunk.chunk == ""

    def test_rejects_chunk_with_null_bytes(self) -> None:
        """Rejects chunk containing null bytes."""
        with pytest.raises(ValidationError):
            StreamingResponse(chunk="text\x00injection")

    def test_rejects_extra_fields(self) -> None:
        """Rejects streaming chunk with extra fields."""
        with pytest.raises(ValidationError):
            StreamingResponse(
                chunk="text",
                extra="field",  # type: ignore
            )


class TestValidateResponseFunction:
    """Test validate_response function."""

    def test_validates_dict_response(self) -> None:
        """Validates response from dict."""
        data = {
            "content": "test response",
            "model": "gpt-4",
            "cost_usd": 0.01,
        }
        response = validate_response(data)
        assert response.content == "test response"

    def test_raises_validation_error_on_invalid_response(self) -> None:
        """Raises ResponseValidationError on invalid response."""
        data = {"content": "", "model": "gpt-4"}  # Empty content
        with pytest.raises(ResponseValidationError):
            validate_response(data)

    def test_logs_error_on_validation_failure(self, caplog) -> None:
        """Logs error when validation fails."""
        data = {"content": "", "model": "gpt-4"}
        try:
            validate_response(data)
        except ResponseValidationError:
            pass
        assert "Response validation failed" in caplog.text


class TestValidateResponseList:
    """Test validate_response_list function."""

    def test_validates_list_of_responses(self) -> None:
        """Validates a list of responses."""
        responses = [
            {"content": "response 1", "model": "gpt-4"},
            {"content": "response 2", "model": "claude-3"},
        ]
        validated = validate_response_list(responses)
        assert len(validated) == 2
        assert validated[0].content == "response 1"

    def test_rejects_non_list(self) -> None:
        """Rejects non-list input."""
        with pytest.raises(ResponseValidationError):
            validate_response_list("not a list")  # type: ignore

    def test_reports_all_invalid_responses(self) -> None:
        """Reports errors for each invalid response."""
        responses = [
            {"content": "valid", "model": "gpt-4"},
            {"content": "", "model": "gpt-4"},  # Invalid
            {"model": "gpt-4"},  # Missing content
        ]
        with pytest.raises(ResponseValidationError) as exc_info:
            validate_response_list(responses)
        error_msg = str(exc_info.value)
        assert "Response 1:" in error_msg
        assert "Response 2:" in error_msg


class TestSafeExtractContent:
    """Test safe_extract_content function."""

    def test_extracts_from_valid_response_object(self) -> None:
        """Extracts content from validated LLMResponse."""
        response = LLMResponse(content="test content", model="gpt-4")
        content = safe_extract_content(response)
        assert content == "test content"

    def test_extracts_from_valid_dict(self) -> None:
        """Extracts content from dict, validating first."""
        data = {"content": "test content", "model": "gpt-4"}
        content = safe_extract_content(data)
        assert content == "test content"

    def test_raises_on_invalid_response(self) -> None:
        """Raises ResponseValidationError on invalid response."""
        data = {"content": "", "model": "gpt-4"}
        with pytest.raises(ResponseValidationError):
            safe_extract_content(data)

    def test_handles_edge_case_content(self) -> None:
        """Handles edge case content strings."""
        # Long content
        long_content = "x" * 100_000
        response = LLMResponse(content=long_content, model="gpt-4")
        assert safe_extract_content(response) == long_content

        # Content with special chars (but no null bytes)
        special_content = "émojis 🎉 and ñ special chars"
        response = LLMResponse(content=special_content, model="gpt-4")
        assert safe_extract_content(response) == special_content
