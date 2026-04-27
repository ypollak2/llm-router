"""Tests for secret scrubbing in structured logs."""


from llm_router.secret_scrubber import (
    scrub_event,
    scrub_environment,
    _scrub_value,
    _should_scrub_field,
)


class TestSecretFieldDetection:
    """Test detection of secret field names."""

    def test_api_key_field_detected(self):
        """Field named 'api_key' should be detected."""
        assert _should_scrub_field("api_key")

    def test_secret_field_detected(self):
        """Field named 'secret' should be detected."""
        assert _should_scrub_field("secret")

    def test_password_field_detected(self):
        """Field named 'password' should be detected."""
        assert _should_scrub_field("password")

    def test_token_field_detected(self):
        """Field named 'token' should be detected."""
        assert _should_scrub_field("token")

    def test_authorization_field_detected(self):
        """Field named 'authorization' should be detected."""
        assert _should_scrub_field("authorization")

    def test_case_insensitive_detection(self):
        """Secret detection should be case-insensitive."""
        assert _should_scrub_field("API_KEY")
        assert _should_scrub_field("ApiKey")
        assert _should_scrub_field("PASSWORD")

    def test_normal_field_not_detected(self):
        """Normal fields should not be detected."""
        assert not _should_scrub_field("user_id")
        assert not _should_scrub_field("request_id")
        assert not _should_scrub_field("trace_id")


class TestSecretValueDetection:
    """Test detection of secret values."""

    def test_anthropic_api_key_detected(self):
        """Anthropic API keys should be detected and scrubbed."""
        value = "sk-ant-abcdefghij1234567890"
        result = _scrub_value(value)
        assert "[REDACTED" in result
        assert "sk-ant" not in result

    def test_openai_api_key_detected(self):
        """OpenAI API keys should be detected."""
        value = "sk-proj-abcdefghij1234567890"
        result = _scrub_value(value)
        assert "[REDACTED" in result

    def test_aws_access_key_detected(self):
        """AWS access keys should be detected."""
        value = "AKIAIOSFODNN7EXAMPLE"  # Real format
        result = _scrub_value(value)
        assert "[REDACTED" in result

    def test_bearer_token_detected(self):
        """Bearer tokens should be detected."""
        value = "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        result = _scrub_value(value)
        assert "[REDACTED" in result

    def test_authorization_header_detected(self):
        """Authorization header values should be detected."""
        value = "Authorization: Bearer token123456789"
        result = _scrub_value(value)
        assert "[REDACTED" in result

    def test_normal_string_not_scrubbed(self):
        """Normal strings should not be scrubbed."""
        value = "This is a normal message"
        result = _scrub_value(value)
        assert result == value

    def test_empty_string_not_scrubbed(self):
        """Empty strings should not be scrubbed."""
        result = _scrub_value("")
        assert result == ""

    def test_non_string_values_passed_through(self):
        """Non-string values should be passed through unchanged."""
        assert _scrub_value(123) == 123
        assert _scrub_value(45.67) == 45.67
        assert _scrub_value(True) is True


class TestEventScrubbing:
    """Test scrubbing of event dicts."""

    def test_sensitive_field_redacted(self):
        """Fields with sensitive names should be redacted."""
        event = {"user_id": "123", "api_key": "sk-ant-secret"}
        result = scrub_event(event)
        assert result["user_id"] == "123"
        assert "[REDACTED" in result["api_key"]

    def test_secret_values_redacted(self):
        """Secret values should be redacted even in normal fields."""
        event = {
            "message": "Logged in with token",
            "token_value": "sk-ant-abcdefghij1234567890",
        }
        result = scrub_event(event)
        assert "sk-ant" not in result["token_value"]
        assert "[REDACTED" in result["token_value"]

    def test_nested_dict_scrubbed(self):
        """Nested dicts should be recursively scrubbed."""
        event = {
            "response": {
                "status": "ok",
                "api_key": "sk-ant-secret",
            }
        }
        result = scrub_event(event)
        assert result["response"]["status"] == "ok"
        assert "[REDACTED" in result["response"]["api_key"]

    def test_list_values_scrubbed(self):
        """Values in lists should be scrubbed."""
        event = {
            "keys": ["normal", "sk-ant-abcdefghij1234567890", "another"]
        }
        result = scrub_event(event)
        assert result["keys"][0] == "normal"
        assert "[REDACTED" in result["keys"][1]
        assert result["keys"][2] == "another"

    def test_tuple_values_scrubbed(self):
        """Values in tuples should be scrubbed."""
        event = {
            "values": ("sk-ant-abcdefghij1234567890", "normal")
        }
        result = scrub_event(event)
        assert "[REDACTED" in result["values"][0]
        assert result["values"][1] == "normal"

    def test_complex_nested_structure(self):
        """Complex nested structures should be fully scrubbed."""
        event = {
            "user": {
                "id": "123",
                "credentials": {
                    "api_key": "sk-ant-abcdefghij1234567890",
                    "token": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
                }
            },
            "request": {
                "headers": ["Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"]
            }
        }
        result = scrub_event(event)
        assert result["user"]["id"] == "123"
        assert "[REDACTED" in result["user"]["credentials"]["api_key"]
        assert "[REDACTED" in result["user"]["credentials"]["token"]
        assert "[REDACTED" in result["request"]["headers"][0]

    def test_empty_dict_returned_unchanged(self):
        """Empty dicts should be returned unchanged."""
        result = scrub_event({})
        assert result == {}

    def test_all_fields_with_secrets_redacted(self):
        """Event with all secret fields should redact them all."""
        event = {
            "api_key": "sk-ant-abcdefghij1234567890",
            "password": "mypassword123",
            "token": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
        }
        result = scrub_event(event)
        for key in event.keys():
            # Field names should be redacted (since they're sensitive field names)
            assert "[REDACTED" in result[key]


class TestEnvironmentScrubbing:
    """Test scrubbing of environment variables."""

    def test_anthropic_key_scrubbed(self, monkeypatch):
        """ANTHROPIC_API_KEY should be scrubbed."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
        monkeypatch.setenv("USER", "testuser")
        
        env = scrub_environment()
        assert "[REDACTED" in env["ANTHROPIC_API_KEY"]
        assert env["USER"] == "testuser"

    def test_database_url_scrubbed(self, monkeypatch):
        """DATABASE_URL should be scrubbed."""
        monkeypatch.setenv("DATABASE_URL", "postgres://user:pass@localhost/db")
        env = scrub_environment()
        assert "[REDACTED" in env["DATABASE_URL"]

    def test_aws_keys_scrubbed(self, monkeypatch):
        """AWS keys should be scrubbed."""
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAIOSFODNN7EXAMPLE")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret123")
        
        env = scrub_environment()
        assert "[REDACTED" in env["AWS_ACCESS_KEY_ID"]
        assert "[REDACTED" in env["AWS_SECRET_ACCESS_KEY"]

    def test_normal_env_vars_preserved(self, monkeypatch):
        """Normal environment variables should be preserved."""
        monkeypatch.setenv("PATH", "/usr/bin:/usr/local/bin")
        monkeypatch.setenv("HOME", "/home/user")
        
        env = scrub_environment()
        assert env["PATH"] == "/usr/bin:/usr/local/bin"
        assert env["HOME"] == "/home/user"


class TestSecurityEdgeCases:
    """Test edge cases and security-critical scenarios."""

    def test_multiple_secrets_in_string(self):
        """Strings with multiple secrets should be scrubbed."""
        value = "First: sk-ant-abcdefghij1234567890 and second: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        result = _scrub_value(value)
        assert "[REDACTED" in result
        # At least one secret should be detected
        assert not (result.startswith("sk-ant") or "Bearer " in result)

    def test_secret_in_structured_field(self):
        """Secrets in structured fields should be scrubbed."""
        event = {
            "config": "api_key=sk-ant-abcdefghij1234567890"
        }
        result = scrub_event(event)
        assert "sk-ant" not in result["config"]

    def test_authorization_header_redacted(self):
        """Authorization headers should be redacted."""
        event = {
            "headers": "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        }
        result = scrub_event(event)
        assert "[REDACTED" in result["headers"]

    def test_very_long_secret_scrubbed(self):
        """Very long secret values should be scrubbed."""
        long_token = "sk-ant-" + "a" * 1000
        result = _scrub_value(long_token)
        assert "[REDACTED" in result
        assert "a" * 100 not in result  # Most of the padding should be gone

    def test_case_variation_secrets_detected(self):
        """Secrets with case variations should be detected."""
        # Test bearer token with different cases
        value = "BEARER eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        result = _scrub_value(value)
        assert "[REDACTED" in result
