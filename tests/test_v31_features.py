"""Tests for v3.1.0 features: savings import wiring, host column, llm_auto, host install."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from llm_router import cost


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def savings_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    log_path = tmp_path / "savings_log.jsonl"
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(db_path))
    monkeypatch.setattr(cost, "SAVINGS_LOG_PATH", log_path)
    return db_path, log_path


# ── Phase 1: host column in savings_stats ────────────────────────────────────


class TestSavingsStatsHostColumn:
    @pytest.mark.asyncio
    async def test_import_with_host_field(self, savings_db):
        _, log_path = savings_db
        entry = {
            "timestamp": "2026-04-01T10:00:00Z",
            "session_id": "s1",
            "task_type": "query",
            "estimated_saved": 0.033,
            "external_cost": 0.001,
            "model": "gemini-flash",
            "host": "codex",
        }
        log_path.write_text(json.dumps(entry) + "\n")

        imported = await cost.import_savings_log()
        assert imported == 1

        # Verify host is stored — query savings_stats directly
        import aiosqlite
        from llm_router.config import get_config
        db = await aiosqlite.connect(str(get_config().llm_router_db_path))
        try:
            cursor = await db.execute("SELECT host FROM savings_stats LIMIT 1")
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == "codex"
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_import_without_host_defaults_to_claude_code(self, savings_db):
        _, log_path = savings_db
        entry = {
            "timestamp": "2026-04-01T10:00:00Z",
            "session_id": "s1",
            "task_type": "code",
            "estimated_saved": 0.05,
            "external_cost": 0.002,
            "model": "gpt-4o-mini",
            # no "host" key
        }
        log_path.write_text(json.dumps(entry) + "\n")

        imported = await cost.import_savings_log()
        assert imported == 1

        import aiosqlite
        from llm_router.config import get_config
        db = await aiosqlite.connect(str(get_config().llm_router_db_path))
        try:
            cursor = await db.execute("SELECT host FROM savings_stats LIMIT 1")
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == "claude_code"
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_multi_host_entries(self, savings_db):
        _, log_path = savings_db
        entries = [
            {"session_id": "s1", "task_type": "query", "estimated_saved": 0.03,
             "external_cost": 0.001, "model": "flash", "host": "claude_code"},
            {"session_id": "s2", "task_type": "code", "estimated_saved": 0.05,
             "external_cost": 0.002, "model": "gpt-4o", "host": "codex"},
            {"session_id": "s3", "task_type": "research", "estimated_saved": 0.10,
             "external_cost": 0.005, "model": "sonar", "host": "desktop"},
        ]
        log_path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

        imported = await cost.import_savings_log()
        assert imported == 3

        summary = await cost.get_lifetime_savings_summary(days=0)
        assert summary["tasks_routed"] == 3


# ── Phase 1: import wiring in admin tools ────────────────────────────────────


class TestImportWiringInAdminTools:
    @pytest.mark.asyncio
    async def test_llm_savings_flushes_jsonl(self, savings_db, monkeypatch):
        """llm_savings() should auto-import pending JSONL before returning stats."""
        _, log_path = savings_db
        entry = {
            "session_id": "s1", "task_type": "query",
            "estimated_saved": 0.033, "external_cost": 0.001,
            "model": "flash", "host": "claude_code",
        }
        log_path.write_text(json.dumps(entry) + "\n")

        # JSONL is written but not yet imported
        initial = await cost.get_lifetime_savings_summary(days=0)
        assert initial["tasks_routed"] == 0

        # llm_savings should trigger the import
        from llm_router.tools.admin import llm_savings
        result = await llm_savings()

        assert "Savings" in result  # tool returned output
        # JSONL file should be truncated after import
        assert log_path.read_text() == ""

    @pytest.mark.asyncio
    async def test_llm_usage_flushes_jsonl(self, savings_db):
        """llm_usage() should auto-import pending JSONL before the savings section."""
        _, log_path = savings_db
        entry = {
            "session_id": "s1", "task_type": "query",
            "estimated_saved": 0.033, "external_cost": 0.0,
            "model": "flash", "host": "claude_code",
        }
        log_path.write_text(json.dumps(entry) + "\n")

        from llm_router.tools.admin import llm_usage
        result = await llm_usage("all")

        assert "Usage Dashboard" in result
        assert log_path.read_text() == ""


# ── Phase 2: llm_auto tool ───────────────────────────────────────────────────


class TestLlmAutoRegistered:
    def test_llm_auto_in_server_tools(self):
        from llm_router.server import mcp
        names = {t.name for t in mcp._tool_manager.list_tools()}
        assert "llm_auto" in names


class TestLlmAutoSavingsEnvelope:
    @pytest.mark.asyncio
    async def test_savings_envelope_shown_at_5th_call(self, savings_db):
        """After 5 tasks_routed, llm_auto appends a savings envelope."""
        db_path, _ = savings_db
        # Pre-populate savings_stats with 5 entries
        for i in range(5):
            await cost.log_savings(
                task_type="query",
                estimated_saved=0.03,
                external_cost=0.001,
                model="flash",
                session_id=f"s{i}",
            )

        from llm_router.cost import get_lifetime_savings_summary
        summary = await get_lifetime_savings_summary(days=0)
        assert summary["tasks_routed"] == 5
        # 5 % 5 == 0, so a savings message would be shown

    @pytest.mark.asyncio
    async def test_savings_envelope_not_shown_at_3rd_call(self, savings_db):
        """At 3 tasks_routed (not a multiple of 5), no savings envelope."""
        for i in range(3):
            await cost.log_savings("query", 0.03, 0.001, "flash", f"s{i}")

        from llm_router.cost import get_lifetime_savings_summary
        summary = await get_lifetime_savings_summary(days=0)
        assert summary["tasks_routed"] % 5 != 0


# ── Phase 3: --host install CLI ──────────────────────────────────────────────


class TestInstallHost:
    def test_install_host_codex(self, capsys):
        from llm_router.cli import _install_host
        _install_host("codex")
        out = capsys.readouterr().out
        assert "Codex CLI" in out
        assert "config.yaml" in out
        assert "no files are modified" in out.lower() or "COPY-PASTE" in out

    def test_install_host_desktop(self, capsys):
        from llm_router.cli import _install_host
        _install_host("desktop")
        out = capsys.readouterr().out
        assert "Claude Desktop" in out
        assert "claude_desktop_config.json" in out

    def test_install_host_copilot(self, capsys):
        from llm_router.cli import _install_host
        _install_host("copilot")
        out = capsys.readouterr().out
        assert "Copilot" in out
        assert "mcp.json" in out

    def test_install_host_all(self, capsys):
        from llm_router.cli import _install_host
        _install_host("all")
        out = capsys.readouterr().out
        assert "Codex CLI" in out
        assert "Claude Desktop" in out
        assert "Copilot" in out

    def test_install_host_unknown(self, capsys):
        from llm_router.cli import _install_host
        _install_host("nonexistent")
        out = capsys.readouterr().out
        assert "Unknown host" in out


class TestInstallHostRulesFilesExist:
    def test_codex_rules_exists(self):
        from llm_router import __file__ as pkg_init
        rules = Path(pkg_init).parent / "rules" / "codex-rules.md"
        assert rules.exists(), f"codex-rules.md not found at {rules}"

    def test_desktop_rules_exists(self):
        from llm_router import __file__ as pkg_init
        rules = Path(pkg_init).parent / "rules" / "desktop-rules.md"
        assert rules.exists(), f"desktop-rules.md not found at {rules}"

    def test_copilot_rules_exists(self):
        from llm_router import __file__ as pkg_init
        rules = Path(pkg_init).parent / "rules" / "copilot-rules.md"
        assert rules.exists(), f"copilot-rules.md not found at {rules}"
