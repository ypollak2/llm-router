"""Error message sanitization for user-facing responses.

Removes sensitive information from error messages (database paths, SQL queries,
file paths, stack traces) before displaying them to users.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# Patterns to detect and redact sensitive information
_SENSITIVE_PATTERNS = {
    # File paths (absolute and relative)
    r"(/[\w\-./]+)+\.(?:py|js|ts|sql|json|yaml|yml|env|ini|conf|cfg)",
    # Database paths
    r"(?:\/[^\s/]+)*\/\w+\.(?:db|sqlite|sqlite3|mdb)",
    # SQL query patterns
    r"(?:SELECT|INSERT|UPDATE|DELETE|DROP|CREATE|ALTER)\s+[\w\s(),;*=<>'\"`-]+",
    # AWS/GCP/Azure credentials
    r"(?:AKIA|asia)[0-9A-Z]{16}",
    r"AIza[0-9A-Za-z\-_]{35}",
    # Database connection strings
    r"(?:mysql|postgres|mongodb|redis)://[^\s]+",
    # API endpoints with credentials
    r"https?://[^\s:/@]+:[^\s/@]+@[^\s/]+",
    # Stack traces (file:line patterns)
    r"File\s+['\"]([^\s'\"]+)['\"],\s+line\s+\d+",
    # Sensitive function names in traces
    r"in\s+(?:authenticate|login|authorize|verify|decrypt|hash|password|secret|key|token)",
}


class SanitizedError(Exception):
    """Exception with a sanitized user message and detailed log message."""

    def __init__(self, user_message: str, log_message: str | None = None):
        """Initialize sanitized error.

        Args:
            user_message: Safe message to show to users
            log_message: Detailed message to log (can contain sensitive info)
        """
        self.user_message = user_message
        self.log_message = log_message or user_message
        super().__init__(user_message)


def sanitize_error_message(error_message: str, generic_fallback: str | None = None) -> str:
    """Sanitize an error message by removing sensitive information.

    Args:
        error_message: Raw error message (may contain sensitive data)
        generic_fallback: Generic message to return if sanitization removes all content

    Returns:
        Sanitized error message safe for user display
    """
    if not error_message:
        return generic_fallback or "An error occurred"

    sanitized = error_message
    redaction_count = 0

    # Redact each sensitive pattern
    for pattern in _SENSITIVE_PATTERNS:
        matches = re.findall(pattern, sanitized, re.IGNORECASE)
        if matches:
            redaction_count += len(matches)
            sanitized = re.sub(pattern, "[REDACTED]", sanitized, flags=re.IGNORECASE)

    # If entire message was redacted, return generic fallback
    if sanitized.replace("[REDACTED]", "").strip() == "":
        logger.warning(
            f"Error message was entirely redacted ({redaction_count} sensitive items found). "
            "Original message logged separately.",
            extra={"original_message_length": len(error_message)},
        )
        return generic_fallback or "An error occurred"

    # Log original message for debugging
    if redaction_count > 0:
        logger.debug(
            f"Sanitized error message (removed {redaction_count} sensitive item(s))",
            extra={
                "original_message": error_message,
                "sanitized_message": sanitized,
            },
        )

    return sanitized.strip()


def sanitize_exception(exc: Exception, generic_fallback: str | None = None) -> str:
    """Sanitize an exception message.

    Args:
        exc: Exception to sanitize
        generic_fallback: Generic message to return if sanitization removes all content

    Returns:
        Sanitized exception message
    """
    exc_str = str(exc)
    exc_type = type(exc).__name__

    # Include exception type if it's informative
    if exc_type in ("ValueError", "TypeError", "KeyError", "AttributeError"):
        message = f"{exc_type}: {exc_str}"
    else:
        message = exc_str

    return sanitize_error_message(message, generic_fallback)


def create_user_error_message(
    error_type: str,
    public_details: str | None = None,
    internal_error: Exception | None = None,
) -> str:
    """Create a user-friendly error message with optional internal error logging.

    Args:
        error_type: Type of error (e.g., "database_error", "api_error", "validation_error")
        public_details: Safe details that can be shown to users
        internal_error: Internal exception to log (will be sanitized for display)

    Returns:
        User-friendly error message
    """
    # Map error types to user-friendly messages
    error_messages = {
        "database_error": "Database operation failed. Please try again.",
        "api_error": "API request failed. Please try again.",
        "validation_error": "Invalid input provided.",
        "authentication_error": "Authentication failed. Please log in again.",
        "authorization_error": "You don't have permission for this action.",
        "rate_limit_error": "Too many requests. Please wait a moment and try again.",
        "timeout_error": "The request took too long. Please try again.",
        "unknown_error": "An unexpected error occurred. Please try again.",
    }

    base_message = error_messages.get(error_type, error_messages["unknown_error"])

    # Add public details if provided
    if public_details:
        user_message = f"{base_message} {public_details}"
    else:
        user_message = base_message

    # Log internal error for debugging
    if internal_error:
        logger.error(
            f"Internal error ({error_type}): {internal_error}",
            exc_info=internal_error,
        )

    return user_message


def sanitize_database_error(error: Exception) -> str:
    """Sanitize a database error for user display.

    Removes SQL queries, connection strings, and database paths.

    Args:
        error: Database exception

    Returns:
        Sanitized error message
    """
    return create_user_error_message(
        "database_error",
        internal_error=error,
    )


def sanitize_api_error(error: Exception, include_status: bool = False) -> str:
    """Sanitize an API error for user display.

    Removes endpoints, credentials, and sensitive headers.

    Args:
        error: API exception
        include_status: Whether to include HTTP status (if available)

    Returns:
        Sanitized error message
    """
    message = create_user_error_message("api_error", internal_error=error)

    # Try to extract status code if available
    if include_status and hasattr(error, "status_code"):
        message = f"{message} (HTTP {error.status_code})"  # type: ignore

    return message
