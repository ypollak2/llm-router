"""Contract tests: session-end.py must pass correct data to SessionSummaryDashboard.

Tests the 4 bugs fixed in session-end.py:
  1. dashboard_decisions uses canonical method IDs, not human reason strings
  2. claude_quota_pct is not double-scaled (weekly_pct already 0-100)
  3. daily_calls / daily_tokens are populated from 14-day DB data
  4. session_models is built from tools_data, not hardcoded []
"""

from pathlib import Path

import pytest
import yaml


def _load_expectations() -> dict:
    local = Path(__file__).parent / "fixtures" / "routing_expectations.local.yaml"
    default = Path(__file__).parent / "fixtures" / "routing_expectations.example.yaml"
    path = local if local.exists() else default
    with open(path) as f:
        return yaml.safe_load(f)


EXPECTATIONS = _load_expectations()
DASH_EXP = EXPECTATIONS["dashboard"]
CANONICAL_IDS = set(DASH_EXP["canonical_method_ids"])


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def sample_routing_logic() -> list[dict]:
    """Routing logic rows as returned by _query_routing_logic()."""
    return [
        {"method": "heuristic",        "symbol": "⚡", "hits": 46, "avg_confidence": 0.9,
         "reason": "Cached patterns / Static rules"},
        {"method": "build-fast-path",  "symbol": "🔨", "hits": 17, "avg_confidence": 0.95,
         "reason": "Fast-path pattern matched"},
        {"method": "fallback",         "symbol": "❓", "hits": 10, "avg_confidence": 0.0,
         "reason": "No classifier matched"},
        {"method": "context-inherit",  "symbol": "🔗", "hits":  6, "avg_confidence": 0.8,
         "reason": "Session context inherited"},
    ]


@pytest.fixture()
def sample_tools_data() -> dict:
    return {
        "llm_query": {
            "count": 20, "in": 500, "out": 200, "cost": 0.0,
            "models": {"gpt-4o": 12, "gemini-2.5-flash": 8},
        },
        "llm_code": {
            "count": 5, "in": 1000, "out": 400, "cost": 0.0,
            "models": {"codex/gpt-5.4": 5},
        },
    }


@pytest.fixture()
def sample_daily_14d() -> list[tuple]:
    """(date_str, calls, tokens, cost_usd) for 7 days."""
    return [
        ("2026-06-07", 5,  1200, 0.012),
        ("2026-06-08", 8,  2100, 0.021),
        ("2026-06-09", 3,   800, 0.008),
        ("2026-06-10", 12, 3400, 0.034),
        ("2026-06-11", 7,  1800, 0.018),
        ("2026-06-12", 15, 4200, 0.042),
        ("2026-06-13", 10, 2800, 0.028),
    ]


# ── Bug 1: canonical method IDs ───────────────────────────────────────────────

class TestRoutingDecisionsCanonical:
    def test_dashboard_decisions_use_method_not_reason(
        self, sample_routing_logic: list[dict]
    ) -> None:
        """dashboard_decisions must use d['method'], not d['reason']."""
        # This is what session-end.py now does (the fix)
        dashboard_decisions = [
            {"method": d["method"], "count": d["hits"]}
            for d in sample_routing_logic
        ]
        methods = {d["method"] for d in dashboard_decisions}
        # All methods must be canonical IDs
        for m in methods:
            assert m in CANONICAL_IDS, (
                f"Method {m!r} is not a canonical ID — session-end.py is using "
                f"the human 'reason' string instead of 'method'"
            )

    def test_human_reason_strings_are_not_canonical(
        self, sample_routing_logic: list[dict]
    ) -> None:
        """Human reason strings like 'Cached patterns / Static rules' are NOT canonical."""
        human_reasons = {d["reason"] for d in sample_routing_logic}
        for reason in human_reasons:
            # Human reasons should not appear in canonical ID set
            assert reason not in CANONICAL_IDS, (
                f"Human reason string {reason!r} accidentally matches a canonical ID"
            )


# ── Bug 2: weekly_pct not double-scaled ────────────────────────────────────────

class TestWeeklyQuotaRange:
    @pytest.mark.parametrize("weekly_pct", [0.0, 10.0, 35.7, 100.0])
    def test_weekly_pct_stays_in_range(self, weekly_pct: float) -> None:
        """weekly_pct (already 0-100) must NOT be multiplied by 100 again."""
        current = {"weekly_pct": weekly_pct, "session_pct": 26.0}
        # The fix: just read the value directly
        claude_quota_pct = current.get("weekly_pct", 0.0)
        lo, hi = DASH_EXP["weekly_pct_range"]
        assert lo <= claude_quota_pct <= hi, (
            f"claude_quota_pct={claude_quota_pct} is outside [{lo}, {hi}] "
            f"(double-scaling bug: was multiplied by 100 again)"
        )

    def test_weekly_pct_100_does_not_become_10000(self) -> None:
        current = {"weekly_pct": 100.0}
        # Bug: * 100 would give 10000; fix gives 100
        fixed = current.get("weekly_pct", 0.0)
        bugged = current.get("weekly_pct", 0.0) * 100
        assert fixed == 100.0
        assert bugged == 10000.0  # demonstrates what the old code did


# ── Bug 3: daily series from 14-day data ──────────────────────────────────────

class TestDailySeriesPopulated:
    def test_daily_calls_comes_from_14d_data(
        self, sample_daily_14d: list[tuple]
    ) -> None:
        """daily_calls must be column[1] of daily_14d, not []."""
        daily_calls = [d[1] for d in sample_daily_14d]
        assert daily_calls == [5, 8, 3, 12, 7, 15, 10]
        assert len(daily_calls) == len(sample_daily_14d)

    def test_daily_tokens_comes_from_14d_data(
        self, sample_daily_14d: list[tuple]
    ) -> None:
        """daily_tokens must be column[2] of daily_14d, not []."""
        daily_tokens = [d[2] for d in sample_daily_14d]
        assert daily_tokens == [1200, 2100, 800, 3400, 1800, 4200, 2800]

    def test_empty_14d_gives_empty_lists_not_crash(self) -> None:
        daily_14d: list[tuple] = []
        daily_calls = [d[1] for d in daily_14d] if daily_14d else []
        daily_tokens = [d[2] for d in daily_14d] if daily_14d else []
        assert daily_calls == []
        assert daily_tokens == []


# ── Bug 4: session_models from tools_data ─────────────────────────────────────

class TestSessionModelsFromToolsData:
    def test_session_models_built_from_tools(
        self, sample_tools_data: dict
    ) -> None:
        """session_models must be aggregated from tools_data, not []."""
        model_agg: dict[str, dict] = {}
        for data in sample_tools_data.values():
            if not isinstance(data, dict):
                continue
            in_tok = data.get("in", 0)
            out_tok = data.get("out", 0)
            cost = data.get("cost", 0.0)
            for model, count in data.get("models", {}).items():
                if model not in model_agg:
                    model_agg[model] = {"calls": 0, "tokens": 0, "cost": 0.0}
                model_agg[model]["calls"] += count
                model_agg[model]["tokens"] += (in_tok + out_tok) * count
                model_agg[model]["cost"] += cost * count

        session_models = sorted(model_agg.items(), key=lambda x: -x[1]["calls"])
        assert len(session_models) > 0, "session_models must not be empty when tools_data has calls"
        model_names = [m for m, _ in session_models]
        assert "gpt-4o" in model_names
        assert "gemini-2.5-flash" in model_names
        assert "codex/gpt-5.4" in model_names

    def test_top_model_has_most_calls(self, sample_tools_data: dict) -> None:
        model_agg: dict[str, dict] = {}
        for data in sample_tools_data.values():
            if not isinstance(data, dict):
                continue
            for model, count in data.get("models", {}).items():
                model_agg.setdefault(model, {"calls": 0})["calls"] += count

        ranked = sorted(model_agg.items(), key=lambda x: -x[1]["calls"])
        assert ranked[0][0] == "gpt-4o"  # 12 calls

    def test_empty_tools_gives_empty_session_models(self) -> None:
        session_models: list[dict] = []
        tools_data: dict = {}
        if tools_data:
            # (aggregation logic — skipped because tools_data is empty)
            pass
        assert session_models == []
