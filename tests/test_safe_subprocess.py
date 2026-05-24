"""Tests for safe_subprocess.py."""

from __future__ import annotations

import os
import pytest


class TestEnvironmentFiltering:
    """Unit tests for safe_subprocess environment filtering."""

    def test_get_safe_env_removes_secrets(self):
        """get_safe_env removes common AI provider secret patterns."""
        from llm_router.safe_subprocess import get_safe_env
        
        # Add test secrets to os.environ
        os.environ["TEST_OPENAI_API_KEY"] = "sk-123"
        os.environ["TEST_ANTHROPIC_API_KEY"] = "sk-ant-456"
        os.environ["TEST_GEMINI_API_KEY"] = "AIza789"
        os.environ["SAFE_PATH_VAR"] = "/usr/bin"
        
        try:
            safe_env = get_safe_env()
            assert "SAFE_PATH_VAR" in safe_env
            assert "TEST_OPENAI_API_KEY" not in safe_env
            assert "TEST_ANTHROPIC_API_KEY" not in safe_env
            assert "TEST_GEMINI_API_KEY" not in safe_env
        finally:
            for key in ["TEST_OPENAI_API_KEY", "TEST_ANTHROPIC_API_KEY", "TEST_GEMINI_API_KEY", "SAFE_PATH_VAR"]:
                if key in os.environ:
                    del os.environ[key]

    def test_is_sensitive_var_case_insensitive(self):
        """_is_sensitive_var is case-insensitive."""
        from llm_router.safe_subprocess import _is_sensitive_var
        assert _is_sensitive_var("openai_api_key") is True
        assert _is_sensitive_var("OPENAI_API_KEY") is True


class TestSubprocessSafety:
    """Integration tests for subprocess safety."""

    @pytest.mark.asyncio
    async def test_async_exec_filters_env(self):
        """safe_subprocess_exec filters environment when running commands."""
        import sys
        from llm_router.safe_subprocess import safe_subprocess_exec

        # Add a test API key
        os.environ["TEST_OPENAI_API_KEY"] = "secret-value"

        try:
            # Run a simple command that prints environment
            # (use a portable approach)
            stdout, stderr, code = await safe_subprocess_exec(
                sys.executable, "-c",
                "import os; print('TEST_OPENAI_API_KEY' in os.environ)",
            )

            # The subprocess should NOT see the API key
            output = stdout.decode().strip()
            assert output == "False", "API key should be filtered from subprocess"
        finally:
            if "TEST_OPENAI_API_KEY" in os.environ:
                del os.environ["TEST_OPENAI_API_KEY"]

    def test_sync_run_filters_env(self):
        """safe_subprocess_run filters environment when running commands."""
        import sys
        from llm_router.safe_subprocess import safe_subprocess_run

        # Add a test API key
        os.environ["TEST_ANTHROPIC_API_KEY"] = "secret-value"

        try:
            # Run a simple command that prints environment
            # Note: safe_subprocess_run uses *args, so we pass them individually
            result = safe_subprocess_run(
                sys.executable, "-c",
                "import os; print('TEST_ANTHROPIC_API_KEY' in os.environ)",
                capture_output=True,
                text=True,
            )

            output = result.stdout.strip()
            assert output == "False", "API key should be filtered from subprocess"
        finally:
            if "TEST_ANTHROPIC_API_KEY" in os.environ:
                del os.environ["TEST_ANTHROPIC_API_KEY"]
