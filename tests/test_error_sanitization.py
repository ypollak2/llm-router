"""Tests for error message sanitization."""

from __future__ import annotations


from llm_router.error_sanitization import (
    create_user_error_message,
    sanitize_api_error,
    sanitize_database_error,
    sanitize_error_message,
    sanitize_exception,
)


class TestErrorSanitization:
    """Test basic error message sanitization."""

    def test_sanitizes_file_paths(self) -> None:
        """Removes absolute and relative file paths."""
        error = "Error in /home/user/project/src/auth.py:42"
        sanitized = sanitize_error_message(error)
        assert "/home/user/project" not in sanitized
        assert "[REDACTED]" in sanitized

    def test_sanitizes_database_paths(self) -> None:
        """Removes database file paths."""
        error = "Database error at /var/lib/db/users.sqlite"
        sanitized = sanitize_error_message(error)
        assert "/var/lib/db" not in sanitized
        assert "users.sqlite" not in sanitized

    def test_sanitizes_sql_queries(self) -> None:
        """Removes SQL queries from error messages."""
        error = "SQL Error: SELECT * FROM users WHERE password='secret'"
        sanitized = sanitize_error_message(error)
        assert "SELECT * FROM users" not in sanitized
        assert "[REDACTED]" in sanitized

    def test_sanitizes_connection_strings(self) -> None:
        """Removes database connection strings."""
        error = "Connection failed: postgres://user:pass@localhost:5432/mydb"
        sanitized = sanitize_error_message(error)
        assert "postgres://" not in sanitized
        assert "user:pass" not in sanitized

    def test_sanitizes_api_endpoints_with_auth(self) -> None:
        """Removes API endpoints with embedded credentials."""
        error = "API error: https://user:apikey@api.example.com/v1/endpoint"
        sanitized = sanitize_error_message(error)
        assert "user:apikey" not in sanitized
        assert "[REDACTED]" in sanitized

    def test_sanitizes_aws_credentials(self) -> None:
        """Removes AWS access key patterns."""
        error = "Auth failed: AKIA2345ABCD1234EFGH"
        sanitized = sanitize_error_message(error)
        assert "AKIA" not in sanitized or "[REDACTED]" in sanitized

    def test_sanitizes_file_line_patterns(self) -> None:
        """Removes file:line patterns from stack traces."""
        error = 'Traceback: File "/app/utils/crypto.py", line 42'
        sanitized = sanitize_error_message(error)
        assert "/app/utils/crypto.py" not in sanitized

    def test_preserves_non_sensitive_content(self) -> None:
        """Preserves error messages without sensitive data."""
        error = "Invalid input provided by user"
        sanitized = sanitize_error_message(error)
        assert sanitized == error

    def test_handles_multiple_sensitive_items(self) -> None:
        """Handles messages with multiple sensitive items."""
        error = (
            "Error in /home/user/auth.py connecting to "
            "postgres://admin:password@db.example.com:5432/mydb"
        )
        sanitized = sanitize_error_message(error)
        # Should have at least 2 redactions
        assert sanitized.count("[REDACTED]") >= 2
        assert "password" not in sanitized.lower() or "[REDACTED]" in sanitized

    def test_handles_empty_message(self) -> None:
        """Handles empty error message."""
        assert sanitize_error_message("") == "An error occurred"

    def test_uses_custom_fallback(self) -> None:
        """Uses provided fallback for completely redacted messages."""
        # Message that will be entirely redacted
        error = "/home/user/file.py SELECT * FROM users WHERE id=1"
        sanitized = sanitize_error_message(
            error, generic_fallback="Database operation failed"
        )
        assert sanitized == "Database operation failed"

    def test_case_insensitive_sanitization(self) -> None:
        """Sanitization is case-insensitive."""
        error1 = "Error: SELECT * FROM users"
        error2 = "Error: select * from users"
        error3 = "Error: SeLeCt * FrOm users"

        assert "[REDACTED]" in sanitize_error_message(error1)
        assert "[REDACTED]" in sanitize_error_message(error2)
        assert "[REDACTED]" in sanitize_error_message(error3)


class TestSanitizeException:
    """Test exception sanitization."""

    def test_sanitizes_exception_message(self) -> None:
        """Sanitizes exception messages."""
        try:
            raise ValueError(
                "Invalid config: database at /var/lib/secret.db"
            )
        except ValueError as e:
            sanitized = sanitize_exception(e)
            assert "/var/lib/secret.db" not in sanitized

    def test_includes_exception_type(self) -> None:
        """Includes exception type for common exceptions."""
        try:
            raise ValueError("Some error")
        except ValueError as e:
            sanitized = sanitize_exception(e)
            assert "ValueError" in sanitized

    def test_omits_unhelpful_types(self) -> None:
        """Omits unhelpful exception types from output."""
        try:
            raise Exception("Generic error")
        except Exception as e:
            sanitized = sanitize_exception(e)
            # Generic exceptions don't include type prefix
            assert sanitized == "Generic error"


class TestCreateUserErrorMessage:
    """Test user-friendly error message creation."""

    def test_creates_database_error_message(self) -> None:
        """Creates appropriate database error message."""
        message = create_user_error_message("database_error")
        assert "Database operation failed" in message

    def test_creates_api_error_message(self) -> None:
        """Creates appropriate API error message."""
        message = create_user_error_message("api_error")
        assert "API request failed" in message

    def test_creates_validation_error_message(self) -> None:
        """Creates appropriate validation error message."""
        message = create_user_error_message("validation_error")
        assert "Invalid input" in message

    def test_adds_public_details(self) -> None:
        """Includes public details in message."""
        message = create_user_error_message(
            "validation_error", public_details="Email format is invalid"
        )
        assert "Email format is invalid" in message

    def test_logs_internal_error(self, caplog) -> None:
        """Logs internal error when provided."""
        exc = ValueError("Secret database path: /secret/path.db")
        create_user_error_message("api_error", internal_error=exc)
        assert "Internal error" in caplog.text

    def test_unknown_error_type_fallback(self) -> None:
        """Falls back to generic message for unknown error types."""
        message = create_user_error_message("unknown_type")
        assert "An unexpected error occurred" in message

    def test_all_error_types_have_messages(self) -> None:
        """All documented error types have friendly messages."""
        error_types = [
            "database_error",
            "api_error",
            "validation_error",
            "authentication_error",
            "authorization_error",
            "rate_limit_error",
            "timeout_error",
        ]
        for error_type in error_types:
            message = create_user_error_message(error_type)
            # Each should produce a non-generic message
            assert message
            assert "An unexpected error" not in message or error_type == "unknown_error"


class TestSpecializedSanitization:
    """Test specialized sanitization functions."""

    def test_sanitizes_database_error(self) -> None:
        """Sanitizes database errors appropriately."""
        exc = Exception(
            "Connection failed to postgres://user:pass@db.local/mydb"
        )
        message = sanitize_database_error(exc)
        assert "Database operation failed" in message
        assert "postgres://" not in message

    def test_sanitizes_api_error(self) -> None:
        """Sanitizes API errors appropriately."""
        exc = Exception("API call to https://key:secret@api.local/v1 failed")
        message = sanitize_api_error(exc)
        assert "API request failed" in message
        assert "key:secret" not in message

    def test_includes_http_status_when_available(self) -> None:
        """Includes HTTP status code when available."""
        exc = Exception("API error")
        exc.status_code = 500  # type: ignore
        message = sanitize_api_error(exc, include_status=True)
        assert "(HTTP 500)" in message

    def test_skips_http_status_when_not_requested(self) -> None:
        """Doesn't include HTTP status unless requested."""
        exc = Exception("API error")
        exc.status_code = 404  # type: ignore
        message = sanitize_api_error(exc, include_status=False)
        assert "(HTTP" not in message
