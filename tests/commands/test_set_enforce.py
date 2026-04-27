"""Tests for the set-enforce command."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch


from llm_router.commands.set_enforce import cmd_set_enforce, _run_set_enforce, _ENFORCE_MODES


class TestCmdSetEnforce:
    """Tests for cmd_set_enforce entry point."""

    def test_cmd_set_enforce_returns_zero(self):
        """cmd_set_enforce should return 0."""
        with patch("llm_router.commands.set_enforce._run_set_enforce"):
            result = cmd_set_enforce([])
        assert result == 0

    def test_cmd_set_enforce_with_mode(self):
        """cmd_set_enforce with mode should call _run_set_enforce."""
        with patch("llm_router.commands.set_enforce._run_set_enforce") as mock_run:
            cmd_set_enforce(["soft"])
        mock_run.assert_called_once_with("soft")

    def test_cmd_set_enforce_no_args(self):
        """cmd_set_enforce with no args should call _run_set_enforce with empty string."""
        with patch("llm_router.commands.set_enforce._run_set_enforce") as mock_run:
            cmd_set_enforce([])
        mock_run.assert_called_once_with("")


class TestSetEnforceCommand:
    """Tests for set-enforce functionality."""

    def test_set_enforce_invalid_mode_shows_help(self, capsys):
        """set-enforce with invalid mode should show help."""
        _run_set_enforce("invalid")
        captured = capsys.readouterr()
        assert "Usage: llm-router set-enforce" in captured.out
        assert "smart" in captured.out
        assert "soft" in captured.out
        assert "hard" in captured.out
        assert "off" in captured.out

    def test_set_enforce_no_mode_shows_help(self, capsys):
        """set-enforce with no mode should show help."""
        _run_set_enforce("")
        captured = capsys.readouterr()
        assert "Usage: llm-router set-enforce" in captured.out

    def test_set_enforce_valid_mode_creates_files(self, monkeypatch):
        """set-enforce with valid mode should create config files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            monkeypatch.setattr(Path, "home", lambda: home)

            _run_set_enforce("soft")

            # Check routing.yaml was created
            routing_yaml = home / ".llm-router" / "routing.yaml"
            assert routing_yaml.exists()
            content = routing_yaml.read_text()
            assert "enforce: soft" in content

    def test_set_enforce_updates_existing_routing_yaml(self, monkeypatch):
        """set-enforce should update existing routing.yaml."""
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            monkeypatch.setattr(Path, "home", lambda: home)

            # Create existing routing.yaml
            routing_yaml = home / ".llm-router" / "routing.yaml"
            routing_yaml.parent.mkdir(parents=True, exist_ok=True)
            routing_yaml.write_text("profile: balanced\nenforce: smart\n")

            _run_set_enforce("hard")

            content = routing_yaml.read_text()
            assert "enforce: hard" in content
            assert "profile: balanced" in content

    def test_set_enforce_creates_env_file(self, monkeypatch):
        """set-enforce should create .env file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            monkeypatch.setattr(Path, "home", lambda: home)

            _run_set_enforce("soft")

            env_path = home / ".llm-router" / ".env"
            assert env_path.exists()
            content = env_path.read_text()
            assert "LLM_ROUTER_ENFORCE=soft" in content

    def test_set_enforce_updates_existing_env(self, monkeypatch):
        """set-enforce should update existing .env file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            monkeypatch.setattr(Path, "home", lambda: home)

            # Create existing .env
            env_path = home / ".llm-router" / ".env"
            env_path.parent.mkdir(parents=True, exist_ok=True)
            env_path.write_text("OPENAI_API_KEY=sk-123\nLLM_ROUTER_ENFORCE=smart\n")

            _run_set_enforce("hard")

            content = env_path.read_text()
            assert "LLM_ROUTER_ENFORCE=hard" in content
            assert "OPENAI_API_KEY=sk-123" in content

    def test_set_enforce_displays_mode_description(self, capsys, monkeypatch):
        """set-enforce should display description of chosen mode."""
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            monkeypatch.setattr(Path, "home", lambda: home)

            _run_set_enforce("soft")

            captured = capsys.readouterr()
            assert "soft" in captured.out.lower()
            assert "Route hints in context" in captured.out


class TestSetEnforceIntegration:
    """Integration tests for set-enforce command."""

    def test_cmd_set_enforce_basic(self):
        """cmd_set_enforce should execute and return 0."""
        with patch("llm_router.commands.set_enforce._run_set_enforce"):
            result = cmd_set_enforce(["soft"])
        assert result == 0

    def test_enforce_modes_constants(self):
        """Enforce modes should be defined."""
        assert _ENFORCE_MODES is not None
        assert "smart" in _ENFORCE_MODES
        assert "soft" in _ENFORCE_MODES
        assert "hard" in _ENFORCE_MODES
        assert "off" in _ENFORCE_MODES
