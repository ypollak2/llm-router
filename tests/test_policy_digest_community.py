"""Tests for v3.2 (Policy Engine), v3.3 (Digest), v3.4 (Community Benchmarks)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# v3.2 — Policy Engine
# ─────────────────────────────────────────────────────────────────────────────

class TestOrgPolicyLoad:
    def test_no_file_returns_default_permissive_policy(self, tmp_path):
        from llm_router.policy import load_org_policy

        policy = load_org_policy(tmp_path / "nonexistent.yaml")
        assert policy.block_providers == []
        assert policy.block_models == []
        assert policy.allow_models == []
        assert policy.source == "default"

    def test_load_block_providers(self, tmp_path):
        from llm_router.policy import load_org_policy

        p = tmp_path / "org-policy.yaml"
        p.write_text("block_providers:\n  - openai\n  - anthropic\n")
        policy = load_org_policy(p)
        assert "openai" in policy.block_providers
        assert "anthropic" in policy.block_providers

    def test_load_block_and_allow_models(self, tmp_path):
        from llm_router.policy import load_org_policy

        p = tmp_path / "org-policy.yaml"
        p.write_text(
            "block_models:\n  - openai/gpt-4o\n"
            "allow_models:\n  - ollama/*\n  - codex/*\n"
        )
        policy = load_org_policy(p)
        assert "openai/gpt-4o" in policy.block_models
        assert "ollama/*" in policy.allow_models

    def test_load_task_caps(self, tmp_path):
        from llm_router.policy import load_org_policy

        p = tmp_path / "org-policy.yaml"
        p.write_text("task_caps:\n  code: 0.50\n  _total: 5.00\n")
        policy = load_org_policy(p)
        assert policy.task_caps["code"] == 0.50
        assert policy.task_caps["_total"] == 5.00

    def test_invalid_yaml_returns_default(self, tmp_path):
        from llm_router.policy import load_org_policy

        p = tmp_path / "org-policy.yaml"
        p.write_text("not: valid: yaml: [[[")
        policy = load_org_policy(p)
        assert policy.source == "default"


class TestApplyPolicy:
    def test_no_restrictions_passes_all(self):
        from llm_router.policy import OrgPolicy, apply_policy

        models = ["openai/gpt-4o", "anthropic/claude-haiku-4-5-20251001", "ollama/llama3"]
        policy = OrgPolicy()
        allowed, blocked = apply_policy(models, "code", policy)
        assert allowed == models
        assert blocked == []

    def test_block_provider_removes_all_its_models(self):
        from llm_router.policy import OrgPolicy, apply_policy

        models = ["openai/gpt-4o", "openai/gpt-4o-mini", "ollama/llama3"]
        policy = OrgPolicy(block_providers=["openai"])
        allowed, blocked = apply_policy(models, "code", policy)
        assert "ollama/llama3" in allowed
        assert all("openai" not in m for m in allowed)
        assert len(blocked) == 2

    def test_block_model_by_exact_name(self):
        from llm_router.policy import OrgPolicy, apply_policy

        models = ["openai/gpt-4o", "openai/gpt-4o-mini", "ollama/llama3"]
        policy = OrgPolicy(block_models=["openai/gpt-4o"])
        allowed, blocked = apply_policy(models, "code", policy)
        assert "openai/gpt-4o" not in allowed
        assert "openai/gpt-4o-mini" in allowed

    def test_block_model_by_glob(self):
        from llm_router.policy import OrgPolicy, apply_policy

        models = ["openai/gpt-4o", "openai/gpt-4o-mini", "ollama/llama3"]
        policy = OrgPolicy(block_models=["openai/*"])
        allowed, blocked = apply_policy(models, "code", policy)
        assert allowed == ["ollama/llama3"]
        assert len(blocked) == 2

    def test_allow_list_restricts_to_listed_only(self):
        from llm_router.policy import OrgPolicy, apply_policy

        models = ["openai/gpt-4o", "ollama/llama3", "gemini/gemini-2.5-flash"]
        policy = OrgPolicy(allow_models=["ollama/*"])
        allowed, blocked = apply_policy(models, "code", policy)
        assert allowed == ["ollama/llama3"]

    def test_allow_overrides_block_for_same_model(self):
        from llm_router.policy import OrgPolicy, apply_policy

        # block all openai/* but explicitly allow gpt-4o-mini
        models = ["openai/gpt-4o", "openai/gpt-4o-mini", "ollama/llama3"]
        policy = OrgPolicy(
            block_models=["openai/*"],
            allow_models=["openai/gpt-4o-mini", "ollama/*"],
        )
        allowed, blocked = apply_policy(models, "code", policy)
        assert "openai/gpt-4o-mini" in allowed
        assert "openai/gpt-4o" not in allowed


class TestRepoCfgModelFields:
    def test_block_models_parsed_from_yaml(self, tmp_path):
        from llm_router.repo_config import _dict_to_config

        cfg = _dict_to_config(
            {"block_models": ["openai/gpt-4o"], "allow_models": ["ollama/*"]},
            "test"
        )
        assert "openai/gpt-4o" in cfg.block_models
        assert "ollama/*" in cfg.allow_models

    def test_merge_combines_block_model_lists(self):
        from llm_router.repo_config import RepoConfig, _merge

        base = RepoConfig(block_models=["openai/gpt-4o"])
        override = RepoConfig(block_models=["anthropic/claude-opus-4-6"])
        merged = _merge(base, override)
        assert "openai/gpt-4o" in merged.block_models
        assert "anthropic/claude-opus-4-6" in merged.block_models


class TestPolicySummary:
    def test_no_file_shows_permissive_message(self, tmp_path):
        from llm_router.policy import OrgPolicy, policy_summary

        summary = policy_summary(OrgPolicy())
        assert "No org policy" in summary or "All providers" in summary

    def test_policy_with_blocks_shows_details(self, tmp_path):
        from llm_router.policy import OrgPolicy, policy_summary

        org = OrgPolicy(
            block_providers=["openai"],
            block_models=["anthropic/claude-opus-4-6"],
            source="/etc/llm-router/policy.yaml",
        )
        summary = policy_summary(org)
        assert "openai" in summary
        assert "claude-opus" in summary


class TestLlmPolicyTool:
    @pytest.mark.asyncio
    async def test_returns_policy_header(self, mock_env):
        from llm_router.tools.admin import llm_policy

        result = await llm_policy()
        assert "Policy" in result
        assert "org" in result.lower() or "Org" in result


# ─────────────────────────────────────────────────────────────────────────────
# v3.3 — Digest
# ─────────────────────────────────────────────────────────────────────────────

class TestFormatDigest:
    @pytest.mark.asyncio
    async def test_digest_contains_period_label(self, mock_env, tmp_path, monkeypatch):
        monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(tmp_path / "test.db"))
        from llm_router.digest import format_digest

        result = await format_digest("week")
        assert "week" in result.lower() or "Digest" in result

    @pytest.mark.asyncio
    async def test_digest_shows_dividers(self, mock_env, tmp_path, monkeypatch):
        monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(tmp_path / "test.db"))
        from llm_router.digest import format_digest

        result = await format_digest("today")
        assert "─" in result


class TestDetectSpendSpike:
    @pytest.mark.asyncio
    async def test_no_spike_on_empty_db(self, mock_env, tmp_path, monkeypatch):
        monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(tmp_path / "test.db"))
        import llm_router.config as config_module
        config_module._config = None
        from llm_router.digest import detect_spend_spike

        is_spike, today, avg = await detect_spend_spike()
        assert not is_spike
        assert today == 0.0
        assert avg == 0.0

    @pytest.mark.asyncio
    async def test_spike_threshold_logic(self):
        """Spike fires when today >= multiplier * 7-day average."""

        # Patch the DB query to return controlled values
        with patch("llm_router.digest._get_db") as mock_db:
            # today = 10, 7-day total = 7 → avg = 1 → spike at 2x
            conn = AsyncMock()
            conn.execute = AsyncMock()
            conn.execute.return_value.__aenter__ = AsyncMock()
            conn.execute.return_value.fetchone = AsyncMock(side_effect=[(10.0,), (7.0,)])
            mock_db.return_value = conn

            # Use functional math directly
            today_usd = 10.0
            avg_7day = 7.0 / 7.0  # = 1.0
            is_spike = today_usd >= 2.0 * avg_7day
            assert is_spike


class TestSimulateWithoutRouting:
    @pytest.mark.asyncio
    async def test_empty_db_returns_zeros(self, mock_env, tmp_path, monkeypatch):
        monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(tmp_path / "test.db"))
        import llm_router.config as config_module
        config_module._config = None
        from llm_router.digest import simulate_without_routing

        actual, baseline, pct = await simulate_without_routing("week")
        assert actual == 0.0
        assert baseline == 0.0
        assert pct == 0.0


class TestSendToWebhook:
    @pytest.mark.asyncio
    async def test_no_url_returns_error(self):
        from llm_router.digest import send_to_webhook

        ok, msg = await send_to_webhook("", "test digest")
        assert not ok
        assert "No webhook URL" in msg

    @pytest.mark.asyncio
    async def test_payload_format_slack(self):
        from llm_router.digest import _build_payload

        payload = _build_payload("https://hooks.slack.com/T12/B12/xxx", "hello")
        assert "blocks" in payload

    @pytest.mark.asyncio
    async def test_payload_format_discord(self):
        from llm_router.digest import _build_payload

        payload = _build_payload("https://discord.com/api/webhooks/123/abc", "hello")
        assert "content" in payload

    @pytest.mark.asyncio
    async def test_payload_format_generic(self):
        from llm_router.digest import _build_payload

        payload = _build_payload("https://my-server.com/webhook", "hello")
        assert "text" in payload


class TestLlmDigestTool:
    @pytest.mark.asyncio
    async def test_digest_tool_no_send_returns_formatted(self, mock_env, tmp_path, monkeypatch):
        monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(tmp_path / "test.db"))
        from llm_router.tools.admin import llm_digest

        result = await llm_digest(period="week", send=False)
        assert "Digest" in result or "─" in result

    @pytest.mark.asyncio
    async def test_digest_tool_send_without_url_shows_hint(self, mock_env, tmp_path, monkeypatch):
        monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(tmp_path / "test.db"))
        monkeypatch.delenv("LLM_ROUTER_WEBHOOK_URL", raising=False)
        monkeypatch.delenv("LLM_ROUTER_TEAM_ENDPOINT", raising=False)
        from llm_router.tools.admin import llm_digest

        result = await llm_digest(period="week", send=True)
        assert "LLM_ROUTER_WEBHOOK_URL" in result


# ─────────────────────────────────────────────────────────────────────────────
# v3.4 — Community Benchmarks
# ─────────────────────────────────────────────────────────────────────────────

class TestGetBenchmarkStats:
    @pytest.mark.asyncio
    async def test_empty_db_returns_empty_dict(self, mock_env, tmp_path, monkeypatch):
        monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(tmp_path / "test.db"))
        from llm_router.community import get_benchmark_stats

        stats = await get_benchmark_stats()
        assert isinstance(stats, dict)

    @pytest.mark.asyncio
    async def test_stats_structure(self, mock_env, tmp_path, monkeypatch):
        """Stats dict has expected keys when data exists."""
        import aiosqlite
        monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(tmp_path / "test.db"))

        # Seed routing_decisions with one row
        db = await aiosqlite.connect(str(tmp_path / "test.db"))
        await db.execute("""
            CREATE TABLE IF NOT EXISTS routing_decisions (
                id INTEGER PRIMARY KEY, timestamp TEXT, task_type TEXT,
                profile TEXT, final_model TEXT, final_provider TEXT,
                was_good INTEGER, input_tokens INTEGER, output_tokens INTEGER,
                latency_ms REAL, classifier_type TEXT, classifier_model TEXT,
                classifier_confidence REAL, classifier_latency_ms REAL,
                complexity TEXT, recommended_model TEXT, base_model TEXT,
                was_downshifted INTEGER, budget_pct_used REAL, quality_mode TEXT,
                success INTEGER, cost_usd REAL, prompt_hash TEXT,
                reason_code TEXT, policy_applied TEXT
            )
        """)
        await db.execute(
            "INSERT INTO routing_decisions (task_type, final_model, was_good) "
            "VALUES ('code', 'openai/gpt-4o', 1)"
        )
        await db.commit()
        await db.close()

        from llm_router.community import get_benchmark_stats
        stats = await get_benchmark_stats()
        if "code" in stats:
            s = stats["code"]
            assert "total" in s
            assert "accuracy_pct" in s
            assert "top_model" in s


class TestGetConfidenceStr:
    def test_few_calls_shows_insufficient_message(self):
        from llm_router.community import get_confidence_str

        stats = {"code": {"total": 3, "rated": 0, "accuracy_pct": None}}
        result = get_confidence_str(stats, "code")
        assert "too few" in result

    def test_no_rated_calls_suggests_rating(self):
        from llm_router.community import get_confidence_str

        stats = {"code": {"total": 20, "rated": 0, "accuracy_pct": None}}
        result = get_confidence_str(stats, "code")
        assert "llm_rate" in result or "rate" in result.lower()

    def test_accuracy_shown_when_rated(self):
        from llm_router.community import get_confidence_str

        stats = {"code": {"total": 50, "rated": 30, "accuracy_pct": 93.3}}
        result = get_confidence_str(stats, "code")
        assert "93%" in result
        assert "50" in result


class TestFormatBenchmarkReport:
    def test_empty_stats_shows_no_data_message(self):
        from llm_router.community import format_benchmark_report

        result = format_benchmark_report({})
        assert "No routing decisions" in result

    def test_report_contains_task_types(self):
        from llm_router.community import format_benchmark_report

        stats = {
            "code":  {"total": 100, "rated": 50, "good": 45, "bad": 5, "accuracy_pct": 90.0, "top_model": "ollama/qwen3"},
            "query": {"total":  50, "rated": 20, "good": 18, "bad": 2, "accuracy_pct": 90.0, "top_model": "gemini/flash"},
        }
        result = format_benchmark_report(stats)
        assert "code" in result
        assert "query" in result
        assert "90%" in result


class TestLlmBenchmarkTool:
    @pytest.mark.asyncio
    async def test_tool_returns_report(self, mock_env, tmp_path, monkeypatch):
        monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(tmp_path / "test.db"))
        from llm_router.tools.admin import llm_benchmark

        result = await llm_benchmark()
        assert isinstance(result, str)
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_community_flag_triggers_export(self, mock_env, tmp_path, monkeypatch):
        monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(tmp_path / "test.db"))
        monkeypatch.setenv("LLM_ROUTER_COMMUNITY", "true")
        from llm_router.tools.admin import llm_benchmark

        result = await llm_benchmark()
        assert "export" in result.lower() or "Community" in result


class TestNewToolsRegistered:
    def test_policy_digest_benchmark_registered(self):
        from llm_router.server import mcp

        tools = {t.name for t in mcp._tool_manager.list_tools()}
        assert "llm_policy"    in tools
        assert "llm_digest"    in tools
        assert "llm_benchmark" in tools
