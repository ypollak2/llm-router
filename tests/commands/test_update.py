"""Tests for the update command."""

from __future__ import annotations

import importlib.metadata
import json
from unittest.mock import MagicMock, patch


from llm_router.commands.update import cmd_update, _run_update


class TestCmdUpdate:
    """Tests for cmd_update entry point."""

    def test_cmd_update_returns_zero(self):
        """cmd_update should return 0."""
        with patch("llm_router.commands.update._run_update"):
            result = cmd_update([])
        assert result == 0

    def test_cmd_update_ignores_args(self):
        """cmd_update should ignore arguments."""
        with patch("llm_router.commands.update._run_update") as mock_run:
            cmd_update(["ignored", "args"])
        mock_run.assert_called_once()


class TestUpdateCommand:
    """Tests for update functionality."""

    def test_run_update_calls_install(self, capsys):
        """_run_update should call install with force=True."""
        with patch("llm_router.install_hooks.install") as mock_install:
            mock_install.return_value = []
            with patch("importlib.metadata.version", return_value="1.0.0"):
                with patch("urllib.request.urlopen") as mock_urlopen:
                    mock_response = MagicMock()
                    mock_response.__enter__.return_value.read.return_value = json.dumps({
                        "info": {"version": "1.0.0"}
                    }).encode()
                    mock_urlopen.return_value = mock_response
                    _run_update()
        mock_install.assert_called_once_with(force=True)

    def test_run_update_displays_header(self, capsys):
        """_run_update should display update header."""
        with patch("llm_router.install_hooks.install", return_value=[]):
            with patch("importlib.metadata.version", return_value="1.0.0"):
                with patch("urllib.request.urlopen") as mock_urlopen:
                    mock_response = MagicMock()
                    mock_response.__enter__.return_value.read.return_value = json.dumps({
                        "info": {"version": "1.0.0"}
                    }).encode()
                    mock_urlopen.return_value = mock_response
                    _run_update()
        captured = capsys.readouterr()
        assert "llm-router update" in captured.out
        assert "Hooks & rules" in captured.out

    def test_run_update_handles_empty_install_actions(self, capsys):
        """_run_update should handle no install actions."""
        with patch("llm_router.install_hooks.install", return_value=[]):
            with patch("importlib.metadata.version", return_value="1.0.0"):
                with patch("urllib.request.urlopen") as mock_urlopen:
                    mock_response = MagicMock()
                    mock_response.__enter__.return_value.read.return_value = json.dumps({
                        "info": {"version": "1.0.0"}
                    }).encode()
                    mock_urlopen.return_value = mock_response
                    _run_update()
        captured = capsys.readouterr()
        assert "up to date" in captured.out

    def test_run_update_displays_updated_actions(self, capsys):
        """_run_update should display updated actions."""
        actions = ["Hook → ~/.claude/hooks/auto-route.py", "Updated rule", "Registered"]
        with patch("llm_router.install_hooks.install", return_value=actions):
            with patch("importlib.metadata.version", return_value="1.0.0"):
                with patch("urllib.request.urlopen") as mock_urlopen:
                    mock_response = MagicMock()
                    mock_response.__enter__.return_value.read.return_value = json.dumps({
                        "info": {"version": "1.0.0"}
                    }).encode()
                    mock_urlopen.return_value = mock_response
                    _run_update()
        captured = capsys.readouterr()
        # Actions with arrows should be shown
        assert any(a in captured.out for a in actions)

    def test_run_update_checks_version_match(self, capsys):
        """_run_update should show up-to-date message when versions match."""
        with patch("llm_router.install_hooks.install", return_value=[]):
            with patch("importlib.metadata.version", return_value="1.0.0"):
                with patch("urllib.request.urlopen") as mock_urlopen:
                    mock_response = MagicMock()
                    mock_response.__enter__.return_value.read.return_value = json.dumps({
                        "info": {"version": "1.0.0"}
                    }).encode()
                    mock_urlopen.return_value = mock_response
                    _run_update()
        captured = capsys.readouterr()
        assert "up to date" in captured.out

    def test_run_update_shows_available_upgrade(self, capsys):
        """_run_update should show upgrade available message."""
        with patch("llm_router.install_hooks.install", return_value=[]):
            with patch("importlib.metadata.version", return_value="1.0.0"):
                with patch("urllib.request.urlopen") as mock_urlopen:
                    mock_response = MagicMock()
                    mock_response.__enter__.return_value.read.return_value = json.dumps({
                        "info": {"version": "2.0.0"}
                    }).encode()
                    mock_urlopen.return_value = mock_response
                    _run_update()
        captured = capsys.readouterr()
        assert "1.0.0" in captured.out
        assert "2.0.0" in captured.out
        assert "available" in captured.out

    def test_run_update_handles_unknown_current_version(self, capsys):
        """_run_update should handle unknown current version."""
        with patch("llm_router.install_hooks.install", return_value=[]):
            with patch("importlib.metadata.version") as mock_version:
                mock_version.side_effect = importlib.metadata.PackageNotFoundError()
                with patch("urllib.request.urlopen") as mock_urlopen:
                    mock_response = MagicMock()
                    mock_response.__enter__.return_value.read.return_value = json.dumps({
                        "info": {"version": "1.0.0"}
                    }).encode()
                    mock_urlopen.return_value = mock_response
                    _run_update()
        captured = capsys.readouterr()
        assert "unknown" in captured.out or "1.0.0" in captured.out

    def test_run_update_handles_pypi_error(self, capsys):
        """_run_update should handle PyPI check failure."""
        with patch("llm_router.install_hooks.install", return_value=[]):
            with patch("importlib.metadata.version", return_value="1.0.0"):
                with patch("urllib.request.urlopen", side_effect=Exception("Network error")):
                    _run_update()
        captured = capsys.readouterr()
        assert "could not check PyPI" in captured.out or "1.0.0" in captured.out

    def test_run_update_displays_install_command(self, capsys):
        """_run_update should display pip install command for upgrades."""
        with patch("llm_router.install_hooks.install", return_value=[]):
            with patch("importlib.metadata.version", return_value="1.0.0"):
                with patch("urllib.request.urlopen") as mock_urlopen:
                    mock_response = MagicMock()
                    mock_response.__enter__.return_value.read.return_value = json.dumps({
                        "info": {"version": "2.0.0"}
                    }).encode()
                    mock_urlopen.return_value = mock_response
                    _run_update()
        captured = capsys.readouterr()
        assert "pip install --upgrade" in captured.out


class TestUpdateIntegration:
    """Integration tests for update command."""

    def test_cmd_update_basic(self):
        """cmd_update should execute and return 0."""
        with patch("llm_router.commands.update._run_update"):
            result = cmd_update([])
        assert result == 0

    def test_run_update_completes_without_error(self):
        """_run_update should complete without raising exceptions."""
        with patch("llm_router.install_hooks.install", return_value=[]):
            with patch("importlib.metadata.version", return_value="1.0.0"):
                with patch("urllib.request.urlopen") as mock_urlopen:
                    mock_response = MagicMock()
                    mock_response.__enter__.return_value.read.return_value = json.dumps({
                        "info": {"version": "1.0.0"}
                    }).encode()
                    mock_urlopen.return_value = mock_response
                    # Should not raise
                    _run_update()
