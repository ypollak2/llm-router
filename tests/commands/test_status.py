"""Tests for the status command."""

from __future__ import annotations

from unittest.mock import patch

from llm_router.commands.status import cmd_status


class TestCmdStatus:
    """Tests for cmd_status entry point."""

    def test_cmd_status_no_args(self, capsys):
        """cmd_status with no args should display status and return 0."""
        with patch("os.path.expanduser") as mock_expand:
            mock_expand.return_value = "/fake/home"
            with patch("os.path.exists", return_value=False):
                result = cmd_status([])
        assert result == 0
        captured = capsys.readouterr()
        assert "LLM_ROUTER Status" in captured.out or "Status" in captured.out

    def test_cmd_status_with_invalid_args(self):
        """cmd_status with invalid args should return 0 (ignores extra args)."""
        with patch("os.path.expanduser") as mock_expand:
            mock_expand.return_value = "/fake/home"
            with patch("os.path.exists", return_value=False):
                result = cmd_status(["--invalid-flag"])
        assert result == 0

    def test_cmd_status_displays_title(self, capsys):
        """cmd_status should display the status title."""
        with patch("os.path.expanduser") as mock_expand:
            mock_expand.return_value = "/fake/home"
            with patch("os.path.exists", return_value=False):
                cmd_status([])
        captured = capsys.readouterr()
        assert "LLM_ROUTER Status" in captured.out or "Status" in captured.out

    def test_cmd_status_no_usage_db(self, capsys):
        """cmd_status should display status even when usage.db doesn't exist."""
        with patch("os.path.expanduser") as mock_expand:
            mock_expand.return_value = "/fake/home"
            with patch("os.path.exists", return_value=False):
                cmd_status([])
        captured = capsys.readouterr()
        # Should display status output (the TUI dashboard)
        assert "LLM_ROUTER Status" in captured.out or "Status" in captured.out




class TestStatusIntegration:
    """Integration tests for status command."""

    def test_status_displays_subcommands(self, capsys):
        """status should display subcommands at the end."""
        with patch("os.path.expanduser") as mock_expand:
            mock_expand.return_value = "/fake/home"
            with patch("os.path.exists", return_value=False):
                cmd_status([])
        captured = capsys.readouterr()
        assert "llm_router update" in captured.out
        assert "llm_router doctor" in captured.out
        assert "llm_router dashboard" in captured.out

    def test_status_handles_missing_pressure_data(self, capsys):
        """status should handle missing subscription pressure data gracefully."""
        with patch("os.path.expanduser") as mock_expand:
            mock_expand.return_value = "/fake/home"
            with patch("os.path.exists", return_value=False):
                with patch("builtins.open", side_effect=FileNotFoundError):
                    cmd_status([])
        captured = capsys.readouterr()
        # Should still display status output even with missing data
        assert "LLM_ROUTER Status" in captured.out or "Status" in captured.out
