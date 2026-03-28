"""Tests for routing profiles."""

from llm_router.profiles import get_model_chain, provider_from_model, ROUTING_TABLE
from llm_router.types import RoutingProfile, TaskType


class TestGetModelChain:
    def test_budget_research_prefers_perplexity(self):
        chain = get_model_chain(RoutingProfile.BUDGET, TaskType.RESEARCH)
        assert chain[0].startswith("perplexity/")

    def test_premium_code_prefers_o3(self):
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


class TestProviderFromModel:
    def test_extracts_provider(self):
        assert provider_from_model("openai/gpt-4o") == "openai"
        assert provider_from_model("gemini/gemini-2.0-flash") == "gemini"
        assert provider_from_model("perplexity/sonar") == "perplexity"

    def test_unknown_format(self):
        assert provider_from_model("bare-model") == "unknown"
