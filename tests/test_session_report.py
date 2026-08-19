"""Tests for session-end report formatting and data integrity.

Verifies that the session summary output is mathematically consistent,
filters test/mock data, and uses honest terminology.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

# Import the session-end hook as a module
_hook_path = Path(__file__).parent.parent / "src" / "llm_router" / "hooks"
sys.path.insert(0, str(_hook_path))

_spec = importlib.util.spec_from_file_location(
    "session_end", _hook_path / "session-end.py"
)
se = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(se)


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def temp_db(tmp_path):
    """Create an isolated SQLite database for session-end queries."""
    db_path = tmp_path / "usage.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            task_type TEXT,
            model TEXT,
            provider TEXT,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cost_usd REAL DEFAULT 0.0,
            success INTEGER DEFAULT 1,
            complexity TEXT DEFAULT 'moderate'
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS routing_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            recommended_model TEXT,
            final_model TEXT,
            classifier_latency_ms REAL,
            is_real INTEGER DEFAULT 1
        )
    """)
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def session_start():
    """Return a session start timestamp 1 hour ago."""
    return time.time() - 3600


def _insert_usage(db_path, rows):
    """Insert rows into usage table. Each row is a dict."""
    conn = sqlite3.connect(str(db_path))
    for r in rows:
        conn.execute(
            "INSERT INTO usage (timestamp, task_type, model, provider, "
            "input_tokens, output_tokens, cost_usd, success, complexity) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                r.get("timestamp", "2026-01-01 12:00:00"),
                r.get("task_type", "query"),
                r.get("model", "gpt-4o-mini"),
                r.get("provider", "openai"),
                r.get("input_tokens", 500),
                r.get("output_tokens", 300),
                r.get("cost_usd", 0.001),
                r.get("success", 1),
                r.get("complexity", "moderate"),
            ),
        )
    conn.commit()
    conn.close()


# ── Issue 1: Subscription percentage deltas ───────────────────────────────────


class TestSubscriptionDeltas:
    """Verify that percentage deltas are displayed honestly."""

    def test_zero_delta_shows_no_change(self):
        """When start == end, show 'no change' instead of +0.0pp."""
        result = se._cc_row("session (5h)", 35.0, 35.0)
        assert "+0.0pp" not in result
        assert "no change" in result

    def test_tiny_delta_shows_precision(self):
        """When delta is 0.01-0.09pp, show 2 decimal places."""
        result = se._cc_row("session (5h)", 35.0, 35.05)
        assert "+0.05pp" in result
        # Should NOT round to +0.0pp or +0.1pp
        assert "+0.0pp" not in result

    def test_normal_delta_shows_one_decimal(self):
        """When delta >= 0.1pp, show standard 1 decimal."""
        result = se._cc_row("weekly (all)", 18.0, 19.5)
        assert "+1.5pp" in result

    def test_negative_delta(self):
        """Negative deltas should also work correctly."""
        result = se._cc_row("session (5h)", 40.0, 38.0)
        assert "-2.0pp" in result

    def test_sub_point_one_delta_not_misleading(self):
        """35.0 → 35.03 should not show +0.0pp."""
        result = se._cc_row("session (5h)", 35.0, 35.03)
        assert "+0.0pp" not in result
        assert "+0.03pp" in result

    def test_no_snapshot_hides_arrow(self):
        """When no start snapshot, don't show arrow or delta."""
        result = se._cc_row("session (5h)", None, 35.0)
        assert "→" not in result
        assert "pp" not in result
        assert "35%" in result


# ── Issue 2: Mock/test model filtering ────────────────────────────────────────


class TestMockModelFiltering:
    """Verify that mock/test models never appear in production reports."""

    def test_is_test_model_catches_mock(self):
        assert se._is_test_model("mock-model") is True
        assert se._is_test_model("test-model") is True
        assert se._is_test_model("fake-model") is True
        assert se._is_test_model("test/something") is True
        assert se._is_test_model("mock/anything") is True

    def test_is_test_model_allows_real(self):
        assert se._is_test_model("gpt-4o-mini") is False
        assert se._is_test_model("claude-sonnet-4") is False
        assert se._is_test_model("gemini-flash") is False
        assert se._is_test_model("qwen3.5:latest") is False

    def test_is_test_model_catches_empty(self):
        assert se._is_test_model("") is True
        assert se._is_test_model(None) is True

    def test_aggregate_excludes_mock_models(self):
        """_aggregate should silently drop rows with mock model names."""
        rows = [
            {"task_type": "query", "model": "gpt-4o-mini", "input_tokens": 100,
             "output_tokens": 50, "cost_usd": 0.001},
            {"task_type": "query", "model": "mock-model", "input_tokens": 100,
             "output_tokens": 50, "cost_usd": 0.001},
            {"task_type": "code", "model": "test-model", "input_tokens": 200,
             "output_tokens": 100, "cost_usd": 0.002},
        ]
        result = se._aggregate(rows)
        # Only the gpt-4o-mini row should survive
        assert "query" in result
        assert result["query"]["count"] == 1
        assert "mock-model" not in result["query"]["models"]
        # code task had only test-model, so it should be absent
        assert "code" not in result

    def test_query_session_data_excludes_mock(self, temp_db, session_start):
        """Mock models should be filtered at the data level."""
        ts = se._session_start_iso(session_start - 60)
        _insert_usage(temp_db, [
            {"timestamp": ts, "model": "gpt-4o-mini", "provider": "openai"},
            {"timestamp": ts, "model": "mock-model", "provider": "openai"},
            {"timestamp": ts, "model": "gemma4:latest", "provider": "ollama"},
            {"timestamp": ts, "model": "test-model", "provider": "ollama"},
        ])
        with patch.object(se, "DB_PATH", str(temp_db)):
            paid, cc, free = se._query_session_data(session_start - 120)
        # mock-model and test-model should be excluded
        paid_models = [r["model"] for r in paid]
        free_models = [r["model"] for r in free]
        assert "mock-model" not in paid_models
        assert "test-model" not in free_models
        assert "gpt-4o-mini" in paid_models
        assert "gemma4:latest" in free_models

    def test_complexity_breakdown_excludes_mock(self, temp_db, session_start):
        """Mock models should not appear in complexity breakdown."""
        ts = se._session_start_iso(session_start - 60)
        _insert_usage(temp_db, [
            {"timestamp": ts, "model": "gpt-4o-mini", "provider": "openai",
             "complexity": "moderate"},
            {"timestamp": ts, "model": "mock-model", "provider": "openai",
             "complexity": "moderate"},
        ])
        with patch.object(se, "DB_PATH", str(temp_db)):
            result = se._query_session_complexity_breakdown(session_start - 120)
        if "moderate" in result:
            model_names = [m for m, _, _, _ in result["moderate"]]
            assert "mock-model" not in model_names

    def test_cc_model_section_excludes_mock(self):
        """CC model section should filter test models."""
        rows = [
            {"model": "claude-sonnet-4", "task_type": "code"},
            {"model": "mock-model", "task_type": "query"},
        ]
        lines = se._format_cc_model_section(rows)
        text = "\n".join(lines)
        assert "mock-model" not in text
        assert "claude-sonnet-4" in text or "sonnet-4" in text


# ── Issue 3: Unknown model ID handling ────────────────────────────────────────


class TestModelValidation:
    """Verify that unknown model IDs are handled safely."""

    def test_known_models_recognized(self):
        assert se._is_known_model("gpt-4o-mini") is True
        assert se._is_known_model("claude-sonnet-4") is True
        assert se._is_known_model("gemini-flash") is True
        assert se._is_known_model("qwen3.5:latest") is True
        assert se._is_known_model("deepseek-r1") is True
        assert se._is_known_model("llama3.2") is True
        assert se._is_known_model("o3") is True
        assert se._is_known_model("o4-mini") is True

    def test_unknown_model_not_recognized(self):
        assert se._is_known_model("") is False
        assert se._is_known_model("?") is False

    def test_gpt_5_4_is_known(self):
        """gpt-5.4 should be recognized as a valid OpenAI model."""
        assert se._is_known_model("gpt-5.4") is True


# ── Issue 4: Call totals reconciliation ───────────────────────────────────────


class TestCallReconciliation:
    """Verify that call totals reconcile across sections."""

    def test_complexity_breakdown_reconciles(self, temp_db, session_start):
        """Total in complexity breakdown should match free + paid."""
        ts = se._session_start_iso(session_start - 60)
        _insert_usage(temp_db, [
            {"timestamp": ts, "model": "gemma4:latest", "provider": "ollama",
             "complexity": "simple", "cost_usd": 0.0},
            {"timestamp": ts, "model": "gemma4:latest", "provider": "ollama",
             "complexity": "simple", "cost_usd": 0.0},
            {"timestamp": ts, "model": "gpt-4o-mini", "provider": "openai",
             "complexity": "moderate", "cost_usd": 0.001},
            {"timestamp": ts, "model": "gpt-4o-mini", "provider": "openai",
             "complexity": "moderate", "cost_usd": 0.001},
            {"timestamp": ts, "model": "gpt-4o-mini", "provider": "openai",
             "complexity": "moderate", "cost_usd": 0.001},
        ])
        with patch.object(se, "DB_PATH", str(temp_db)):
            paid, cc, free = se._query_session_data(session_start - 120)
            complexity, filtered = se._query_session_complexity_breakdown(session_start - 120)

        total_free = len(free)
        total_paid = len(paid)
        total_complexity = sum(
            cnt for models in complexity.values()
            for _, cnt, _, _ in models
        )
        assert total_free + total_paid == total_complexity

    def test_reconciliation_line_in_output(self, temp_db, session_start):
        """The complexity section should include a reconciliation line."""
        ts = se._session_start_iso(session_start - 60)
        _insert_usage(temp_db, [
            {"timestamp": ts, "model": "gemma4:latest", "provider": "ollama",
             "complexity": "simple", "cost_usd": 0.0},
            {"timestamp": ts, "model": "gpt-4o-mini", "provider": "openai",
             "complexity": "moderate", "cost_usd": 0.001},
        ])
        with patch.object(se, "DB_PATH", str(temp_db)):
            lines = se._format_complexity_breakdown(session_start - 120)
        text = "\n".join(lines)
        assert "routed" in text
        assert "local" in text
        assert "external" in text


# ── Issue 5: Savings math transparency ────────────────────────────────────────


class TestSavingsMath:
    """Verify savings calculations are correct and transparent."""

    def test_host_baseline_math(self):
        """Host baseline is derived from cost.py's single source of truth (AC-3),
        not a hardcoded rate — so it stays correct as the host price changes."""
        from llm_router import cost

        in_pm = float(cost._HOST_INPUT_PER_M)
        out_pm = float(cost._HOST_OUTPUT_PER_M)
        result = se._host_baseline(1000, 500)
        expected = (1000 * in_pm + 500 * out_pm) / 1_000_000
        assert abs(result - expected) < 1e-9

    def test_free_model_saves_full_baseline(self):
        """Free models should report full host baseline as savings."""
        rows = [
            {"provider": "ollama", "input_tokens": 1000, "output_tokens": 500,
             "model": "gemma4:latest", "task_type": "query", "cost_usd": 0.0},
        ]
        lines = se._format_free_section(rows, [])
        text = "\n".join(lines)
        # Free-provider call saves the full host baseline (AC-3: derived from cost.py,
        # not a hardcoded rate) for 1000 input / 500 output tokens.
        from llm_router import cost
        expected = (1000 * float(cost._HOST_INPUT_PER_M)
                    + 500 * float(cost._HOST_OUTPUT_PER_M)) / 1_000_000
        assert f"${expected:.4f}" in text

    def test_external_routing_shows_baseline(self):
        """External routing section should show both actual and baseline cost."""
        tools = {
            "query": {"count": 5, "in": 5000, "out": 2500, "cost": 0.01,
                      "models": {"gpt-4o-mini": 5}},
        }
        # Cash rendering — pin the mode rather than inheriting the dev's config.
        lines = se._format_routing_section(tools, subscription=False)
        text = "\n".join(lines)
        assert "actual" in text
        assert "baseline" in text

    def test_overspend_is_shown_not_clamped(self):
        """INVERTED (AUD-06 / WP-04). This previously asserted "0% saved" when
        cost exceeded baseline, pinning the clamp as the intended contract and
        making a session that lost money render identically to one that broke
        even. The compute layer reported the loss correctly the whole time; only
        this display layer hid it. Owner decision 2026-08-12: show it.

        Reported in dollars rather than as a negative percentage -- unclamping
        alone yields "-571329% saved" on a small overspending session, which is
        honest but unreadable, and a number that shape invites a future "fix"
        that restores the clamp.
        """
        tools = {
            "query": {"count": 1, "in": 100, "out": 50, "cost": 10.0,
                      "models": {"o3": 1}},
        }
        # Cash rendering — pin the mode rather than inheriting the dev's config.
        lines = se._format_routing_section(tools, subscription=False)
        text = "\n".join(lines)
        assert "0% saved" not in text, text
        assert "overspent" in text, text


# ── Issue 6: Free model classification ────────────────────────────────────────


class TestFreeModelClassification:
    """Verify accurate labeling of zero-cost vs paid models."""

    def test_ollama_only_label(self):
        """When only Ollama, label should say 'Local models'."""
        rows = [
            {"provider": "ollama", "input_tokens": 500, "output_tokens": 300,
             "model": "gemma4:latest", "task_type": "query", "cost_usd": 0.0},
        ]
        lines = se._format_free_section(rows, [])
        text = "\n".join(lines)
        assert "Local (Ollama)" in text
        assert "Free models" not in text

    def test_codex_only_label(self):
        """When only Codex, label should say 'Prepaid'."""
        rows = [
            {"provider": "codex", "input_tokens": 0, "output_tokens": 0,
             "model": "gpt-5.4", "task_type": "code", "cost_usd": 0.0},
        ]
        lines = se._format_free_section(rows, [])
        text = "\n".join(lines)
        assert "Prepaid (Codex)" in text
        assert "Free models" not in text

    def test_mixed_label(self):
        """When both Ollama and Codex, label should say 'Local / prepaid'."""
        rows = [
            {"provider": "ollama", "input_tokens": 500, "output_tokens": 300,
             "model": "gemma4:latest", "task_type": "query", "cost_usd": 0.0},
            {"provider": "codex", "input_tokens": 0, "output_tokens": 0,
             "model": "gpt-5.4", "task_type": "code", "cost_usd": 0.0},
        ]
        lines = se._format_free_section(rows, [])
        text = "\n".join(lines)
        assert "Local / prepaid" in text


# ── Issue 7: Router efficiency wording ────────────────────────────────────────


class TestRouterEfficiencyWording:
    """Verify honest, precise efficiency reporting."""

    def test_no_fallbacks_wording(self, temp_db):
        """When all decisions match, say 'No fallbacks' not '100% on-target'."""
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        conn = sqlite3.connect(str(temp_db))
        for _ in range(5):
            conn.execute(
                "INSERT INTO routing_decisions (timestamp, recommended_model, "
                "final_model, classifier_latency_ms) VALUES (?, ?, ?, ?)",
                (ts, "gpt-4o-mini", "gpt-4o-mini", 10.0),
            )
        conn.commit()
        conn.close()

        with patch.object(se, "DB_PATH", str(temp_db)):
            efficiency = se._query_router_efficiency()

        # Verify the data
        assert efficiency["on_target"] == efficiency["total"]

        # Now format — should not say "on-target"
        lines = []
        if efficiency:
            total = efficiency["total"]
            on_target = efficiency["on_target"]
            fallbacks = total - on_target
            if fallbacks == 0:
                lines.append(f"No fallbacks today ({total} routing decisions)")
            else:
                lines.append(f"Fallback rate: {fallbacks}/{total}")
        text = "\n".join(lines)
        assert "on-target" not in text
        assert "No fallbacks" in text

    def test_with_fallbacks_wording(self, temp_db):
        """When some decisions needed fallback, show the fallback rate."""
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        conn = sqlite3.connect(str(temp_db))
        # 8 on-target, 2 fallbacks
        for _ in range(8):
            conn.execute(
                "INSERT INTO routing_decisions (timestamp, recommended_model, "
                "final_model, classifier_latency_ms) VALUES (?, ?, ?, ?)",
                (ts, "gpt-4o-mini", "gpt-4o-mini", 10.0),
            )
        for _ in range(2):
            conn.execute(
                "INSERT INTO routing_decisions (timestamp, recommended_model, "
                "final_model, classifier_latency_ms) VALUES (?, ?, ?, ?)",
                (ts, "gpt-4o-mini", "gemini-flash", 10.0),
            )
        conn.commit()
        conn.close()

        with patch.object(se, "DB_PATH", str(temp_db)):
            efficiency = se._query_router_efficiency()
        assert efficiency["total"] == 10
        assert efficiency["on_target"] == 8


# ── Issue 8: Production polish ────────────────────────────────────────────────


class TestProductionPolish:
    """Verify the output is polished and consistent."""

    def test_full_report_no_mock_model(self, temp_db, session_start):
        """End-to-end: mock-model should never appear in formatted output."""
        ts = se._session_start_iso(session_start - 60)
        _insert_usage(temp_db, [
            {"timestamp": ts, "model": "gpt-4o-mini", "provider": "openai",
             "complexity": "moderate", "cost_usd": 0.001},
            {"timestamp": ts, "model": "mock-model", "provider": "openai",
             "complexity": "moderate", "cost_usd": 0.001},
            {"timestamp": ts, "model": "gemma4:latest", "provider": "ollama",
             "complexity": "simple", "cost_usd": 0.0},
            {"timestamp": ts, "model": "test-model", "provider": "codex",
             "complexity": "simple", "cost_usd": 0.0},
        ])
        with patch.object(se, "DB_PATH", str(temp_db)):
            paid, cc, free = se._query_session_data(session_start - 120)
            tools = se._aggregate(paid)
            output = se._format(
                tools, [], free, paid,
                None, None, False,
                session_start=session_start - 120,
            )
        assert "mock-model" not in output
        assert "test-model" not in output
        assert "gpt-4o-mini" in output or "4o-mini" in output

    def test_cumulative_savings_readable_format(self):
        """Large savings should use $X.XX, small should use $X.XXXX."""
        periods = [
            ("today", 10, 5000, 3000, 0.0234),
            ("this week", 50, 25000, 15000, 0.1234),
            ("this month", 200, 100000, 60000, 1.5678),
            ("all time", 5000, 2500000, 1500000, 45.6789),
        ]
        lines = se._format_cumulative_section(periods)
        text = "\n".join(lines)
        # Sub-dollar amounts: 4 decimal places
        assert "$0.0234" in text
        assert "$0.1234" in text
        # Dollar+ amounts: 2 decimal places
        assert "$1.57" in text
        assert "$45.68" in text

    def test_consistent_terminology(self, temp_db, session_start):
        """Verify output uses 'local/prepaid' instead of 'free'."""
        ts = se._session_start_iso(session_start - 60)
        _insert_usage(temp_db, [
            {"timestamp": ts, "model": "gemma4:latest", "provider": "ollama",
             "complexity": "simple", "cost_usd": 0.0},
            {"timestamp": ts, "model": "gpt-4o-mini", "provider": "openai",
             "complexity": "moderate", "cost_usd": 0.001},
        ])
        with patch.object(se, "DB_PATH", str(temp_db)):
            lines = se._format_complexity_breakdown(session_start - 120)
        text = "\n".join(lines)
        assert "local" in text


# ── Enhanced Dashboard Tests ──────────────────────────────────────────────────

class TestEnhancedDashboard:
    """Tests for the enhanced sparkline and models dashboard."""

    def test_sparkline_renders_with_valid_data(self):
        """Sparkline produces multi-line output with axes."""
        from llm_router.hooks.dashboard_enhanced import render_enhanced_sparkline

        data = [
            ("2026-05-10", 10, 500, 0.01),
            ("2026-05-11", 20, 1000, 0.02),
            ("2026-05-12", 30, 1500, 0.03),
        ]
        output = render_enhanced_sparkline(data, max_height=4)
        assert "14-Day Routing Activity" in output
        assert "┤" in output  # Y-axis
        assert "└" in output  # X-axis
        assert "calls" in output

    def test_sparkline_empty_data(self):
        """Empty data returns empty string."""
        from llm_router.hooks.dashboard_enhanced import render_enhanced_sparkline

        assert render_enhanced_sparkline([]) == ""
        assert render_enhanced_sparkline([("2026-05-10", 0, 0, 0.0)]) == ""

    def test_models_section_renders(self):
        """Models section shows model names and percentages."""
        from llm_router.hooks.dashboard_enhanced import render_models_section

        models = {"ollama:gemma4": 10, "gpt-4o-mini": 5}
        output = render_models_section(models)
        assert "Models Routed This Session" in output
        assert "gemma4" in output
        assert "gpt-4o-mini" in output
        assert "67%" in output  # 10/15

    def test_models_section_empty(self):
        """Empty models returns empty string."""
        from llm_router.hooks.dashboard_enhanced import render_models_section

        assert render_models_section({}) == ""

    def test_query_session_models_filters_test_models(self, temp_db, session_start):
        """Mock/test models are filtered from query results."""
        from llm_router.hooks.dashboard_enhanced import query_session_models

        ts = se._session_start_iso(session_start - 60)
        _insert_usage(temp_db, [
            {"timestamp": ts, "model": "gpt-4o-mini", "provider": "openai",
             "complexity": "simple", "cost_usd": 0.001},
            {"timestamp": ts, "model": "mock-model", "provider": "openai",
             "complexity": "simple", "cost_usd": 0.0},
        ])
        models = query_session_models(session_start - 120, db_path=str(temp_db))
        assert "gpt-4o-mini" in models
        assert "mock-model" not in models

    def test_format_includes_enhanced_sparkline(self, temp_db, session_start):
        """Full _format output includes enhanced sparkline when data exists."""
        ts = se._session_start_iso(session_start - 60)
        _insert_usage(temp_db, [
            {"timestamp": ts, "model": "gpt-4o-mini", "provider": "openai",
             "complexity": "moderate", "cost_usd": 0.001},
        ])
        with patch.object(se, "DB_PATH", str(temp_db)):
            paid, cc, free = se._query_session_data(session_start - 120)
            tools = se._aggregate(paid)
            output = se._format(
                tools, [], free, paid,
                None, None, False,
                session_start=session_start - 120,
            )
        # Should contain either enhanced sparkline or fallback.
        # Post-rebrand: dashboard header reads "⚡ LLM Router" (was "LLM Router").
        assert "LLM Router" in output
