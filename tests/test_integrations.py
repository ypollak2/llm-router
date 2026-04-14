"""Tests for enterprise integrations: Helicone, LiteLLM BudgetManager."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


# ── Helicone ──────────────────────────────────────────────────────────────────

class TestHeliconeHeaders:
    def test_returns_empty_without_api_key(self, monkeypatch):
        monkeypatch.delenv("HELICONE_API_KEY", raising=False)
        from llm_router.integrations.helicone import get_helicone_headers
        assert get_helicone_headers(task_type="code", model="gpt-4o") == {}

    def test_returns_auth_header_with_key(self, monkeypatch):
        monkeypatch.setenv("HELICONE_API_KEY", "sk-helicone-test")
        from llm_router.integrations.helicone import get_helicone_headers
        headers = get_helicone_headers()
        assert headers["Helicone-Auth"] == "Bearer sk-helicone-test"

    def test_includes_routing_properties(self, monkeypatch):
        monkeypatch.setenv("HELICONE_API_KEY", "sk-helicone-test")
        from llm_router.integrations.helicone import get_helicone_headers
        headers = get_helicone_headers(
            task_type="code", model="ollama/qwen3:32b",
            complexity="moderate", profile="balanced",
        )
        assert headers["Helicone-Property-Router-Task-Type"] == "code"
        assert headers["Helicone-Property-Router-Model"] == "ollama/qwen3:32b"
        assert headers["Helicone-Property-Router-Complexity"] == "moderate"
        assert headers["Helicone-Property-Router-Profile"] == "balanced"

    def test_omits_empty_properties(self, monkeypatch):
        monkeypatch.setenv("HELICONE_API_KEY", "sk-helicone-test")
        from llm_router.integrations.helicone import get_helicone_headers
        headers = get_helicone_headers()
        assert "Helicone-Property-Router-Task-Type" not in headers
        assert "Helicone-Property-Router-Model" not in headers

    def test_is_helicone_enabled_true(self, monkeypatch):
        monkeypatch.setenv("HELICONE_API_KEY", "sk-test")
        from llm_router.integrations.helicone import is_helicone_enabled
        assert is_helicone_enabled() is True

    def test_is_helicone_enabled_false(self, monkeypatch):
        monkeypatch.delenv("HELICONE_API_KEY", raising=False)
        from llm_router.integrations.helicone import is_helicone_enabled
        assert is_helicone_enabled() is False


class TestHeliconeSpendPull:
    @pytest.mark.asyncio
    async def test_returns_empty_without_key(self, monkeypatch):
        monkeypatch.delenv("HELICONE_API_KEY", raising=False)
        from llm_router.integrations.helicone import get_helicone_spend
        result = await get_helicone_spend()
        assert result == {}

    @pytest.mark.asyncio
    async def test_returns_empty_when_pull_disabled(self, monkeypatch):
        monkeypatch.setenv("HELICONE_API_KEY", "sk-test")
        monkeypatch.delenv("LLM_ROUTER_HELICONE_PULL", raising=False)
        from llm_router.integrations.helicone import get_helicone_spend
        result = await get_helicone_spend()
        assert result == {}

    @pytest.mark.asyncio
    async def test_parses_helicone_response(self, monkeypatch):
        monkeypatch.setenv("HELICONE_API_KEY", "sk-test")
        monkeypatch.setenv("LLM_ROUTER_HELICONE_PULL", "true")

        fake_response = json.dumps({
            "data": [
                {"provider": "openai", "cost": 4.21},
                {"provider": "gemini", "cost": 0.87},
            ]
        }).encode()

        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = fake_response

        with patch("urllib.request.urlopen", return_value=mock_resp):
            from llm_router.integrations import helicone as _hm
            import importlib
            importlib.reload(_hm)
            result = await _hm.get_helicone_spend()

        assert result.get("openai", 0) == pytest.approx(4.21)
        assert result.get("gemini", 0) == pytest.approx(0.87)

    @pytest.mark.asyncio
    async def test_returns_empty_on_network_error(self, monkeypatch):
        monkeypatch.setenv("HELICONE_API_KEY", "sk-test")
        monkeypatch.setenv("LLM_ROUTER_HELICONE_PULL", "true")
        with patch("urllib.request.urlopen", side_effect=OSError("timeout")):
            from llm_router.integrations.helicone import get_helicone_spend
            result = await get_helicone_spend()
        assert result == {}


# ── LiteLLM BudgetManager ─────────────────────────────────────────────────────

class TestLiteLLMBudget:
    def test_not_enabled_without_db_path(self, monkeypatch):
        monkeypatch.delenv("LLM_ROUTER_LITELLM_BUDGET_DB", raising=False)
        from llm_router.integrations.litellm_budget import is_litellm_budget_enabled
        assert is_litellm_budget_enabled() is False

    def test_not_enabled_when_path_missing(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LLM_ROUTER_LITELLM_BUDGET_DB", str(tmp_path / "nonexistent.db"))
        from llm_router.integrations.litellm_budget import is_litellm_budget_enabled
        assert is_litellm_budget_enabled() is False

    def test_enabled_when_db_exists(self, monkeypatch, tmp_path):
        db = tmp_path / "litellm.db"
        db.touch()
        monkeypatch.setenv("LLM_ROUTER_LITELLM_BUDGET_DB", str(db))
        from llm_router.integrations.litellm_budget import is_litellm_budget_enabled
        assert is_litellm_budget_enabled() is True

    @pytest.mark.asyncio
    async def test_returns_empty_without_config(self, monkeypatch):
        monkeypatch.delenv("LLM_ROUTER_LITELLM_BUDGET_DB", raising=False)
        from llm_router.integrations.litellm_budget import get_litellm_spend
        result = await get_litellm_spend()
        assert result == {}

    @pytest.mark.asyncio
    async def test_returns_empty_on_missing_db(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LLM_ROUTER_LITELLM_BUDGET_DB", str(tmp_path / "missing.db"))
        from llm_router.integrations.litellm_budget import get_litellm_spend
        result = await get_litellm_spend()
        assert result == {}

    @pytest.mark.asyncio
    async def test_queries_spend_logs(self, monkeypatch, tmp_path):
        """With a real SQLite DB, get_litellm_spend should aggregate by provider."""
        import aiosqlite
        db_path = tmp_path / "litellm.db"

        # Create minimal LiteLLM-style spend_logs table
        async with aiosqlite.connect(db_path) as db:
            await db.execute("""
                CREATE TABLE spend_logs (
                    user TEXT, model TEXT, spend REAL,
                    startTime TEXT, endTime TEXT
                )
            """)
            await db.execute("""
                INSERT INTO spend_logs VALUES
                ('user1', 'openai/gpt-4o', 2.50, datetime('now'), datetime('now')),
                ('user1', 'openai/gpt-4o-mini', 0.30, datetime('now'), datetime('now')),
                ('user1', 'gemini/gemini-2.0-flash', 0.75, datetime('now'), datetime('now'))
            """)
            await db.commit()

        monkeypatch.setenv("LLM_ROUTER_LITELLM_BUDGET_DB", str(db_path))
        from llm_router.integrations.litellm_budget import get_litellm_spend
        result = await get_litellm_spend()

        assert result.get("openai", 0) == pytest.approx(2.80, abs=0.01)
        assert result.get("gemini", 0) == pytest.approx(0.75, abs=0.01)

    @pytest.mark.asyncio
    async def test_returns_empty_on_db_error(self, monkeypatch, tmp_path):
        """When DB exists but is corrupt, should return empty dict not raise."""
        db_path = tmp_path / "corrupt.db"
        db_path.write_bytes(b"this is not a sqlite database")
        monkeypatch.setenv("LLM_ROUTER_LITELLM_BUDGET_DB", str(db_path))
        from llm_router.integrations.litellm_budget import get_litellm_spend
        result = await get_litellm_spend()
        assert result == {}
