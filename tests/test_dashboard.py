"""Tests for v8.5.0 savings dashboard."""

from __future__ import annotations

from llm_router.tools.dashboard import (
    _format_tokens,
    _render_bar,
    _render_sparkline,
    _sonnet_baseline,
    _opus_baseline,
    _window_label,
    _window_to_sql,
    _render_dashboard,
)


class TestFormatTokens:
    def test_small(self):
        assert _format_tokens(500) == "500"

    def test_thousands(self):
        assert _format_tokens(1500) == "1.5K"

    def test_millions(self):
        assert _format_tokens(2_500_000) == "2.5M"

    def test_exact_thousand(self):
        assert _format_tokens(1000) == "1.0K"


class TestBaselines:
    def test_sonnet_baseline(self):
        # 1M input tokens at $3/M + 1M output at $15/M = $18
        result = _sonnet_baseline(1_000_000, 1_000_000)
        assert abs(result - 18.0) < 0.001

    def test_opus_baseline(self):
        # 1M input at $15/M + 1M output at $75/M = $90
        result = _opus_baseline(1_000_000, 1_000_000)
        assert abs(result - 90.0) < 0.001

    def test_zero_tokens(self):
        assert _sonnet_baseline(0, 0) == 0.0
        assert _opus_baseline(0, 0) == 0.0


class TestWindowConversion:
    def test_14d(self):
        assert "14 days" in _window_to_sql("14d")

    def test_3m(self):
        assert "90 days" in _window_to_sql("3m")

    def test_1y(self):
        assert "365 days" in _window_to_sql("1y")

    def test_all(self):
        assert "2020" in _window_to_sql("all")

    def test_default(self):
        assert "14 days" in _window_to_sql("unknown")

    def test_labels(self):
        assert _window_label("14d") == "LAST 14 DAYS"
        assert _window_label("3m") == "LAST 3 MONTHS"
        assert _window_label("1y") == "LAST YEAR"
        assert _window_label("all") == "ALL TIME"


class TestRenderBar:
    def test_full_bar(self):
        bar = _render_bar(100, 100, width=10)
        assert "█" * 10 in bar

    def test_half_bar(self):
        bar = _render_bar(50, 100, width=10)
        assert "█" * 5 in bar

    def test_zero(self):
        bar = _render_bar(0, 100, width=10)
        assert "█" not in bar

    def test_zero_max(self):
        bar = _render_bar(50, 0, width=10)
        assert bar == ""


class TestRenderSparkline:
    def test_basic(self):
        sparkline = _render_sparkline([0, 50, 100])
        assert len(sparkline) > 0  # has content

    def test_empty(self):
        assert _render_sparkline([]) == ""

    def test_uniform(self):
        sparkline = _render_sparkline([50, 50, 50])
        assert len(sparkline) > 0


class TestRenderDashboard:
    def test_renders_with_data(self):
        daily = [
            {"day": "2026-05-10", "tokens": 5000, "input_tokens": 2000,
             "output_tokens": 3000, "actual_cost": 0.001, "baseline_cost": 0.015,
             "saved": 0.014, "calls": 10},
            {"day": "2026-05-11", "tokens": 8000, "input_tokens": 3000,
             "output_tokens": 5000, "actual_cost": 0.002, "baseline_cost": 0.025,
             "saved": 0.023, "calls": 15},
        ]
        breakdown = [
            {"provider": "ollama", "tokens": 10000, "input_tokens": 4000,
             "output_tokens": 6000, "actual_cost": 0.0, "baseline_cost": 0.03,
             "saved": 0.03, "calls": 20},
            {"provider": "gemini", "tokens": 3000, "input_tokens": 1000,
             "output_tokens": 2000, "actual_cost": 0.003, "baseline_cost": 0.01,
             "saved": 0.007, "calls": 5},
        ]
        result = _render_dashboard(daily, breakdown, 0.001, "LAST 14 DAYS", "sonnet", False)

        assert "LAST 14 DAYS" in result
        assert "ollama" in result
        assert "gemini" in result
        assert "NET SAVED" in result
        assert "Classifier overhead" in result

    def test_subscription_notice(self):
        daily = [{"day": "2026-05-10", "tokens": 1000, "input_tokens": 500,
                  "output_tokens": 500, "actual_cost": 0.0, "baseline_cost": 0.01,
                  "saved": 0.01, "calls": 5}]
        breakdown = [{"provider": "ollama", "tokens": 1000, "input_tokens": 500,
                      "output_tokens": 500, "actual_cost": 0.0, "baseline_cost": 0.01,
                      "saved": 0.01, "calls": 5}]
        result = _render_dashboard(daily, breakdown, 0.0, "LAST 14 DAYS", "sonnet", True)

        assert "SUBSCRIPTION MODE" in result
        assert "quota freed" in result
        assert "flat-rate" in result

    def test_no_subscription_notice_when_api(self):
        daily = [{"day": "2026-05-10", "tokens": 1000, "input_tokens": 500,
                  "output_tokens": 500, "actual_cost": 0.001, "baseline_cost": 0.01,
                  "saved": 0.009, "calls": 5}]
        breakdown = [{"provider": "openai", "tokens": 1000, "input_tokens": 500,
                      "output_tokens": 500, "actual_cost": 0.001, "baseline_cost": 0.01,
                      "saved": 0.009, "calls": 5}]
        result = _render_dashboard(daily, breakdown, 0.0, "LAST 14 DAYS", "sonnet", False)

        assert "SUBSCRIPTION MODE" not in result

    def test_routing_distribution(self):
        daily = [{"day": "2026-05-10", "tokens": 5000, "input_tokens": 2000,
                  "output_tokens": 3000, "actual_cost": 0.001, "baseline_cost": 0.015,
                  "saved": 0.014, "calls": 25}]
        breakdown = [
            {"provider": "ollama", "tokens": 3000, "input_tokens": 1000,
             "output_tokens": 2000, "actual_cost": 0.0, "baseline_cost": 0.01,
             "saved": 0.01, "calls": 15},
            {"provider": "gemini", "tokens": 2000, "input_tokens": 1000,
             "output_tokens": 1000, "actual_cost": 0.001, "baseline_cost": 0.005,
             "saved": 0.004, "calls": 10},
        ]
        result = _render_dashboard(daily, breakdown, 0.0, "LAST 14 DAYS", "sonnet", False)

        assert "ROUTING DISTRIBUTION" in result
        assert "█" in result  # colored bars present
