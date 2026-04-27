"""Tests for the budget command."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from llm_router.commands.budget import cmd_budget, _run_budget


class TestCmdBudget:
    """Tests for cmd_budget entry point."""

    def test_cmd_budget_returns_zero(self):
        """cmd_budget should return 0."""
        with patch("llm_router.commands.budget._run_budget"):
            result = cmd_budget([])
        assert result == 0

    def test_cmd_budget_with_list_subcommand(self):
        """cmd_budget with 'list' should call _run_budget."""
        with patch("llm_router.commands.budget._run_budget") as mock_run:
            cmd_budget(["list"])
        mock_run.assert_called_once_with("list", [])

    def test_cmd_budget_with_set_subcommand(self):
        """cmd_budget with 'set' should call _run_budget."""
        with patch("llm_router.commands.budget._run_budget") as mock_run:
            cmd_budget(["set", "openai", "50.00"])
        mock_run.assert_called_once_with("set", ["openai", "50.00"])

    def test_cmd_budget_with_remove_subcommand(self):
        """cmd_budget with 'remove' should call _run_budget."""
        with patch("llm_router.commands.budget._run_budget") as mock_run:
            cmd_budget(["remove", "openai"])
        mock_run.assert_called_once_with("remove", ["openai"])

    def test_cmd_budget_no_args_defaults_to_list(self):
        """cmd_budget with no args should call _run_budget with 'list'."""
        with patch("llm_router.commands.budget._run_budget") as mock_run:
            cmd_budget([])
        mock_run.assert_called_once_with("list", [])


class TestBudgetList:
    """Tests for budget list functionality."""

    def test_budget_list_displays_header(self, capsys):
        """budget list should display header."""
        with patch("llm_router.budget.get_all_budget_states") as mock_states:
            with patch("llm_router.budget_store.list_caps") as mock_caps:
                # Mock empty budget state
                mock_states.return_value = {}
                mock_caps.return_value = {}

                _run_budget("list", [])

        captured = capsys.readouterr()
        assert "[llm-router] Budget Caps" in captured.out
        assert "Provider" in captured.out
        assert "Spend" in captured.out
        assert "Cap" in captured.out

    def test_budget_list_with_providers(self, capsys):
        """budget list should display provider information."""
        with patch("llm_router.budget.get_all_budget_states") as mock_states:
            with patch("llm_router.budget_store.list_caps") as mock_caps:
                # Create mock budget states
                state_openai = MagicMock()
                state_openai.spend_usd = 15.50
                state_openai.cap_usd = 50.0
                state_openai.pressure = 0.31

                state_ollama = MagicMock()
                state_ollama.spend_usd = 0.0
                state_ollama.cap_usd = 0.0
                state_ollama.pressure = 0.0

                mock_states.return_value = {
                    "openai": state_openai,
                    "ollama": state_ollama,
                }
                mock_caps.return_value = {"openai": 50.0}

                _run_budget("list", [])

        captured = capsys.readouterr()
        assert "openai" in captured.out
        assert "$15.50" in captured.out
        assert "$50.00" in captured.out
        assert "31%" in captured.out

    def test_budget_list_with_uncapped_providers(self, capsys):
        """budget list should show alert for uncapped providers."""
        with patch("llm_router.budget.get_all_budget_states") as mock_states:
            with patch("llm_router.budget_store.list_caps") as mock_caps:
                state_openai = MagicMock()
                state_openai.spend_usd = 5.0
                state_openai.cap_usd = 0.0
                state_openai.pressure = 0.0

                mock_states.return_value = {"openai": state_openai}
                mock_caps.return_value = {}

                _run_budget("list", [])

        captured = capsys.readouterr()
        assert "No cap set for: openai" in captured.out
        assert "Set one: llm-router budget set" in captured.out


class TestBudgetSet:
    """Tests for budget set functionality."""

    def test_budget_set_missing_provider(self, capsys):
        """budget set without provider should show error."""
        with pytest.raises(SystemExit) as exc_info:
            _run_budget("set", [])
        assert exc_info.value.code == 1

    def test_budget_set_missing_amount(self, capsys):
        """budget set without amount should show error."""
        with pytest.raises(SystemExit) as exc_info:
            _run_budget("set", ["openai"])
        assert exc_info.value.code == 1

    def test_budget_set_invalid_amount(self, capsys):
        """budget set with non-numeric amount should show error."""
        with pytest.raises(SystemExit) as exc_info:
            _run_budget("set", ["openai", "invalid"])
        assert exc_info.value.code == 1

    def test_budget_set_valid_amount(self, capsys):
        """budget set with valid amount should set the cap."""
        with patch("llm_router.budget_store.set_cap") as mock_set:
            with patch("llm_router.budget.invalidate_cache") as mock_inv:
                _run_budget("set", ["openai", "50.00"])

        mock_set.assert_called_once_with("openai", 50.0)
        mock_inv.assert_called_once_with("openai")

        captured = capsys.readouterr()
        assert "Budget cap set" in captured.out
        assert "openai" in captured.out
        assert "$50.00" in captured.out

    def test_budget_set_invalid_provider(self, capsys):
        """budget set with invalid provider should show error."""
        with patch("llm_router.budget_store.set_cap") as mock_set:
            mock_set.side_effect = ValueError("Unknown provider")
            with pytest.raises(SystemExit) as exc_info:
                _run_budget("set", ["invalid", "50.00"])
            assert exc_info.value.code == 1


class TestBudgetRemove:
    """Tests for budget remove functionality."""

    def test_budget_remove_missing_provider(self, capsys):
        """budget remove without provider should show error."""
        with pytest.raises(SystemExit) as exc_info:
            _run_budget("remove", [])
        assert exc_info.value.code == 1

    def test_budget_remove_existing_cap(self, capsys):
        """budget remove with existing cap should remove it."""
        with patch("llm_router.budget_store.remove_cap") as mock_remove:
            with patch("llm_router.budget.invalidate_cache") as mock_inv:
                mock_remove.return_value = True
                _run_budget("remove", ["openai"])

        mock_remove.assert_called_once_with("openai")
        mock_inv.assert_called_once_with("openai")

        captured = capsys.readouterr()
        assert "Removed cap for openai" in captured.out

    def test_budget_remove_nonexistent_cap(self, capsys):
        """budget remove with nonexistent cap should show warning."""
        with patch("llm_router.budget_store.remove_cap") as mock_remove:
            with patch("llm_router.budget.invalidate_cache"):
                mock_remove.return_value = False
                _run_budget("remove", ["openai"])

        captured = capsys.readouterr()
        assert "No cap was set for openai" in captured.out


class TestBudgetIntegration:
    """Integration tests for budget command."""

    def test_cmd_budget_command_basic(self):
        """cmd_budget should execute and return 0."""
        with patch("llm_router.commands.budget._run_budget"):
            result = cmd_budget(["list"])
        assert result == 0

    def test_cmd_budget_set_integration(self):
        """cmd_budget set should dispatch correctly."""
        with patch("llm_router.budget_store.set_cap") as mock_set:
            with patch("llm_router.budget.invalidate_cache"):
                result = cmd_budget(["set", "openai", "100.00"])

        assert result == 0
        mock_set.assert_called_once()
