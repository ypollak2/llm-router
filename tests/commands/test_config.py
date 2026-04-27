"""Tests for the config command."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock


from llm_router.commands.config import cmd_config, _run_config_init


class TestCmdConfig:
    """Tests for cmd_config entry point."""

    def test_cmd_config_returns_zero(self):
        """cmd_config should return 0."""
        with patch("llm_router.commands.config._run_config"):
            result = cmd_config([])
        assert result == 0

    def test_cmd_config_with_show_subcommand(self):
        """cmd_config with 'show' should call _run_config."""
        with patch("llm_router.commands.config._run_config") as mock_run:
            cmd_config(["show"])
        mock_run.assert_called_once_with(["show"])

    def test_cmd_config_with_lint_subcommand(self):
        """cmd_config with 'lint' should call _run_config."""
        with patch("llm_router.commands.config._run_config") as mock_run:
            cmd_config(["lint"])
        mock_run.assert_called_once_with(["lint"])

    def test_cmd_config_with_init_subcommand(self):
        """cmd_config with 'init' should call _run_config."""
        with patch("llm_router.commands.config._run_config") as mock_run:
            cmd_config(["init"])
        mock_run.assert_called_once_with(["init"])

    def test_cmd_config_no_args_defaults_to_show(self):
        """cmd_config with no args should call _run_config with empty list."""
        with patch("llm_router.commands.config._run_config") as mock_run:
            cmd_config([])
        mock_run.assert_called_once_with([])


class TestConfigInit:
    """Tests for _run_config_init function."""

    def test_config_init_creates_file(self):
        """_run_config_init should create .llm-router.yml."""
        with tempfile.TemporaryDirectory() as tmpdir:
            old_cwd = os.getcwd()
            try:
                os.chdir(tmpdir)
                with patch("llm_router.repo_config.fingerprint_repo") as mock_fp:
                    mock_fp.return_value = ("python", "balanced")
                    _run_config_init()
                assert Path(".llm-router.yml").exists()
            finally:
                os.chdir(old_cwd)

    def test_config_init_file_content(self):
        """_run_config_init should create valid YAML."""
        with tempfile.TemporaryDirectory() as tmpdir:
            old_cwd = os.getcwd()
            try:
                os.chdir(tmpdir)
                with patch("llm_router.repo_config.fingerprint_repo") as mock_fp:
                    mock_fp.return_value = ("python", "balanced")
                    _run_config_init()
                content = Path(".llm-router.yml").read_text()
                assert "version: 1" in content
                assert "profile: balanced" in content
                assert "enforce: enforce" in content
            finally:
                os.chdir(old_cwd)

    def test_config_init_skips_existing_file(self, capsys):
        """_run_config_init should not overwrite existing file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            old_cwd = os.getcwd()
            try:
                os.chdir(tmpdir)
                # Create the file first
                Path(".llm-router.yml").write_text("existing content")
                
                with patch("llm_router.repo_config.fingerprint_repo") as mock_fp:
                    mock_fp.return_value = ("python", "balanced")
                    _run_config_init()
                
                # File should still have original content
                content = Path(".llm-router.yml").read_text()
                assert content == "existing content"
                
                captured = capsys.readouterr()
                assert "already exists" in captured.out
            finally:
                os.chdir(old_cwd)

    def test_config_init_uses_fingerprinted_profile(self):
        """_run_config_init should use repo-specific profile."""
        with tempfile.TemporaryDirectory() as tmpdir:
            old_cwd = os.getcwd()
            try:
                os.chdir(tmpdir)
                with patch("llm_router.repo_config.fingerprint_repo") as mock_fp:
                    mock_fp.return_value = ("nodejs", "premium")
                    _run_config_init()
                content = Path(".llm-router.yml").read_text()
                assert "profile: premium" in content
            finally:
                os.chdir(old_cwd)


class TestConfigShow:
    """Tests for config show/lint functionality."""

    def test_config_show_displays_header(self, capsys):
        """config show should display config header."""
        with patch("llm_router.repo_config.effective_config") as mock_config:
            with patch("llm_router.repo_config.fingerprint_repo") as mock_fp:
                with patch("llm_router.repo_config.find_repo_config_path") as mock_find:
                    mock_config_obj = MagicMock()
                    mock_config_obj.effective_enforce.return_value = "enforce"
                    mock_config_obj.effective_profile.return_value = "balanced"
                    mock_config_obj.block_providers = []
                    mock_config_obj.daily_caps = {}
                    mock_config_obj.routing = {}
                    mock_config.return_value = mock_config_obj
                    mock_fp.return_value = ("python", "balanced")
                    mock_find.return_value = None
                    
                    from llm_router.commands.config import _run_config
                    _run_config(["show"])
        
        captured = capsys.readouterr()
        assert "llm-router config" in captured.out

    def test_config_lint_validates_yaml(self):
        """config lint should validate YAML syntax."""
        with tempfile.TemporaryDirectory() as tmpdir:
            old_cwd = os.getcwd()
            try:
                os.chdir(tmpdir)
                Path(".llm-router.yml").write_text("version: 1\nprofile: balanced")
                
                with patch("llm_router.repo_config.effective_config") as mock_config:
                    with patch("llm_router.repo_config.fingerprint_repo") as mock_fp:
                        with patch("llm_router.repo_config.find_repo_config_path") as mock_find:
                            mock_config_obj = MagicMock()
                            mock_config_obj.effective_enforce.return_value = "enforce"
                            mock_config_obj.effective_profile.return_value = "balanced"
                            mock_config_obj.block_providers = []
                            mock_config_obj.daily_caps = {}
                            mock_config_obj.routing = {}
                            mock_config.return_value = mock_config_obj
                            mock_fp.return_value = ("python", "balanced")
                            mock_find.return_value = Path.cwd() / ".llm-router.yml"
                            
                            from llm_router.commands.config import _run_config
                            _run_config(["lint"])
            finally:
                os.chdir(old_cwd)


class TestConfigIntegration:
    """Integration tests for config command."""

    def test_config_command_basic(self):
        """config command should execute without error."""
        with patch("llm_router.repo_config.effective_config") as mock_config:
            with patch("llm_router.repo_config.fingerprint_repo") as mock_fp:
                with patch("llm_router.repo_config.find_repo_config_path") as mock_find:
                    mock_config_obj = MagicMock()
                    mock_config_obj.effective_enforce.return_value = "enforce"
                    mock_config_obj.effective_profile.return_value = None
                    mock_config_obj.block_providers = []
                    mock_config_obj.daily_caps = {}
                    mock_config_obj.routing = {}
                    mock_config.return_value = mock_config_obj
                    mock_fp.return_value = ("python", "balanced")
                    mock_find.return_value = None
                    
                    result = cmd_config([])
        assert result == 0
