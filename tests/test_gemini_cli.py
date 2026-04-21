"""Tests for Gemini CLI agent integration."""

from unittest.mock import patch

import pytest

from llm_router.gemini_cli_agent import (
    GEMINI_MODELS,
    GeminiCLIResult,
    find_gemini_binary,
    is_gemini_cli_available,
    run_gemini_cli,
)
from llm_router.gemini_cli_quota import (
    get_gemini_pressure,
    get_gemini_quota_status,
    log_gemini_request,
)


class TestGeminiCLIAgent:
    """Test Gemini CLI agent binary detection and invocation."""

    def test_gemini_models_defined(self):
        """Test that Gemini models list is not empty."""
        assert len(GEMINI_MODELS) > 0
        assert "gemini-2.5-flash" in GEMINI_MODELS

    def test_find_gemini_binary_not_found(self):
        """Test binary detection when not installed."""
        # On most systems, Gemini CLI is not installed
        result = find_gemini_binary()
        # Just check it returns either None or a valid path
        assert result is None or isinstance(result, str)

    def test_is_gemini_cli_available(self):
        """Test availability check works."""
        # Should not raise
        available = is_gemini_cli_available()
        assert isinstance(available, bool)

    @pytest.mark.asyncio
    async def test_run_gemini_cli_not_found(self):
        """Test subprocess call when binary is missing."""
        # Override find_gemini_binary to return None
        with patch("llm_router.gemini_cli_agent.find_gemini_binary", return_value=None):
            result = await run_gemini_cli("test prompt")
            assert isinstance(result, GeminiCLIResult)
            assert result.exit_code == 1
            assert "not found" in result.content.lower()

    @pytest.mark.asyncio
    async def test_gemini_cli_result_dataclass(self):
        """Test GeminiCLIResult dataclass."""
        result = GeminiCLIResult(
            content="test output",
            model="gemini-2.5-flash",
            exit_code=0,
            duration_sec=1.5,
        )
        assert result.success is True
        assert result.content == "test output"
        assert result.model == "gemini-2.5-flash"

    @pytest.mark.asyncio
    async def test_gemini_cli_result_failure(self):
        """Test GeminiCLIResult with non-zero exit code."""
        result = GeminiCLIResult(
            content="error",
            model="gemini-2.5-flash",
            exit_code=1,
            duration_sec=0.5,
        )
        assert result.success is False


class TestGeminiCLIQuota:
    """Test Gemini CLI quota tracking."""

    @pytest.mark.asyncio
    async def test_get_gemini_pressure_default(self):
        """Test quota pressure returns 0.0-1.0."""
        pressure = await get_gemini_pressure()
        assert isinstance(pressure, float)
        assert 0.0 <= pressure <= 1.0

    @pytest.mark.asyncio
    async def test_get_gemini_quota_status_structure(self):
        """Test quota status dict has required keys."""
        status = await get_gemini_quota_status()
        assert isinstance(status, dict)
        assert "count" in status
        assert "daily_limit" in status
        assert "tier" in status
        assert "auth_method" in status
        assert "pressure" in status

    def test_log_gemini_request(self):
        """Test logging a request (no-op in test)."""
        # Should not raise
        log_gemini_request()


class TestGeminiCLIIntegration:
    """Integration tests with router."""

    @pytest.mark.asyncio
    async def test_gemini_models_in_routing(self):
        """Test Gemini models can be imported by router."""
        from llm_router.router import GEMINI_MODELS as router_gemini_models
        from llm_router.gemini_cli_agent import GEMINI_MODELS as agent_gemini_models

        assert router_gemini_models == agent_gemini_models

    def test_gemini_cli_in_profiles(self):
        """Test Gemini CLI models are in free models list."""
        from llm_router.profiles import _FREE_EXTERNAL_MODELS

        # Check at least one Gemini CLI model is marked as free
        gemini_models = [m for m in _FREE_EXTERNAL_MODELS if "gemini_cli" in m]
        assert len(gemini_models) > 0

    @pytest.mark.asyncio
    async def test_gemini_cli_tool_registration(self):
        """Test Gemini CLI tool can be imported."""
        from llm_router.tools.gemini_cli import llm_gemini

        assert callable(llm_gemini)
