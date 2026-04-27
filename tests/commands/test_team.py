"""Tests for the team command."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch


from llm_router.commands.team import cmd_team, _run_team, _run_team_setup


class TestCmdTeam:
    """Tests for cmd_team entry point."""

    def test_cmd_team_returns_zero(self):
        """cmd_team should return 0."""
        with patch("llm_router.commands.team._run_team"):
            result = cmd_team([])
        assert result == 0

    def test_cmd_team_with_report_subcommand(self):
        """cmd_team with 'report' should call _run_team."""
        with patch("llm_router.commands.team._run_team") as mock_run:
            cmd_team(["report"])
        mock_run.assert_called_once_with("report", [])

    def test_cmd_team_with_push_subcommand(self):
        """cmd_team with 'push' should call _run_team."""
        with patch("llm_router.commands.team._run_team") as mock_run:
            cmd_team(["push"])
        mock_run.assert_called_once_with("push", [])

    def test_cmd_team_with_setup_subcommand(self):
        """cmd_team with 'setup' should call _run_team."""
        with patch("llm_router.commands.team._run_team") as mock_run:
            cmd_team(["setup"])
        mock_run.assert_called_once_with("setup", [])

    def test_cmd_team_no_args_defaults_to_report(self):
        """cmd_team with no args should call _run_team with 'report'."""
        with patch("llm_router.commands.team._run_team") as mock_run:
            cmd_team([])
        mock_run.assert_called_once_with("report", [])

    def test_cmd_team_with_period_flag(self):
        """cmd_team with period should pass it as flag."""
        with patch("llm_router.commands.team._run_team") as mock_run:
            cmd_team(["report", "month"])
        mock_run.assert_called_once_with("report", ["month"])


class TestTeamReport:
    """Tests for team report functionality."""

    def test_team_report_displays_header(self, capsys):
        """team report should display header with user and project."""
        with patch("llm_router.team.build_team_report") as mock_report:
            with patch("llm_router.team.get_user_id") as mock_user:
                with patch("llm_router.team.get_project_id") as mock_proj:
                    with patch("llm_router.config.get_config") as mock_cfg:
                        report = {
                            "total_calls": 0,
                            "saved_usd": 0.0,
                            "actual_usd": 0.0,
                            "free_pct": 0.0,
                            "top_models": [],
                        }
                        mock_report.return_value = report
                        mock_user.return_value = "user@example.com"
                        mock_proj.return_value = "my-project"
                        mock_cfg.return_value = MagicMock(
                            llm_router_user_id=None,
                            llm_router_team_endpoint=None,
                            llm_router_team_chat_id=None,
                        )
                        _run_team("report", [])

        captured = capsys.readouterr()
        assert "[llm-router] Team Report" in captured.out
        assert "user@example.com" in captured.out
        assert "my-project" in captured.out

    def test_team_report_no_data(self, capsys):
        """team report with no data should display message."""
        with patch("llm_router.team.build_team_report") as mock_report:
            with patch("llm_router.team.get_user_id") as mock_user:
                with patch("llm_router.team.get_project_id") as mock_proj:
                    with patch("llm_router.config.get_config") as mock_cfg:
                        report = {
                            "total_calls": 0,
                            "saved_usd": 0.0,
                            "actual_usd": 0.0,
                            "free_pct": 0.0,
                        }
                        mock_report.return_value = report
                        mock_user.return_value = "user@example.com"
                        mock_proj.return_value = "my-project"
                        mock_cfg.return_value = MagicMock(
                            llm_router_user_id=None,
                            llm_router_team_endpoint=None,
                            llm_router_team_chat_id=None,
                        )
                        _run_team("report", [])

        captured = capsys.readouterr()
        assert "No routing data found" in captured.out

    def test_team_report_with_data(self, capsys):
        """team report with data should display stats."""
        with patch("llm_router.team.build_team_report") as mock_report:
            with patch("llm_router.team.get_user_id") as mock_user:
                with patch("llm_router.team.get_project_id") as mock_proj:
                    with patch("llm_router.config.get_config") as mock_cfg:
                        report = {
                            "total_calls": 100,
                            "saved_usd": 2.5,
                            "actual_usd": 0.5,
                            "free_pct": 0.8,
                            "top_models": [
                                {"model": "openai/gpt-4o", "calls": 50, "cost": 0.40},
                                {"model": "ollama/gemma", "calls": 30, "cost": 0.0},
                            ],
                        }
                        mock_report.return_value = report
                        mock_user.return_value = "user@example.com"
                        mock_proj.return_value = "my-project"
                        mock_cfg.return_value = MagicMock(
                            llm_router_user_id=None,
                            llm_router_team_endpoint=None,
                            llm_router_team_chat_id=None,
                        )
                        _run_team("report", [])

        captured = capsys.readouterr()
        assert "Calls:     100" in captured.out
        assert "$2.5" in captured.out or "2.5000" in captured.out
        assert "80%" in captured.out


class TestTeamSetup:
    """Tests for team setup functionality."""

    def test_team_setup_skip(self, monkeypatch):
        """team setup with skip choice should exit gracefully."""
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            monkeypatch.setattr(Path, "home", lambda: home)

            # Mock input to choose skip
            monkeypatch.setattr("builtins.input", lambda _: "5")

            config = MagicMock()
            _run_team_setup(config)

            # Should not create any files
            assert not (home / ".llm-router" / "routing.yaml").exists()

    def test_team_setup_with_url(self, monkeypatch):
        """team setup with URL should save configuration."""
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            monkeypatch.setattr(Path, "home", lambda: home)

            # Mock inputs: choice 1 (Slack), URL
            inputs = iter(["1", "https://hooks.slack.com/services/T000/B000/XX"])
            monkeypatch.setattr("builtins.input", lambda _: next(inputs))

            config = MagicMock()
            _run_team_setup(config)

            # Should create routing.yaml with endpoint
            routing_yaml = home / ".llm-router" / "routing.yaml"
            assert routing_yaml.exists()
            content = routing_yaml.read_text()
            assert "team_endpoint: https://hooks.slack.com" in content

    def test_team_setup_telegram_with_chat_id(self, monkeypatch):
        """team setup with Telegram should save chat ID."""
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            monkeypatch.setattr(Path, "home", lambda: home)

            # Mock inputs: choice 3 (Telegram), URL, chat ID
            inputs = iter(["3", "https://api.telegram.org/bot123/sendMessage", "-1001234567890"])
            monkeypatch.setattr("builtins.input", lambda _: next(inputs))

            config = MagicMock()
            _run_team_setup(config)

            routing_yaml = home / ".llm-router" / "routing.yaml"
            content = routing_yaml.read_text()
            assert "team_endpoint:" in content
            assert "team_chat_id: -1001234567890" in content


class TestTeamIntegration:
    """Integration tests for team command."""

    def test_team_report_with_period(self, capsys):
        """team report should accept period parameter."""
        with patch("llm_router.team.build_team_report") as mock_report:
            with patch("llm_router.team.get_user_id") as mock_user:
                with patch("llm_router.team.get_project_id") as mock_proj:
                    with patch("llm_router.config.get_config") as mock_cfg:
                        report = {
                            "total_calls": 0,
                            "saved_usd": 0.0,
                            "actual_usd": 0.0,
                            "free_pct": 0.0,
                        }
                        mock_report.return_value = report
                        mock_user.return_value = "user@example.com"
                        mock_proj.return_value = "my-project"
                        mock_cfg.return_value = MagicMock(
                            llm_router_user_id=None,
                            llm_router_team_endpoint=None,
                            llm_router_team_chat_id=None,
                        )
                        _run_team("report", ["month"])

        # Verify the period was passed to build_team_report
        mock_report.assert_called_once()
        call_kwargs = mock_report.call_args[1]
        assert call_kwargs["period"] == "month"

    def test_cmd_team_command_basic(self):
        """cmd_team should execute and return 0."""
        with patch("llm_router.commands.team._run_team"):
            result = cmd_team(["report"])
        assert result == 0
