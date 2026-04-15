"""Tests for llm_setup tool — API provider discovery and configuration."""

import pytest


class TestSetupStatus:
    @pytest.mark.asyncio
    async def test_status_shows_configured_count(self, mock_env):
        from llm_router.server import llm_setup
        result = await llm_setup(action="status")
        assert "API Provider Status" in result
        assert "providers configured" in result

    @pytest.mark.asyncio
    async def test_status_shows_recommended(self, minimal_env):
        from llm_router.server import llm_setup
        result = await llm_setup(action="status")
        assert "Recommended to Add" in result


class TestSetupGuide:
    @pytest.mark.asyncio
    async def test_guide_shows_steps(self, mock_env):
        from llm_router.server import llm_setup
        result = await llm_setup(action="guide")
        assert "Quick Start Guide" in result
        assert "Gemini" in result
        assert "Groq" in result
        assert "Security Notes" in result


class TestSetupDiscover:
    @pytest.mark.asyncio
    async def test_discover_finds_env_keys(self, mock_env, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-1234567890abcdef")
        from llm_router.server import llm_setup
        result = await llm_setup(action="discover")
        assert "API Key Discovery" in result
        assert "openai" in result
        # Key should be masked
        assert "sk-test-1234567890abcdef" not in result

    @pytest.mark.asyncio
    async def test_discover_masks_keys(self, mock_env):
        from llm_router.server import _mask_key
        assert _mask_key("sk-proj-abc123456789xyz") == "sk-p***9xyz"
        assert _mask_key("short") == "sho***rt"


class TestSetupProviderDetail:
    @pytest.mark.asyncio
    async def test_provider_detail(self, mock_env):
        from llm_router.server import llm_setup
        result = await llm_setup(action="provider", provider="gemini")
        assert "Google Gemini" in result
        assert "Imagen 3" in result
        assert "Veo 2" in result

    @pytest.mark.asyncio
    async def test_unknown_provider(self, mock_env):
        from llm_router.server import llm_setup
        result = await llm_setup(action="provider", provider="nonexistent")
        assert "Unknown provider" in result


class TestSetupAdd:
    @pytest.mark.asyncio
    async def test_add_without_key_shows_instructions(self, mock_env):
        from llm_router.server import llm_setup
        result = await llm_setup(action="add", provider="gemini")
        assert "Sign up at" in result
        assert "aistudio.google.com" in result

    @pytest.mark.asyncio
    async def test_add_rejects_short_key(self, mock_env):
        from llm_router.server import llm_setup
        result = await llm_setup(action="add", provider="gemini", api_key="abc")
        assert "too short" in result

    @pytest.mark.asyncio
    async def test_add_rejects_whitespace_key(self, mock_env):
        from llm_router.server import llm_setup
        result = await llm_setup(action="add", provider="gemini", api_key="key with spaces")
        assert "whitespace" in result

    @pytest.mark.asyncio
    async def test_add_writes_to_env(self, mock_env, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("GEMINI_API_KEY=\n")
        monkeypatch.chdir(tmp_path)

        from llm_router.server import llm_setup
        result = await llm_setup(
            action="add", provider="gemini",
            api_key="AIzaSy-test-key-1234567890"
        )
        assert "Added" in result
        assert "Google Gemini" in result

        content = env_file.read_text()
        assert "AIzaSy-test-key-1234567890" in content


class TestSetupInvalidAction:
    @pytest.mark.asyncio
    async def test_invalid_action(self, mock_env):
        from llm_router.server import llm_setup
        result = await llm_setup(action="invalid")
        assert "Unknown action" in result
