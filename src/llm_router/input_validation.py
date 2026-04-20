"""Input validation for routing parameters.

Validates user-provided task types, complexity levels, and other parameters
to prevent invalid routing decisions and potential security issues.
"""

from __future__ import annotations

import logging
from typing import Any

from llm_router.types import Complexity, TaskType

logger = logging.getLogger(__name__)


class ValidationError(ValueError):
    """Raised when input validation fails."""

    pass


def validate_task_type(task_type: str | None) -> TaskType | None:
    """Validate and convert task_type parameter.

    Args:
        task_type: User-provided task type (string or None)

    Returns:
        Validated TaskType enum value, or None if input is None

    Raises:
        ValidationError: If task_type is not a valid task type
    """
    if task_type is None:
        return None

    if not isinstance(task_type, str):
        raise ValidationError(
            f"task_type must be a string, got {type(task_type).__name__}"
        )

    task_type_lower = task_type.lower().strip()

    # Try to convert to enum
    try:
        return TaskType(task_type_lower)
    except ValueError:
        valid_types = [t.value for t in TaskType]
        raise ValidationError(
            f"Invalid task_type: '{task_type}'. "
            f"Must be one of: {', '.join(valid_types)}"
        )


def validate_complexity(complexity: str | None) -> Complexity | None:
    """Validate and convert complexity parameter.

    Args:
        complexity: User-provided complexity level (string or None)

    Returns:
        Validated Complexity enum value, or None if input is None

    Raises:
        ValidationError: If complexity is not a valid complexity level
    """
    if complexity is None:
        return None

    if not isinstance(complexity, str):
        raise ValidationError(
            f"complexity must be a string, got {type(complexity).__name__}"
        )

    complexity_lower = complexity.lower().strip()

    # Try to convert to enum
    try:
        return Complexity(complexity_lower)
    except ValueError:
        valid_levels = [c.value for c in Complexity]
        raise ValidationError(
            f"Invalid complexity: '{complexity}'. "
            f"Must be one of: {', '.join(valid_levels)}"
        )


def validate_temperature(temperature: float | None) -> float | None:
    """Validate temperature parameter.

    Args:
        temperature: Sampling temperature (0.0-2.0) or None

    Returns:
        Validated temperature, or None if input is None

    Raises:
        ValidationError: If temperature is outside valid range
    """
    if temperature is None:
        return None

    if not isinstance(temperature, (int, float)):
        raise ValidationError(
            f"temperature must be a number, got {type(temperature).__name__}"
        )

    # Convert int to float for comparison
    temp_float = float(temperature)

    if not 0.0 <= temp_float <= 2.0:
        raise ValidationError(
            f"temperature must be between 0.0 and 2.0, got {temperature}"
        )

    return temp_float


def validate_max_tokens(max_tokens: int | None) -> int | None:
    """Validate max_tokens parameter.

    Args:
        max_tokens: Maximum output tokens or None

    Returns:
        Validated max_tokens, or None if input is None

    Raises:
        ValidationError: If max_tokens is invalid
    """
    if max_tokens is None:
        return None

    if not isinstance(max_tokens, int):
        raise ValidationError(
            f"max_tokens must be an integer, got {type(max_tokens).__name__}"
        )

    if max_tokens <= 0:
        raise ValidationError(f"max_tokens must be positive, got {max_tokens}")

    # Warn about unusually large values
    if max_tokens > 128000:
        logger.warning(
            f"max_tokens is very large ({max_tokens}), "
            "may exceed model context window"
        )

    return max_tokens


def validate_routing_parameters(
    task_type: str | None = None,
    complexity: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    """Validate all routing parameters together.

    Args:
        task_type: User-provided task type
        complexity: User-provided complexity level
        temperature: Sampling temperature
        max_tokens: Maximum output tokens

    Returns:
        Dictionary with validated parameters

    Raises:
        ValidationError: If any parameter is invalid (includes details about which one)
    """
    validated = {}
    errors = []

    # Validate task_type
    try:
        validated["task_type"] = validate_task_type(task_type)
    except ValidationError as e:
        errors.append(f"task_type: {e}")

    # Validate complexity
    try:
        validated["complexity"] = validate_complexity(complexity)
    except ValidationError as e:
        errors.append(f"complexity: {e}")

    # Validate temperature
    try:
        validated["temperature"] = validate_temperature(temperature)
    except ValidationError as e:
        errors.append(f"temperature: {e}")

    # Validate max_tokens
    try:
        validated["max_tokens"] = validate_max_tokens(max_tokens)
    except ValidationError as e:
        errors.append(f"max_tokens: {e}")

    # If any errors, raise with all details
    if errors:
        raise ValidationError(
            "Input validation failed:\n  - " + "\n  - ".join(errors)
        )

    return validated
