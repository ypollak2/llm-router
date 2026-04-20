"""Response validation for external LLM APIs.

Validates responses from external LLM providers to ensure they match expected
schemas and prevent code injection or malicious payload processing.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError as PydanticValidationError, field_validator

logger = logging.getLogger(__name__)


class ResponseValidationError(ValueError):
    """Raised when response validation fails."""

    pass


class LLMResponse(BaseModel):
    """Strict schema for LLM provider responses."""

    content: str = Field(
        ...,
        description="The text response from the LLM",
        min_length=1,
        max_length=1_000_000,
    )
    model: str = Field(
        ...,
        description="The model that generated the response",
        min_length=1,
        max_length=255,
    )
    stop_reason: Optional[str] = Field(
        None,
        description="Why the model stopped generating (stop, length, error, etc.)",
        max_length=50,
    )
    input_tokens: Optional[int] = Field(
        None,
        description="Number of input tokens used",
        ge=0,
        le=1_000_000,
    )
    output_tokens: Optional[int] = Field(
        None,
        description="Number of output tokens generated",
        ge=0,
        le=1_000_000,
    )
    cost_usd: Optional[float] = Field(
        None,
        description="Approximate cost in USD",
        ge=0.0,
        le=1000.0,  # Sanity check: no single call should cost more than $1000
    )
    provider: Optional[str] = Field(
        None,
        description="The provider (openai, gemini, anthropic, etc.)",
        max_length=50,
    )

    model_config = ConfigDict(
        extra="forbid",  # Reject any extra fields
        validate_assignment=True,
    )

    @field_validator("content", mode="before")
    @classmethod
    def validate_content(cls, v: Any) -> str:
        """Ensure content is a string and not malicious."""
        if not isinstance(v, str):
            raise ValueError("content must be a string")
        # Check for suspicious patterns (basic checks)
        if v.count("\x00") > 0:  # Null bytes
            raise ValueError("content contains null bytes")
        return v

    @field_validator("model", mode="before")
    @classmethod
    def validate_model(cls, v: Any) -> str:
        """Ensure model name is safe."""
        if not isinstance(v, str):
            raise ValueError("model must be a string")
        # Model names should only contain alphanumerics, dots, dashes, underscores
        if not all(c.isalnum() or c in ".-_/" for c in v):
            raise ValueError("model contains invalid characters")
        return v

    @field_validator("provider", mode="before")
    @classmethod
    def validate_provider(cls, v: Any) -> Optional[str]:
        """Ensure provider name is safe."""
        if v is None:
            return None
        if not isinstance(v, str):
            raise ValueError("provider must be a string or None")
        valid_providers = {
            "openai",
            "gemini",
            "anthropic",
            "perplexity",
            "groq",
            "mistral",
            "together",
            "cohere",
            "deepseek",
            "xai",
            "ollama",
            "codex",
            "elevenlabs",
            "runway",
            "replicate",
            "stability",
        }
        if v.lower() not in valid_providers:
            logger.warning(f"Unknown provider: {v}")
        return v


class StreamingResponse(BaseModel):
    """Schema for streaming response chunks."""

    chunk: str = Field(
        ...,
        description="The text chunk from the stream",
        max_length=10_000,
    )
    stop_reason: Optional[str] = Field(
        None,
        description="Stop reason if stream is finished",
        max_length=50,
    )
    is_final: bool = Field(
        default=False,
        description="Whether this is the final chunk",
    )

    model_config = ConfigDict(extra="forbid")

    @field_validator("chunk", mode="before")
    @classmethod
    def validate_chunk(cls, v: Any) -> str:
        """Ensure chunk is safe."""
        if not isinstance(v, str):
            raise ValueError("chunk must be a string")
        if v.count("\x00") > 0:
            raise ValueError("chunk contains null bytes")
        return v


def validate_response(data: Any) -> LLMResponse:
    """Validate an LLM response against the schema.

    Args:
        data: Raw response data from LLM provider (usually a dict)

    Returns:
        Validated LLMResponse object

    Raises:
        ResponseValidationError: If response doesn't match schema
    """
    try:
        return LLMResponse(**data)
    except PydanticValidationError as e:
        logger.error(f"Response validation failed: {e}")
        # Raise with a clear error message
        raise ResponseValidationError(
            f"Invalid LLM response: {e.error_count()} validation error(s)"
        ) from e


def validate_streaming_chunk(data: Any) -> StreamingResponse:
    """Validate a streaming response chunk.

    Args:
        data: Raw chunk data from streaming LLM response

    Returns:
        Validated StreamingResponse object

    Raises:
        ResponseValidationError: If chunk doesn't match schema
    """
    try:
        return StreamingResponse(**data)
    except PydanticValidationError as e:
        logger.error(f"Streaming chunk validation failed: {e}")
        raise ResponseValidationError(
            f"Invalid streaming chunk: {e.error_count()} validation error(s)"
        ) from e


def validate_response_list(responses: list[Any]) -> list[LLMResponse]:
    """Validate a list of LLM responses.

    Args:
        responses: List of raw response data

    Returns:
        List of validated LLMResponse objects

    Raises:
        ResponseValidationError: If any response doesn't match schema (includes details about which)
    """
    if not isinstance(responses, list):
        raise ResponseValidationError("responses must be a list")

    validated = []
    errors = []

    for idx, response_data in enumerate(responses):
        try:
            validated.append(validate_response(response_data))
        except ResponseValidationError as e:
            errors.append(f"Response {idx}: {e}")

    if errors:
        raise ResponseValidationError(
            "List validation failed:\n  - " + "\n  - ".join(errors)
        )

    return validated


def safe_extract_content(response: Any) -> str:
    """Safely extract content from an LLM response.

    Validates the response first, then extracts the content field.

    Args:
        response: Raw or validated response

    Returns:
        The response content string

    Raises:
        ValidationError: If response is invalid
    """
    if isinstance(response, LLMResponse):
        return response.content

    validated = validate_response(response)
    return validated.content
