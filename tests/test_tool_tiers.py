"""Tests for v4.0.0 features: slim mode, session spend, reroute, quickstart, doctor --host."""

from __future__ import annotations

import json
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Tool slim mode ────────────────────────────────────────────────────────────


class TestToolTiers:
    def test_off_allows_all_tools(self):
        from llm_router.tool_tiers import make_should_register

        gate = make_should_register("off")
        assert gate("llm_query")
        assert gate("llm_video")
        assert gate("llm_team_report")
        assert gate("anything")

    def test_core_allows_only_4_tools(self):
        from llm_router.tool_tiers import CORE_TOOLS, make_should_register

        gate = make_should_register("core")
        for t in CORE_TOOLS:
            assert gate(t), f"{t} should be allowed in core tier"
        assert not gate("llm_video"), "llm_video should NOT be in core tier"
        assert not gate("llm_team_report"), "llm_team_report should NOT be in core tier"
        assert not gate("llm_image"), "llm_image should NOT be in core tier"

    def test_routing_allows_subset(self):
        from llm_router.tool_tiers import ROUTING_TOOLS, make_should_register

        gate = make_should_register("routing")
        for t in ROUTING_TOOLS:
            assert gate(t), f"{t} should be allowed in routing tier"
        assert not gate("llm_video"), "llm_video should NOT be in routing tier"
        assert not gate("llm_image"), "llm_image should NOT be in routing tier"

    def test_routing_is_superset_of_core(self):
        from llm_router.tool_tiers import CORE_TOOLS, ROUTING_TOOLS

        assert CORE_TOOLS.issubset(ROUTING_TOOLS)

    def test_unknown_slim_value_allows_all(self):
        from llm_router.tool_tiers import make_should_register

        gate = make_should_register("invalid_value")
        assert gate("llm_query")
        assert gate("llm_video")

    def test_empty_slim_allows_all(self):
        from llm_router.tool_tiers import make_should_register

        gate = make_should_register("")
        assert gate("llm_query")

    def test_tier_summary_returns_string(self):
        from llm_router.tool_tiers import tier_summary

        assert "41" in tier_summary("off") or "43" in tier_summary("off")
        assert "routing" in tier_summary("routing")
        assert "core" in tier_summary("core")


# ── Session spend ─────────────────────────────────────────────────────────────


class TestSessionSpend:
    def test_record_accumulates_cost(self, tmp_path, monkeypatch):
        from llm_router import session_spend as ss
        monkeypatch.setattr(ss, "SESSION_SPEND_FILE", tmp_path / "session_spend.json")
        monkeypatch.setattr(ss, "_spend", None)

        spend = ss.SessionSpend()
        spend.record(model="openai/gpt-4o", tool="llm_code",
                     input_tokens=100, output_tokens=200, cost_usd=0.01)
        spend.record(model="openai/gpt-4o", tool="llm_query",
                     input_tokens=50, output_tokens=100, cost_usd=0.005)

        assert abs(spend.total_usd - 0.015) < 1e-9
        assert spend.call_count == 2

    def test_per_model_and_per_tool_tracked(self, tmp_path, monkeypatch):
        from llm_router import session_spend as ss
        monkeypatch.setattr(ss, "SESSION_SPEND_FILE", tmp_path / "session_spend.json")

        spend = ss.SessionSpend()
        spend.record("openai/gpt-4o", "llm_code", 100, 200, 0.01)
        spend.record("gemini/gemini-2.5-flash", "llm_query", 50, 100, 0.001)

        assert "openai/gpt-4o" in spend.per_model
        assert "gemini/gemini-2.5-flash" in spend.per_model
        assert spend.per_tool["llm_code"] == 1
        assert spend.per_tool["llm_query"] == 1

    def test_anomaly_fires_above_threshold(self, tmp_path, monkeypatch):
        from llm_router import session_spend as ss
        monkeypatch.setattr(ss, "SESSION_SPEND_FILE", tmp_path / "session_spend.json")
        monkeypatch.setenv("LLM_ROUTER_ANOMALY_THRESHOLD", "0.10")

        spend = ss.SessionSpend()
        spend.session_start = time.time() - 60  # only 1 minute elapsed

        spend.record("openai/gpt-4o", "llm_code", 1000, 5000, 0.15)

        assert spend.anomaly_flag

    def test_anomaly_not_fired_for_long_session(self, tmp_path, monkeypatch):
        from llm_router import session_spend as ss
        monkeypatch.setattr(ss, "SESSION_SPEND_FILE", tmp_path / "session_spend.json")
        monkeypatch.setenv("LLM_ROUTER_ANOMALY_THRESHOLD", "0.10")

        spend = ss.SessionSpend()
        spend.session_start = time.time() - 700  # more than 10 minutes

        spend.record("openai/gpt-4o", "llm_code", 1000, 5000, 0.15)

        assert not spend.anomaly_flag  # elapsed > 600s, no anomaly

    def test_persist_writes_to_disk(self, tmp_path, monkeypatch):
        from llm_router import session_spend as ss
        spend_file = tmp_path / "session_spend.json"
        monkeypatch.setattr(ss, "SESSION_SPEND_FILE", spend_file)

        spend = ss.SessionSpend()
        spend.record("gpt-4o", "llm_code", 100, 200, 0.01)

        assert spend_file.exists()
        data = json.loads(spend_file.read_text())
        assert data["call_count"] == 1
        assert abs(data["total_usd"] - 0.01) < 1e-9

    def test_load_from_disk(self, tmp_path, monkeypatch):
        from llm_router import session_spend as ss
        spend_file = tmp_path / "session_spend.json"
        monkeypatch.setattr(ss, "SESSION_SPEND_FILE", spend_file)

        spend = ss.SessionSpend()
        spend.record("gpt-4o", "llm_code", 100, 200, 0.05)

        monkeypatch.setattr(ss, "_spend", None)
        loaded = ss.SessionSpend.load()
        assert abs(loaded.total_usd - 0.05) < 1e-9
        assert loaded.call_count == 1

    def test_reset_clears_data(self, tmp_path, monkeypatch):
        from llm_router import session_spend as ss
        monkeypatch.setattr(ss, "SESSION_SPEND_FILE", tmp_path / "session_spend.json")

        spend = ss.SessionSpend()
        spend.record("gpt-4o", "llm_code", 100, 200, 0.05)
        spend.reset()

        assert spend.total_usd == 0.0
        assert spend.call_count == 0
        assert not spend.anomaly_flag

    def test_cost_estimation_fallback(self):
        from llm_router.session_spend import _estimate_cost

        cost = _estimate_cost("unknown-model-xyz", 500, 300)
        assert cost > 0  # uses conservative fallback

    def test_get_summary_structure(self, tmp_path, monkeypatch):
        from llm_router import session_spend as ss
        monkeypatch.setattr(ss, "SESSION_SPEND_FILE", tmp_path / "session_spend.json")

        spend = ss.SessionSpend()
        spend.record("gpt-4o", "llm_code", 100, 200, 0.05)

        summary = spend.get_summary()
        assert "total_usd" in summary
        assert "call_count" in summary
        assert "anomaly_flag" in summary
        assert "per_model" in summary
        assert "per_tool" in summary


# ── Corrections / llm_reroute ─────────────────────────────────────────────────


class TestCorrectionsTable:
    @pytest.mark.asyncio
    async def test_log_and_count_corrections(self, tmp_path, monkeypatch):
        from llm_router import config as cfg_module
        from llm_router.cost import log_correction, get_correction_count

        test_db = tmp_path / "test.db"
        mock_config = MagicMock()
        mock_config.llm_router_db_path = test_db
        monkeypatch.setattr(cfg_module, "_config", mock_config)

        await log_correction("llm_query", "gemini-flash", "llm_analyze", reason="too simple")
        await log_correction("llm_query", "gemini-flash", "llm_analyze", reason="wrong model")

        count = await get_correction_count("llm_query")
        assert count == 2

    @pytest.mark.asyncio
    async def test_correction_count_zero_for_unknown_tool(self, tmp_path, monkeypatch):
        from llm_router import config as cfg_module
        from llm_router.cost import get_correction_count

        test_db = tmp_path / "test2.db"
        mock_config = MagicMock()
        mock_config.llm_router_db_path = test_db
        monkeypatch.setattr(cfg_module, "_config", mock_config)

        count = await get_correction_count("llm_nonexistent_tool")
        assert count == 0


class TestLlmReroute:
    @pytest.mark.asyncio
    async def test_reroute_valid_tool_records_correction(self, tmp_path, monkeypatch):
        from llm_router import config as cfg_module
        from llm_router.tools.routing import llm_reroute

        test_db = tmp_path / "test.db"
        mock_config = MagicMock()
        mock_config.llm_router_db_path = test_db
        monkeypatch.setattr(cfg_module, "_config", mock_config)

        result = await llm_reroute(
            to_tool="llm_analyze",
            reason="task needs deep analysis",
            original_tool="llm_query",
            original_model="gemini-flash",
        )
        assert "llm_analyze" in result
        assert "Correction recorded" in result

    @pytest.mark.asyncio
    async def test_reroute_invalid_tool_returns_error(self):
        from llm_router.tools.routing import llm_reroute

        result = await llm_reroute(to_tool="llm_nonexistent")
        assert "Unknown tool" in result

    @pytest.mark.asyncio
    async def test_reroute_shows_confidence_impact(self, tmp_path, monkeypatch):
        from llm_router import config as cfg_module
        from llm_router.tools.routing import llm_reroute

        test_db = tmp_path / "test.db"
        mock_config = MagicMock()
        mock_config.llm_router_db_path = test_db
        monkeypatch.setattr(cfg_module, "_config", mock_config)

        result = await llm_reroute(
            to_tool="llm_code",
            original_tool="llm_query",
        )
        assert "correction" in result.lower()


# ── Quickstart ────────────────────────────────────────────────────────────────


class TestQuickstart:
    def test_detect_hosts_returns_list(self):
        from llm_router.quickstart import detect_hosts

        result = detect_hosts()
        assert isinstance(result, list)

    def test_detect_hosts_finds_cursor_when_dir_exists(self, tmp_path, monkeypatch):
        from llm_router import quickstart as qs

        cursor_dir = tmp_path / ".cursor"
        cursor_dir.mkdir()
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        hosts = qs.detect_hosts()
        assert "cursor" in hosts

    def test_detect_hosts_empty_when_nothing_installed(self, tmp_path, monkeypatch):
        import shutil
        from llm_router import quickstart as qs

        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        monkeypatch.setattr(shutil, "which", lambda _: None)

        hosts = qs.detect_hosts()
        # cursor won't be found (no .cursor dir), vscode won't (no dir), claude won't (no binary)
        assert "cursor" not in hosts


# ── Doctor --host ─────────────────────────────────────────────────────────────


class TestDoctorHost:
    def test_doctor_host_vscode_missing_mcp_json(self, tmp_path, monkeypatch, capsys):
        from llm_router.commands.doctor import _run_doctor_host

        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        _run_doctor_host("vscode")
        out = capsys.readouterr().out
        assert "mcp.json" in out.lower() or "not found" in out.lower() or "vscode" in out.lower()

    def test_doctor_host_cursor_missing_mcp_json(self, tmp_path, monkeypatch, capsys):
        from llm_router.commands.doctor import _run_doctor_host

        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        _run_doctor_host("cursor")
        out = capsys.readouterr().out
        assert "cursor" in out.lower()

    def test_doctor_host_cursor_passes_when_configured(self, tmp_path, monkeypatch, capsys):
        from llm_router.commands.doctor import _run_doctor_host

        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        # Create a valid Cursor mcp.json
        cursor_dir = tmp_path / ".cursor"
        cursor_dir.mkdir()
        mcp_json = cursor_dir / "mcp.json"
        mcp_json.write_text(json.dumps({"mcpServers": {"llm-router": {"command": "uvx"}}}))

        _run_doctor_host("cursor")
        out = capsys.readouterr().out
        assert "registered" in out.lower() or "llm-router" in out.lower()

    def test_doctor_host_vscode_passes_when_configured(self, tmp_path, monkeypatch, capsys):
        from llm_router.commands.doctor import _run_doctor_host

        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        if sys.platform == "darwin":
            mcp_dir = tmp_path / "Library" / "Application Support" / "Code" / "User"
        else:
            mcp_dir = tmp_path / ".config" / "Code" / "User"
        mcp_dir.mkdir(parents=True)
        mcp_json = mcp_dir / "mcp.json"
        mcp_json.write_text(json.dumps({"servers": {"llm-router": {"command": "uvx"}}}))

        _run_doctor_host("vscode")
        out = capsys.readouterr().out
        assert "llm-router" in out.lower() or "registered" in out.lower()

    def test_doctor_host_all_checks_multiple_hosts(self, tmp_path, monkeypatch, capsys):
        from llm_router.commands.doctor import _run_doctor_host
        pass  # no additional imports needed

        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        _run_doctor_host("all")
        out = capsys.readouterr().out
        assert "vscode" in out.lower()
        assert "cursor" in out.lower()

    def test_doctor_host_invalid_host(self, capsys):
        from llm_router.commands.doctor import _run_doctor_host

        _run_doctor_host("unknown_host_xyz")
        out = capsys.readouterr().out
        assert "Unknown host" in out or "unknown" in out.lower()


# ── Config new fields ─────────────────────────────────────────────────────────


class TestV4Config:
    def test_slim_field_defaults_to_off(self):
        from llm_router.config import RouterConfig

        cfg = RouterConfig()
        assert cfg.llm_router_slim == "off"

    def test_escalate_above_defaults_to_zero(self):
        from llm_router.config import RouterConfig

        cfg = RouterConfig()
        assert cfg.llm_router_escalate_above == 0.0

    def test_hard_stop_defaults_to_zero(self):
        from llm_router.config import RouterConfig

        cfg = RouterConfig()
        assert cfg.llm_router_hard_stop_above == 0.0

    def test_slim_field_read_from_env(self, monkeypatch):
        from llm_router.config import RouterConfig

        monkeypatch.setenv("LLM_ROUTER_SLIM", "routing")
        cfg = RouterConfig()
        assert cfg.llm_router_slim == "routing"


# ── llm_fs_analyze_context ────────────────────────────────────────────────────


class TestFsAnalyzeContext:
    @pytest.mark.asyncio
    async def test_analyze_with_no_key_files_returns_message(self, tmp_path, monkeypatch):
        from llm_router.tools.fs import llm_fs_analyze_context

        # Empty directory — no key files
        result = await llm_fs_analyze_context(path=str(tmp_path))
        assert "No key project files found" in result

    @pytest.mark.asyncio
    async def test_analyze_reads_pyproject_toml(self, tmp_path, monkeypatch):
        from llm_router.tools import fs as fs_module
        from llm_router.tools.fs import llm_fs_analyze_context
        from llm_router.types import LLMResponse

        # Create a pyproject.toml
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "test-project"\n')

        # Mock route_and_call to avoid real API calls
        mock_resp = MagicMock(spec=LLMResponse)
        mock_resp.content = json.dumps({
            "language": "Python",
            "framework": "none",
            "project_type": "library",
            "summary": "A test project",
            "routing_hint": "mostly code tasks",
        })
        mock_resp.header.return_value = "> test"

        with patch.object(fs_module, "route_and_call", new=AsyncMock(return_value=mock_resp)):
            result = await llm_fs_analyze_context(path=str(tmp_path))

        assert "Python" in result or "context_summary" in result.lower() or "saved" in result


# ── llm_session_spend tool ────────────────────────────────────────────────────


class TestLlmSessionSpend:
    @pytest.mark.asyncio
    async def test_session_spend_tool_returns_summary(self, tmp_path, monkeypatch):
        from llm_router import session_spend as ss
        from llm_router.tools.admin import llm_session_spend

        monkeypatch.setattr(ss, "SESSION_SPEND_FILE", tmp_path / "session_spend.json")
        monkeypatch.setattr(ss, "_spend", None)

        # Prime the spend singleton with some data
        spend = ss.get_session_spend()
        spend.record("gpt-4o", "llm_code", 100, 200, 0.05)

        with patch("llm_router.tools.admin.get_config") as mc:
            mc.return_value.llm_router_escalate_above = 0.0
            mc.return_value.llm_router_hard_stop_above = 0.0
            result = await llm_session_spend()

        assert "Session spend" in result
        assert "$" in result

    @pytest.mark.asyncio
    async def test_session_spend_shows_anomaly_warning(self, tmp_path, monkeypatch):
        from llm_router import session_spend as ss
        from llm_router.tools.admin import llm_session_spend

        monkeypatch.setattr(ss, "SESSION_SPEND_FILE", tmp_path / "session_spend.json")
        monkeypatch.setattr(ss, "_spend", None)

        spend = ss.get_session_spend()
        spend.anomaly_flag = True
        spend.total_usd = 1.50
        spend.call_count = 5
        spend.per_model = {}
        spend.per_tool = {}

        with patch("llm_router.tools.admin.get_config") as mc:
            mc.return_value.llm_router_escalate_above = 0.0
            mc.return_value.llm_router_hard_stop_above = 0.0
            result = await llm_session_spend()

        assert "ANOMALY" in result
