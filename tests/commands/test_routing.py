"""Tests for the routing command."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

# Mock structlog before importing routing
sys.modules["structlog"] = MagicMock()

from llm_router.commands.routing import cmd_routing, _run_routing  # noqa: E402


class TestCmdRouting:
    """Tests for cmd_routing entry point."""

    def test_cmd_routing_returns_zero(self):
        """cmd_routing should return 0."""
        with patch("llm_router.commands.routing._run_routing"):
            result = cmd_routing([])
        assert result == 0

    def test_cmd_routing_ignores_args(self):
        """cmd_routing should ignore arguments."""
        with patch("llm_router.commands.routing._run_routing") as mock_run:
            cmd_routing(["ignored", "args"])
        mock_run.assert_called_once()


class TestRunRouting:
    """Tests for _run_routing functionality."""

    def test_run_routing_displays_header(self, capsys):
        """_run_routing should display main header."""
        with patch("llm_router.codex_agent.is_codex_available", return_value=False):
            with patch("llm_router.gemini_cli_agent.is_gemini_cli_available", return_value=False):
                with patch("llm_router.config.get_config") as mock_cfg:
                    mock_cfg.return_value = MagicMock(openai_api_key=None, gemini_api_key=None, ollama_base_url=None)
                    with patch("llm_router.claude_usage.get_claude_pressure", return_value=0.5):
                        _run_routing()
        captured = capsys.readouterr()
        assert "LLM Router" in captured.out
        assert "Current Routing Configuration" in captured.out

    def test_run_routing_shows_providers_section(self, capsys):
        """_run_routing should show providers section."""
        with patch("llm_router.codex_agent.is_codex_available", return_value=True):
            with patch("llm_router.gemini_cli_agent.is_gemini_cli_available", return_value=False):
                with patch("llm_router.config.get_config") as mock_cfg:
                    mock_cfg.return_value = MagicMock(openai_api_key=None, gemini_api_key=None, ollama_base_url=None)
                    with patch("llm_router.claude_usage.get_claude_pressure", return_value=0.5):
                        _run_routing()
        captured = capsys.readouterr()
        assert "Available Providers" in captured.out
        assert "Codex" in captured.out

    def test_run_routing_shows_claude_quota(self, capsys):
        """_run_routing should display Claude quota pressure."""
        with patch("llm_router.codex_agent.is_codex_available", return_value=False):
            with patch("llm_router.gemini_cli_agent.is_gemini_cli_available", return_value=False):
                with patch("llm_router.config.get_config") as mock_cfg:
                    mock_cfg.return_value = MagicMock(openai_api_key=None, gemini_api_key=None, ollama_base_url=None)
                    with patch("llm_router.claude_usage.get_claude_pressure", return_value=0.5):
                        _run_routing()
        captured = capsys.readouterr()
        assert "Claude Subscription Quota" in captured.out
        assert "Pressure:" in captured.out

    def test_run_routing_shows_routing_chains(self, capsys):
        """_run_routing should show sample routing chains."""
        with patch("llm_router.codex_agent.is_codex_available", return_value=False):
            with patch("llm_router.gemini_cli_agent.is_gemini_cli_available", return_value=False):
                with patch("llm_router.config.get_config") as mock_cfg:
                    mock_cfg.return_value = MagicMock(openai_api_key=None, gemini_api_key=None, ollama_base_url=None)
                    with patch("llm_router.claude_usage.get_claude_pressure", return_value=0.5):
                        _run_routing()
        captured = capsys.readouterr()
        assert "Sample Routing Chains" in captured.out

    def test_run_routing_handles_missing_config(self, capsys):
        """_run_routing should gracefully handle missing config."""
        with patch("llm_router.codex_agent.is_codex_available", return_value=False):
            with patch("llm_router.gemini_cli_agent.is_gemini_cli_available", return_value=False):
                with patch("llm_router.config.get_config", side_effect=Exception("Config error")):
                    with patch("llm_router.claude_usage.get_claude_pressure", return_value=0.5):
                        _run_routing()
        captured = capsys.readouterr()
        # Should not crash even with exception
        assert "LLM Router" in captured.out

    def test_run_routing_handles_missing_pressure(self, capsys):
        """_run_routing should handle missing Claude pressure gracefully."""
        with patch("llm_router.codex_agent.is_codex_available", return_value=False):
            with patch("llm_router.gemini_cli_agent.is_gemini_cli_available", return_value=False):
                with patch("llm_router.config.get_config") as mock_cfg:
                    mock_cfg.return_value = MagicMock(openai_api_key=None, gemini_api_key=None, ollama_base_url=None)
                    with patch("llm_router.claude_usage.get_claude_pressure", side_effect=Exception("Pressure error")):
                        _run_routing()
        captured = capsys.readouterr()
        assert "(unavailable)" in captured.out

    def test_run_routing_displays_pressure_indicators(self, capsys):
        """_run_routing should show correct pressure indicator."""
        with patch("llm_router.codex_agent.is_codex_available", return_value=False):
            with patch("llm_router.gemini_cli_agent.is_gemini_cli_available", return_value=False):
                with patch("llm_router.config.get_config") as mock_cfg:
                    mock_cfg.return_value = MagicMock(openai_api_key=None, gemini_api_key=None, ollama_base_url=None)
                    with patch("llm_router.claude_usage.get_claude_pressure", return_value=0.2):
                        _run_routing()
        captured = capsys.readouterr()
        assert "Available" in captured.out or "Pressure:" in captured.out


class TestRoutingIntegration:
    """Integration tests for routing command."""

    def test_cmd_routing_basic(self):
        """cmd_routing should execute and return 0."""
        with patch("llm_router.commands.routing._run_routing"):
            result = cmd_routing([])
        assert result == 0

    def test_run_routing_completes_without_error(self):
        """_run_routing should complete without raising exceptions."""
        with patch("llm_router.codex_agent.is_codex_available", return_value=False):
            with patch("llm_router.gemini_cli_agent.is_gemini_cli_available", return_value=False):
                with patch("llm_router.config.get_config") as mock_cfg:
                    mock_cfg.return_value = MagicMock(openai_api_key=None, gemini_api_key=None, ollama_base_url=None)
                    with patch("llm_router.claude_usage.get_claude_pressure", return_value=0.5):
                        # Should not raise
                        _run_routing()
