"""Tests for the audit command (WS9 — wires audit_routing.run_audit() to the CLI)."""

from __future__ import annotations

from unittest.mock import patch

from llm_router.commands.audit import cmd_audit, _run_audit


class TestCmdAudit:
    """Tests for cmd_audit entry point."""

    def test_cmd_audit_returns_zero(self):
        """cmd_audit should return 0."""
        with patch("llm_router.commands.audit._run_audit"):
            result = cmd_audit([])
        assert result == 0

    def test_cmd_audit_no_args(self):
        """cmd_audit with no args should call _run_audit with an empty flag list."""
        with patch("llm_router.commands.audit._run_audit") as mock_run:
            cmd_audit([])
        mock_run.assert_called_once_with([])

    def test_cmd_audit_passes_through_flags(self):
        """cmd_audit should forward all args verbatim to _run_audit."""
        with patch("llm_router.commands.audit._run_audit") as mock_run:
            cmd_audit(["--limit", "50", "--json"])
        mock_run.assert_called_once_with(["--limit", "50", "--json"])


class TestRunAuditReport:
    """Tests for _run_audit rendering behavior."""

    def test_run_audit_disabled(self, capsys):
        """When the audit is disabled, the report should say so and not print counts."""
        with patch("llm_router.audit_routing.run_audit") as mock_run:
            with patch("llm_router.audit_routing.audit_disabled", return_value=True):
                mock_run.return_value = {"disabled": True, "sampled": 0, "audited": 0, "verdict_counts": {}}
                _run_audit([])

        captured = capsys.readouterr()
        assert "disabled" in captured.out.lower()
        assert "LLM_ROUTER_AUDIT_DISABLED" in captured.out

    def test_run_audit_no_unaudited_rows(self, capsys):
        """Zero sampled rows should print a clear 'nothing to audit' message."""
        with patch("llm_router.audit_routing.run_audit") as mock_run:
            with patch("llm_router.audit_routing.audit_disabled", return_value=False):
                mock_run.return_value = {
                    "disabled": False,
                    "sampled": 0,
                    "audited": 0,
                    "verdict_counts": {},
                }
                _run_audit([])

        captured = capsys.readouterr()
        assert "No unaudited routing decisions found" in captured.out

    def test_run_audit_with_verdicts(self, capsys):
        """A populated report should show sampled/audited counts and verdict breakdown."""
        with patch("llm_router.audit_routing.run_audit") as mock_run:
            with patch("llm_router.audit_routing.audit_disabled", return_value=False):
                mock_run.return_value = {
                    "disabled": False,
                    "sampled": 10,
                    "audited": 10,
                    "verdict_counts": {
                        "likely_misroute": 2,
                        "likely_correct": 7,
                        "insufficient_data": 1,
                    },
                    "mis_route_rate_inferred_baseline": 0.042,
                }
                _run_audit([])

        captured = capsys.readouterr()
        assert "Sampled:  10" in captured.out
        assert "Audited:  10" in captured.out
        assert "likely_misroute" in captured.out
        assert "2" in captured.out
        assert "likely_correct" in captured.out
        assert "insufficient_data" in captured.out
        assert "4.2%" in captured.out

    def test_run_audit_json_output(self, capsys):
        """--json should print the raw report dict as JSON, not the formatted view."""
        import json

        report = {
            "disabled": False,
            "sampled": 3,
            "audited": 3,
            "verdict_counts": {"likely_misroute": 1, "likely_correct": 2, "insufficient_data": 0},
            "mis_route_rate_inferred_baseline": None,
        }
        with patch("llm_router.audit_routing.run_audit") as mock_run:
            with patch("llm_router.audit_routing.audit_disabled", return_value=False):
                mock_run.return_value = report
                _run_audit(["--json"])

        captured = capsys.readouterr()
        parsed = json.loads(captured.out)
        assert parsed == report

    def test_run_audit_passes_limit_flag(self):
        """--limit N should be forwarded to run_audit(limit=N)."""
        with patch("llm_router.audit_routing.run_audit") as mock_run:
            with patch("llm_router.audit_routing.audit_disabled", return_value=False):
                mock_run.return_value = {
                    "disabled": False,
                    "sampled": 0,
                    "audited": 0,
                    "verdict_counts": {},
                }
                _run_audit(["--limit", "25"])

        mock_run.assert_called_once_with(limit=25)

    def test_run_audit_default_limit(self):
        """No --limit flag should default to limit=100."""
        with patch("llm_router.audit_routing.run_audit") as mock_run:
            with patch("llm_router.audit_routing.audit_disabled", return_value=False):
                mock_run.return_value = {
                    "disabled": False,
                    "sampled": 0,
                    "audited": 0,
                    "verdict_counts": {},
                }
                _run_audit([])

        mock_run.assert_called_once_with(limit=100)


class TestRunAuditNeverMutatesRouting:
    """Blocker 1 requirement: the audit command must be read-only / reporting
    only — it must never touch the live routing decision path."""

    def test_run_audit_only_imports_audit_routing_symbols(self):
        """The command module's runtime imports touching business logic should be
        limited to audit_routing's run_audit/audit_disabled — no router/routing
        decision module should be imported by this command."""
        import llm_router.commands.audit as commands_audit
        import inspect

        source = inspect.getsource(commands_audit)
        assert "from llm_router.audit_routing import" in source
        assert "llm_router.router" not in source


class TestBrandLeak:
    """No 'chuzom' substring anywhere in the audit CLI module or its output."""

    def test_commands_audit_module_has_no_unallowed_brand_leak(self):
        import llm_router.commands.audit as commands_audit

        for name in dir(commands_audit):
            assert "chuzom" not in name.lower(), f"brand leak in name: {name}"

    def test_audit_report_output_never_leaks_brand(self, capsys):
        with patch("llm_router.audit_routing.run_audit") as mock_run:
            with patch("llm_router.audit_routing.audit_disabled", return_value=False):
                mock_run.return_value = {
                    "disabled": False,
                    "sampled": 5,
                    "audited": 5,
                    "verdict_counts": {
                        "likely_misroute": 1,
                        "likely_correct": 3,
                        "insufficient_data": 1,
                    },
                    "mis_route_rate_inferred_baseline": 0.1,
                }
                _run_audit([])

        captured = capsys.readouterr()
        assert "chuzom" not in captured.out.lower()

    def test_audit_disabled_output_never_leaks_brand(self, capsys):
        with patch("llm_router.audit_routing.run_audit") as mock_run:
            with patch("llm_router.audit_routing.audit_disabled", return_value=True):
                mock_run.return_value = {"disabled": True, "sampled": 0, "audited": 0, "verdict_counts": {}}
                _run_audit([])

        captured = capsys.readouterr()
        assert "chuzom" not in captured.out.lower()
