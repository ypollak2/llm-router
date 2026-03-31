"""Tests for routing profiles."""

from unittest.mock import patch

from llm_router.profiles import get_model_chain, provider_from_model, ROUTING_TABLE
from llm_router.types import RoutingProfile, TaskType


class TestGetModelChain:
    def test_budget_research_prefers_perplexity(self):
        # Disable benchmark reordering so we test the static routing table.
        with patch("llm_router.benchmarks.get_benchmark_data", return_value=None):
            chain = get_model_chain(RoutingProfile.BUDGET, TaskType.RESEARCH)
        assert chain[0].startswith("perplexity/")

    def test_premium_code_prefers_o3(self):
        with patch("llm_router.benchmarks.get_benchmark_data", return_value=None):
            chain = get_model_chain(RoutingProfile.PREMIUM, TaskType.CODE)
        assert "openai/o3" in chain

    def test_all_profile_task_combos_exist(self):
        for profile in RoutingProfile:
            for task in TaskType:
                chain = get_model_chain(profile, task)
                assert len(chain) >= 1, f"No models for {profile}/{task}"

    def test_fallback_for_unknown_combo(self):
        # Shouldn't happen, but get_model_chain has a fallback
        chain = get_model_chain(RoutingProfile.BUDGET, TaskType.QUERY)
        assert isinstance(chain, list)

    def test_each_chain_has_fallback(self):
        for key, chain in ROUTING_TABLE.items():
            assert len(chain) >= 2, f"{key} has no fallback model"


class TestResearchPressureTail:
    """RESEARCH chains must keep Perplexity first but demote Claude in the tail."""

    def test_research_perplexity_stays_first_at_high_pressure(self):
        """At ≥ 85% pressure Perplexity must remain at position 0."""
        with (
            patch("llm_router.benchmarks.get_benchmark_data", return_value=None),
            patch("llm_router.claude_usage.get_claude_pressure", return_value=0.90),
        ):
            chain = get_model_chain(RoutingProfile.BALANCED, TaskType.RESEARCH)
        assert chain[0].startswith("perplexity/"), f"Expected perplexity first, got: {chain}"

    def test_research_claude_demoted_from_tail_at_high_pressure(self):
        """At ≥ 85% pressure Claude models in RESEARCH tail move to the end."""
        with (
            patch("llm_router.benchmarks.get_benchmark_data", return_value=None),
            patch("llm_router.claude_usage.get_claude_pressure", return_value=0.90),
        ):
            chain = get_model_chain(RoutingProfile.BALANCED, TaskType.RESEARCH)

        non_perp = [m for m in chain if "perplexity" not in m]
        claude_positions = [i for i, m in enumerate(non_perp) if "anthropic" in m]
        non_claude_positions = [i for i, m in enumerate(non_perp) if "anthropic" not in m]

        if claude_positions and non_claude_positions:
            # All Claude models must come after all non-Claude models in the tail
            assert max(non_claude_positions) < min(claude_positions), (
                f"Claude not demoted at high pressure — tail order: {non_perp}"
            )

    def test_research_claude_leads_tail_at_low_pressure(self):
        """At < 85% pressure Claude is effectively free and leads the RESEARCH tail."""
        with (
            patch("llm_router.benchmarks.get_benchmark_data", return_value=None),
            patch("llm_router.claude_usage.get_claude_pressure", return_value=0.30),
        ):
            chain = get_model_chain(RoutingProfile.BALANCED, TaskType.RESEARCH)

        non_perp = [m for m in chain if "perplexity" not in m]
        if any("anthropic" in m for m in non_perp):
            assert non_perp[0].startswith("anthropic/"), (
                f"Expected Claude first in tail at low pressure, got: {non_perp}"
            )


class TestBudgetHardCap:
    """BUDGET profile must honour the ≥ 99% Claude hard cap."""

    def test_budget_pressure_99_removes_claude(self):
        """At ≥ 99% pressure Claude must be excluded from BUDGET chains."""
        with (
            patch("llm_router.benchmarks.get_benchmark_data", return_value=None),
            patch("llm_router.claude_usage.get_claude_pressure", return_value=0.99),
        ):
            chain = get_model_chain(RoutingProfile.BUDGET, TaskType.CODE)

        assert not any("anthropic" in m for m in chain), (
            f"Claude still in chain at 99% pressure: {chain}"
        )


class TestPrefetchedPenalties:
    """apply_benchmark_ordering must use pre-fetched dicts instead of DB calls."""

    def test_failure_rates_dict_used_directly(self):
        """When failure_rates dict is provided, penalty uses it without DB access."""
        from llm_router.benchmarks import apply_benchmark_ordering

        # Mock benchmark data with two scored models
        mock_data = {
            "tiers": {"code": {"balanced": ["gemini/gemini-2.5-pro", "openai/gpt-4o"]}},
            "task_scores": {"code": {
                "gemini/gemini-2.5-pro": 0.95,
                "openai/gpt-4o": 0.90,
            }},
        }

        with patch("llm_router.benchmarks.get_benchmark_data", return_value=mock_data):
            # gpt-4o has 80% failure rate — should drop below gemini even though scores differ
            result = apply_benchmark_ordering(
                ["gemini/gemini-2.5-pro", "openai/gpt-4o"],
                TaskType.CODE,
                RoutingProfile.BALANCED,
                failure_rates={"openai/gpt-4o": 0.80},
                latency_stats={},
            )

        assert result[0] == "gemini/gemini-2.5-pro", (
            f"Expected gemini first after gpt-4o penalty, got: {result}"
        )

    def test_latency_stats_dict_used_directly(self):
        """When latency_stats dict is provided, penalty uses it without DB access."""
        from llm_router.benchmarks import apply_benchmark_ordering

        mock_data = {
            "tiers": {"code": {"balanced": ["openai/gpt-4o", "gemini/gemini-2.5-flash"]}},
            "task_scores": {"code": {
                "openai/gpt-4o": 0.91,
                "gemini/gemini-2.5-flash": 0.90,
            }},
        }

        with patch("llm_router.benchmarks.get_benchmark_data", return_value=mock_data):
            # gpt-4o has 200s P95 latency → 0.50 penalty → drops below gemini-flash
            result = apply_benchmark_ordering(
                ["openai/gpt-4o", "gemini/gemini-2.5-flash"],
                TaskType.CODE,
                RoutingProfile.BALANCED,
                failure_rates={},
                latency_stats={"openai/gpt-4o": {"p50": 100000.0, "p95": 200000.0, "count": 10}},
            )

        assert result[0] == "gemini/gemini-2.5-flash", (
            f"Expected gemini-flash first after gpt-4o latency penalty, got: {result}"
        )


class TestProviderFromModel:
    def test_extracts_provider(self):
        assert provider_from_model("openai/gpt-4o") == "openai"
        assert provider_from_model("gemini/gemini-2.5-flash") == "gemini"
        assert provider_from_model("perplexity/sonar") == "perplexity"

    def test_unknown_format(self):
        assert provider_from_model("bare-model") == "unknown"
