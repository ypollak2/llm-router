"""Tests for the status command."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from unittest.mock import patch

import pytest

from llm_router.commands.status import (
    cmd_status,
    _savings_bar,
    _query_routing_period,
    _query_free_model_savings,
)


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
        assert "llm-router status" in captured.out

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
        assert "llm-router status" in captured.out
        assert "Claude Code subscription" in captured.out

    def test_cmd_status_no_usage_db(self, capsys):
        """cmd_status should show 'no data yet' when usage.db doesn't exist."""
        with patch("os.path.expanduser") as mock_expand:
            mock_expand.return_value = "/fake/home"
            with patch("os.path.exists", return_value=False):
                cmd_status([])
        captured = capsys.readouterr()
        assert "no data yet" in captured.out or "no data — run" in captured.out


class TestSavingsBar:
    """Tests for _savings_bar formatting."""

    def test_savings_bar_zero_total(self):
        """_savings_bar with zero total should show dashes."""
        result = _savings_bar(0.0, 0.0, width=10)
        assert "─" in result

    def test_savings_bar_all_saved(self):
        """_savings_bar with all savings should show full green bar."""
        result = _savings_bar(100.0, 0.0, width=10)
        assert "█" in result

    def test_savings_bar_half_saved(self):
        """_savings_bar with 50/50 split should show mixed bar."""
        result = _savings_bar(50.0, 50.0, width=10)
        assert "█" in result
        assert "░" in result

    def test_savings_bar_width_parameter(self):
        """_savings_bar should respect width parameter."""
        result = _savings_bar(50.0, 50.0, width=5)
        # Count characters (excluding ANSI codes)
        import re
        clean = re.sub(r'\033\[[0-9;]*m', '', result)
        # Should have roughly the width
        assert "█" in clean or "░" in clean


class TestQueryRoutingPeriod:
    """Tests for _query_routing_period database query."""

    def test_query_routing_period_no_db(self):
        """_query_routing_period with missing DB should return (0, 0.0, 0.0)."""
        result = _query_routing_period("/nonexistent/db.db", "2024-01-01")
        assert result == (0, 0.0, 0.0)

    def test_query_routing_period_empty_db(self):
        """_query_routing_period with empty DB should return (0, 0.0, 0.0)."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            conn = sqlite3.connect(db_path)
            conn.execute(
                "CREATE TABLE usage ("
                "id INTEGER, timestamp TEXT, input_tokens INTEGER, "
                "output_tokens INTEGER, cost_usd REAL, success INTEGER, provider TEXT)"
            )
            conn.commit()
            conn.close()

            result = _query_routing_period(db_path, "2024-01-01")
            assert result == (0, 0.0, 0.0)
        finally:
            os.unlink(db_path)

    def test_query_routing_period_with_rows(self):
        """_query_routing_period should calculate calls, cost, and baseline correctly."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            conn = sqlite3.connect(db_path)
            conn.execute(
                "CREATE TABLE usage ("
                "id INTEGER, timestamp TEXT, input_tokens INTEGER, "
                "output_tokens INTEGER, cost_usd REAL, success INTEGER, provider TEXT)"
            )
            # Add one row: 1000 input, 500 output, $0.05 cost
            conn.execute(
                "INSERT INTO usage VALUES (1, '2024-01-02', 1000, 500, 0.05, 1, 'openai')"
            )
            conn.commit()
            conn.close()

            calls, cost, baseline = _query_routing_period(db_path, "2024-01-01")
            assert calls == 1
            assert cost == 0.05
            # baseline = (1000 * 3.0 + 500 * 15.0) / 1_000_000
            assert baseline == pytest.approx((1000 * 3.0 + 500 * 15.0) / 1_000_000)
        finally:
            os.unlink(db_path)


class TestQueryFreeModelSavings:
    """Tests for _query_free_model_savings database query."""

    def test_query_free_model_savings_no_db(self):
        """_query_free_model_savings with missing DB should return []."""
        result = _query_free_model_savings("/nonexistent/db.db")
        assert result == []

    def test_query_free_model_savings_empty_db(self):
        """_query_free_model_savings with empty DB should return []."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            conn = sqlite3.connect(db_path)
            conn.execute(
                "CREATE TABLE usage ("
                "id INTEGER, input_tokens INTEGER, "
                "output_tokens INTEGER, cost_usd REAL, success INTEGER, provider TEXT)"
            )
            conn.commit()
            conn.close()

            result = _query_free_model_savings(db_path)
            assert result == []
        finally:
            os.unlink(db_path)

    def test_query_free_model_savings_with_ollama(self):
        """_query_free_model_savings should count Ollama calls."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            conn = sqlite3.connect(db_path)
            conn.execute(
                "CREATE TABLE usage ("
                "id INTEGER, input_tokens INTEGER, "
                "output_tokens INTEGER, cost_usd REAL, success INTEGER, provider TEXT)"
            )
            # Add Ollama rows
            conn.execute(
                "INSERT INTO usage VALUES (1, 1000, 500, 0.0, 1, 'ollama')"
            )
            conn.execute(
                "INSERT INTO usage VALUES (2, 2000, 1000, 0.0, 1, 'ollama')"
            )
            conn.commit()
            conn.close()

            result = _query_free_model_savings(db_path)
            assert len(result) == 1
            assert result[0]["provider"] == "ollama"
            assert result[0]["calls"] == 2
            assert result[0]["in_tok"] == 3000
            assert result[0]["out_tok"] == 1500
            assert result[0]["cost_usd"] == 0.0
        finally:
            os.unlink(db_path)

    def test_query_free_model_savings_calculates_baseline(self):
        """_query_free_model_savings should calculate baseline correctly."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            conn = sqlite3.connect(db_path)
            conn.execute(
                "CREATE TABLE usage ("
                "id INTEGER, input_tokens INTEGER, "
                "output_tokens INTEGER, cost_usd REAL, success INTEGER, provider TEXT)"
            )
            conn.execute(
                "INSERT INTO usage VALUES (1, 1000, 500, 0.0, 1, 'ollama')"
            )
            conn.commit()
            conn.close()

            result = _query_free_model_savings(db_path)
            assert result[0]["baseline"] == pytest.approx((1000 * 3.0 + 500 * 15.0) / 1_000_000)
            assert result[0]["saved"] == result[0]["baseline"]
        finally:
            os.unlink(db_path)


class TestStatusIntegration:
    """Integration tests for status command."""

    def test_status_displays_subcommands(self, capsys):
        """status should display subcommands at the end."""
        with patch("os.path.expanduser") as mock_expand:
            mock_expand.return_value = "/fake/home"
            with patch("os.path.exists", return_value=False):
                cmd_status([])
        captured = capsys.readouterr()
        assert "llm-router update" in captured.out
        assert "llm-router doctor" in captured.out
        assert "llm-router dashboard" in captured.out

    def test_status_handles_missing_pressure_data(self, capsys):
        """status should handle missing subscription pressure data gracefully."""
        with patch("os.path.expanduser") as mock_expand:
            mock_expand.return_value = "/fake/home"
            with patch("os.path.exists", return_value=False):
                with patch("builtins.open", side_effect=FileNotFoundError):
                    cmd_status([])
        captured = capsys.readouterr()
        assert "llm-router status" in captured.out
        # Should show the warning about missing data
        assert "no data" in captured.out.lower() or "run" in captured.out.lower()
