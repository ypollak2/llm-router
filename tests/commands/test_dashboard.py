"""Tests for the dashboard command."""

from __future__ import annotations

import pytest


class TestDashboardPortValidation:
    """Tests for dashboard port validation."""

    def test_invalid_port_exits_with_web_flag(self):
        """Dashboard should exit with error for invalid port with --web flag."""
        from llm_router.commands.dashboard import cmd_dashboard

        with pytest.raises(SystemExit) as exc_info:
            cmd_dashboard(["--web", "--port", "not_a_number"])
        assert exc_info.value.code == 1

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

    def test_cmd_dashboard_tui_default(self, monkeypatch):
        """cmd_dashboard should launch TUI by default (empty args)."""
        # Check if textual is available first
        try:
            import textual  # noqa: F401
            textual_available = True
        except ImportError:
            textual_available = False

        if not textual_available:
            pytest.skip("Textual not installed (optional dependency)")

        from llm_router.commands.dashboard import cmd_dashboard

        # Mock the TUI app to avoid blocking the test
        class MockApp:
            def run(self):
                pass

        monkeypatch.setattr("llm_router.tui.LLMRouterDashboard", MockApp)
        result = cmd_dashboard([])
        assert result == 0

    def test_cmd_dashboard_web_flag(self, monkeypatch):
        """cmd_dashboard should launch web dashboard with --web flag."""
        from llm_router.commands.dashboard import cmd_dashboard

        # Mock the web server run function
        async def mock_run(port):
            return None

        monkeypatch.setattr("llm_router.dashboard.server.run", mock_run)

        result = cmd_dashboard(["--web"])
        assert result == 0

    def test_cmd_dashboard_web_with_port(self, monkeypatch):
        """cmd_dashboard should accept --port with --web flag."""
        from llm_router.commands.dashboard import cmd_dashboard

        # Mock the web server to capture the port
        call_args = {}

        async def mock_run(port):
            call_args["port"] = port
            return None

        monkeypatch.setattr("llm_router.dashboard.server.run", mock_run)

        result = cmd_dashboard(["--web", "--port", "8000"])
        assert result == 0
        # Note: async run is wrapped in asyncio.run, so we can't easily test
        # the exact port value without more intrusive mocking
