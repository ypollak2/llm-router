"""Tests for the dashboard command."""

from __future__ import annotations


import pytest


class TestDashboardPortValidation:
    """Tests for dashboard port validation."""

    def test_invalid_port_exits(self):
        """Dashboard should exit with error for invalid port."""
        from llm_router.commands.dashboard import cmd_dashboard
        
        with pytest.raises(SystemExit) as exc_info:
            cmd_dashboard(["--port", "not_a_number"])
        assert exc_info.value.code == 1

    def test_invalid_port_prints_error(self, capsys):
        """Dashboard should print error message for invalid port."""
        from llm_router.commands.dashboard import cmd_dashboard
        
        try:
            cmd_dashboard(["--port", "invalid_port"])
        except SystemExit:
            pass
        
        captured = capsys.readouterr()
        assert "Invalid port" in captured.out

    def test_port_flag_parsing(self):
        """Dashboard should correctly extract port value from flags."""
        # Test the port parsing logic by checking that invalid port causes error
        from llm_router.commands.dashboard import cmd_dashboard
        
        with pytest.raises(SystemExit):
            cmd_dashboard(["--port", "abc123"])


class TestDashboardCommandStructure:
    """Tests for dashboard command structure."""

    def test_cmd_dashboard_exists(self):
        """cmd_dashboard function should exist and be callable."""
        from llm_router.commands.dashboard import cmd_dashboard
        
        assert callable(cmd_dashboard)

    def test_cmd_dashboard_takes_args(self, monkeypatch):
        """cmd_dashboard should accept a list of arguments."""
        from llm_router.commands.dashboard import cmd_dashboard

        # Mock the run function to avoid blocking the test
        async def mock_run(port):
            return None

        # Patch the server run function
        monkeypatch.setattr("llm_router.dashboard.server.run", mock_run)

        # Should handle empty args
        result = cmd_dashboard([])
        assert result == 0
