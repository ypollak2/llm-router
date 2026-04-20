"""Tests for safe subprocess execution with API key filtering."""

import os
import pytest

from llm_router.safe_subprocess import get_safe_env, _is_sensitive_var


class TestSensitiveVarDetection:
    """Test detection of sensitive environment variable names."""

    def test_detects_api_keys(self):
        """Detects *_API_KEY variables."""
        assert _is_sensitive_var("OPENAI_API_KEY") is True
        assert _is_sensitive_var("GEMINI_API_KEY") is True
        assert _is_sensitive_var("MY_CUSTOM_API_KEY") is True

    def test_detects_api_tokens(self):
        """Detects *_API_TOKEN and *_TOKEN variables."""
        assert _is_sensitive_var("REPLICATE_API_TOKEN") is True
        assert _is_sensitive_var("HF_TOKEN") is True
        assert _is_sensitive_var("CUSTOM_TOKEN") is True

    def test_detects_oauth_tokens(self):
        """Detects *_OAUTH_TOKEN and related variables."""
        assert _is_sensitive_var("CLAUDE_OAUTH_TOKEN") is True

    def test_detects_passwords_and_secrets(self):
        """Detects *PASSWORD* and *SECRET* variables."""
        assert _is_sensitive_var("DATABASE_PASSWORD") is True
        assert _is_sensitive_var("API_SECRET") is True
        assert _is_sensitive_var("SECRET_KEY") is True

    def test_allows_safe_variables(self):
        """Does not flag safe environment variables."""
        assert _is_sensitive_var("PATH") is False
        assert _is_sensitive_var("HOME") is False
        assert _is_sensitive_var("USER") is False
        assert _is_sensitive_var("SHELL") is False
        assert _is_sensitive_var("LANG") is False

    def test_case_insensitive(self):
        """Detection is case-insensitive."""
        assert _is_sensitive_var("openai_api_key") is True
        assert _is_sensitive_var("OpenAI_API_Key") is True


class TestGetSafeEnv:
    """Test environment filtering function."""

    def test_removes_api_keys(self):
        """Removes API key variables from environment."""
        # Add test API keys to os.environ
        os.environ["TEST_OPENAI_API_KEY"] = "sk-test-secret"
        os.environ["SAFE_VAR"] = "public_value"

        try:
            safe_env = get_safe_env()

            # API key should be removed
            assert "TEST_OPENAI_API_KEY" not in safe_env
            # Safe variables should be present
            assert safe_env.get("SAFE_VAR") == "public_value"
        finally:
            # Cleanup
            del os.environ["TEST_OPENAI_API_KEY"]
            del os.environ["SAFE_VAR"]

    def test_preserves_normal_vars(self):
        """Preserves non-secret environment variables."""
        safe_env = get_safe_env()

        # Standard system variables should be present
        assert "PATH" in safe_env
        assert "HOME" in safe_env
        assert "USER" in safe_env

    def test_filters_all_known_secrets(self):
        """Filters all known secret variable patterns."""
        test_vars = {
            "OPENAI_API_KEY": "sk-test",
            "GEMINI_API_KEY": "AIza-test",
            "HF_TOKEN": "hf_test",
            "ANTHROPIC_API_KEY": "sk-ant-test",
            "DATABASE_PASSWORD": "secret",
            "API_SECRET": "secret",
            "MY_CUSTOM_API_TOKEN": "token",
        }

        # Temporarily add test variables
        for key, value in test_vars.items():
            os.environ[key] = value

        try:
            safe_env = get_safe_env()

            # None of the secret variables should be in safe_env
            for key in test_vars:
                assert key not in safe_env, f"{key} should be filtered"
        finally:
            # Cleanup
            for key in test_vars:
                del os.environ[key]

    def test_returns_dict_type(self):
        """Returns a dictionary."""
        safe_env = get_safe_env()
        assert isinstance(safe_env, dict)

    def test_is_copy_not_reference(self):
        """Returned dict is a copy, not a reference to os.environ."""
        safe_env = get_safe_env()

        # Modifying safe_env shouldn't affect os.environ
        os.environ["TEST_VAR"] = "original"
        try:
            safe_env = get_safe_env()
            safe_env["TEST_VAR"] = "modified"

            # Original os.environ should be unchanged
            assert os.environ.get("TEST_VAR") == "original"
        finally:
            del os.environ["TEST_VAR"]


class TestSubprocessSafety:
    """Integration tests for subprocess safety."""

    @pytest.mark.asyncio
    async def test_async_exec_filters_env(self):
        """safe_subprocess_exec filters environment when running commands."""
        from llm_router.safe_subprocess import safe_subprocess_exec

        # Add a test API key
        os.environ["TEST_OPENAI_API_KEY"] = "secret-value"

        try:
            # Run a simple command that prints environment
            # (use a portable approach)
            stdout, stderr, code = await safe_subprocess_exec(
                "python", "-c",
                "import os; print('TEST_OPENAI_API_KEY' in os.environ)",
            )

            # The subprocess should NOT see the API key
            output = stdout.decode().strip()
            assert output == "False", "API key should be filtered from subprocess"
        finally:
            del os.environ["TEST_OPENAI_API_KEY"]

    def test_sync_run_filters_env(self):
        """safe_subprocess_run filters environment when running commands."""
        from llm_router.safe_subprocess import safe_subprocess_run

        # Add a test API key
        os.environ["TEST_ANTHROPIC_API_KEY"] = "secret-value"

        try:
            # Run a simple command that prints environment
            result = safe_subprocess_run(
                "python", "-c",
                "import os; print('TEST_ANTHROPIC_API_KEY' in os.environ)",
                capture_output=True, text=True,
            )

            # The subprocess should NOT see the API key
            output = result.stdout.strip()
            assert output == "False", "API key should be filtered from subprocess"
        finally:
            del os.environ["TEST_ANTHROPIC_API_KEY"]
