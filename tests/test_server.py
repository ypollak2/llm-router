"""Tests for MCP server tool registration and basic functionality."""


import pytest

from llm_router.server import mcp


def test_all_tools_registered():
    tools = mcp._tool_manager.list_tools()
    names = {t.name for t in tools}
    expected = {
        "llm_classify", "llm_track_usage", "llm_route", "llm_stream",
        "llm_query", "llm_research", "llm_generate",
        "llm_analyze", "llm_code", "llm_image", "llm_video", "llm_audio",
        "llm_orchestrate", "llm_pipeline_templates",
        "llm_save_session",
        "llm_set_profile", "llm_usage", "llm_health", "llm_providers",
        "llm_check_usage", "llm_update_usage", "llm_refresh_claude_usage", "llm_codex", "llm_setup",
        "llm_cache_stats", "llm_cache_clear", "llm_quality_report",
        "llm_edit", "llm_rate",
    }
    assert expected == names


def test_resource_registered():
    # FastMCP should have our status resource
    resources = mcp._resource_manager.list_resources()
    uris = [str(r.uri) for r in resources]
    assert any("status" in u for u in uris)


class TestSetProfile:
    @pytest.mark.asyncio
    async def test_valid_profile(self, mock_env):
        from llm_router.server import llm_set_profile
        result = await llm_set_profile("budget")
        assert "budget" in result

    @pytest.mark.asyncio
    async def test_invalid_profile(self, mock_env):
        from llm_router.server import llm_set_profile
        result = await llm_set_profile("invalid")
        assert "Invalid" in result


class TestUsage:
    @pytest.mark.asyncio
    async def test_usage_no_data(self, mock_env, tmp_path, monkeypatch):
        monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(tmp_path / "test.db"))
        from llm_router.server import llm_usage
        result = await llm_usage("all")
        assert "Usage Dashboard" in result


class TestHealth:
    @pytest.mark.asyncio
    async def test_health_shows_providers(self, mock_env):
        from llm_router.server import llm_health
        result = await llm_health()
        assert "Provider Health" in result
