"""Tests for the REASONING routing profile and deep_reasoning complexity tier.

This module validates:
1. RoutingProfile.REASONING exists and is distinct from PREMIUM
2. Complexity.DEEP_REASONING maps to RoutingProfile.REASONING (not PREMIUM)
3. The REASONING chain in standard.yaml contains the right model order
4. use_thinking flag is set for deep_reasoning complexity
5. Gemini 2.5 Pro receives thinkingConfig (not just Anthropic)
6. MODELS_PER_PROFILE constraints for REASONING profile
7. get_model_chain returns R1/o3 for the REASONING profile
8. _validate_chain_invariants passes for REASONING chains
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from llm_router.profiles import (
    model_matches,
    COMPLEXITY_TO_PROFILE,
    MODELS_PER_PROFILE,
    ROUTING_TABLE,
    _validate_chain_invariants,
    get_model_chain,
)
from llm_router.types import Complexity, RoutingProfile, TaskType


class TestReasoningProfileExists:
    def test_reasoning_is_a_valid_profile(self) -> None:
        assert RoutingProfile.REASONING == "reasoning"

    def test_reasoning_is_distinct_from_premium(self) -> None:
        assert RoutingProfile.REASONING != RoutingProfile.PREMIUM

    def test_reasoning_is_distinct_from_balanced(self) -> None:
        assert RoutingProfile.REASONING != RoutingProfile.BALANCED

    def test_all_profiles_have_values(self) -> None:
        profiles = {p.value for p in RoutingProfile}
        assert "reasoning" in profiles


class TestDeepReasoningComplexityMapping:
    def test_deep_reasoning_maps_to_reasoning_profile(self) -> None:
        assert COMPLEXITY_TO_PROFILE[Complexity.DEEP_REASONING] == RoutingProfile.REASONING

    def test_deep_reasoning_no_longer_maps_to_premium(self) -> None:
        assert COMPLEXITY_TO_PROFILE[Complexity.DEEP_REASONING] != RoutingProfile.PREMIUM

    def test_complex_still_maps_to_premium(self) -> None:
        """Regression: COMPLEX must remain on PREMIUM, not spill into REASONING."""
        assert COMPLEXITY_TO_PROFILE[Complexity.COMPLEX] == RoutingProfile.PREMIUM

    def test_simple_still_maps_to_budget(self) -> None:
        assert COMPLEXITY_TO_PROFILE[Complexity.SIMPLE] == RoutingProfile.BUDGET

    def test_moderate_still_maps_to_balanced(self) -> None:
        assert COMPLEXITY_TO_PROFILE[Complexity.MODERATE] == RoutingProfile.BALANCED


class TestReasoningChainContent:
    """The REASONING chain must lead with cheap native reasoners, not general frontier."""

    @pytest.mark.parametrize("task_type", [
        TaskType.QUERY,
        TaskType.ANALYZE,
        TaskType.CODE,
        TaskType.RESEARCH,
        TaskType.GENERATE,
    ])
    def test_reasoning_chain_exists_for_text_tasks(self, task_type: TaskType) -> None:
        chain = ROUTING_TABLE.get((RoutingProfile.REASONING, task_type))
        assert chain is not None, f"No REASONING chain for {task_type.value}"
        assert len(chain) >= 2, "Chain must have at least 2 models for fallback"

    def test_deepseek_reasoner_in_query_chain(self) -> None:
        chain = ROUTING_TABLE[(RoutingProfile.REASONING, TaskType.QUERY)]
        assert "deepseek/deepseek-reasoner" in chain, (
            "R1 must be in REASONING/query — it's the cheapest capable reasoner"
        )

    def test_deepseek_reasoner_in_analyze_chain(self) -> None:
        chain = ROUTING_TABLE[(RoutingProfile.REASONING, TaskType.ANALYZE)]
        assert "deepseek/deepseek-reasoner" in chain

    def test_o3_in_reasoning_chains(self) -> None:
        for task_type in (TaskType.QUERY, TaskType.ANALYZE, TaskType.CODE):
            chain = ROUTING_TABLE[(RoutingProfile.REASONING, task_type)]
            assert "openai/o3" in chain, f"o3 must be in REASONING/{task_type.value}"

    def test_gemini_25_pro_not_in_reasoning_chains_while_disabled(self) -> None:
        # Gemini API was intentionally disabled (commit 21aff97 "Disable Gemini
        # API"); 2.5 Pro was removed from every chain. Guard that it stays out so
        # a stale re-add can't silently route to an unconfigured provider.
        for task_type in (TaskType.QUERY, TaskType.ANALYZE):
            chain = ROUTING_TABLE[(RoutingProfile.REASONING, task_type)]
            assert "gemini/gemini-2.5-pro" not in chain, (
                f"Gemini 2.5 Pro is disabled and must NOT be in REASONING/{task_type.value}"
            )

    def test_claude_opus_in_reasoning_chains(self) -> None:
        for task_type in (TaskType.QUERY, TaskType.ANALYZE, TaskType.CODE):
            chain = ROUTING_TABLE[(RoutingProfile.REASONING, task_type)]
            assert any(model_matches(m, "anthropic/claude-opus") for m in chain), (
                f"Claude Opus (use_thinking) must be in REASONING/{task_type.value}"
            )

    def test_deepseek_reasoner_before_o3_in_query_chain(self) -> None:
        """R1 must lead o3 — it costs 28x less and should be tried first."""
        chain = ROUTING_TABLE[(RoutingProfile.REASONING, TaskType.QUERY)]
        r1_idx = chain.index("deepseek/deepseek-reasoner")
        o3_idx = chain.index("openai/o3")
        assert r1_idx < o3_idx, "DeepSeek-R1 must appear before o3 in REASONING chain"

    def test_haiku_not_in_reasoning_chains(self) -> None:
        """Haiku doesn't support extended thinking — must not be in REASONING chains."""
        for task_type in (TaskType.QUERY, TaskType.ANALYZE, TaskType.CODE):
            chain = ROUTING_TABLE[(RoutingProfile.REASONING, task_type)]
            assert not any(model_matches(m, "anthropic/claude-haiku") for m in chain), (
                f"Haiku (no extended thinking) must NOT be in REASONING/{task_type.value}"
            )

    def test_get_model_chain_returns_reasoning_chain(self) -> None:
        with patch("llm_router.benchmarks.apply_benchmark_ordering", side_effect=lambda c, *_, **__: c):
            with patch("llm_router.claude_usage.get_claude_pressure", return_value=0.0):
                chain = get_model_chain(RoutingProfile.REASONING, TaskType.ANALYZE)
        assert "deepseek/deepseek-reasoner" in chain


class TestReasoningModelConstraints:
    def test_reasoning_profile_has_constraints_entry(self) -> None:
        assert RoutingProfile.REASONING in MODELS_PER_PROFILE

    def test_reasoning_forbids_nothing_at_top_level(self) -> None:
        constraints = MODELS_PER_PROFILE[RoutingProfile.REASONING]
        # REASONING can use any model — no hard forbidden list (unlike BUDGET)
        assert constraints["forbidden"] == []

    def test_reasoning_discourages_haiku(self) -> None:
        # constraints are version-agnostic FAMILY prefixes (see model_matches)
        constraints = MODELS_PER_PROFILE[RoutingProfile.REASONING]
        assert "anthropic/claude-haiku" in constraints["discouraged"]

    def test_reasoning_allows_opus(self) -> None:
        constraints = MODELS_PER_PROFILE[RoutingProfile.REASONING]
        assert "anthropic/claude-opus" in constraints["allowed_claude"]


class TestValidateChainInvariantsForReasoning:
    def test_validate_passes_for_r1_o3_opus_chain(self) -> None:
        chain = [
            "deepseek/deepseek-reasoner",
            "openai/o3",
            "anthropic/claude-opus-4-6",
        ]
        # Must not raise
        _validate_chain_invariants(chain, RoutingProfile.REASONING, "test")

    def test_validate_passes_for_full_reasoning_chain(self) -> None:
        chain = ROUTING_TABLE[(RoutingProfile.REASONING, TaskType.ANALYZE)]
        _validate_chain_invariants(chain, RoutingProfile.REASONING, "test_full")

    def test_budget_still_forbids_opus(self) -> None:
        """Regression: adding REASONING must not relax BUDGET constraints."""
        chain = ["anthropic/claude-opus-4-6", "openai/gpt-4o-mini"]
        with pytest.raises(AssertionError, match="POLICY VIOLATION"):
            _validate_chain_invariants(chain, RoutingProfile.BUDGET, "test_regression")


class TestUseThinkingFlag:
    """The router must set use_thinking=True for DEEP_REASONING complexity."""

    def test_resolve_profile_sets_use_thinking_for_deep_reasoning(self) -> None:
        from unittest.mock import MagicMock
        from llm_router.router import _resolve_profile

        mock_config = MagicMock()
        mock_config.llm_router_profile = RoutingProfile.BALANCED

        profile, complexity, use_thinking = _resolve_profile(
            profile=None,
            complexity_hint="deep_reasoning",
            classification_data=None,
            prompt="prove the riemann hypothesis",
            model_override=None,
            config=mock_config,
        )

        assert use_thinking is True, "use_thinking must be True for deep_reasoning"
        assert profile == RoutingProfile.REASONING
        assert complexity == Complexity.DEEP_REASONING

    def test_resolve_profile_does_not_set_use_thinking_for_complex(self) -> None:
        from unittest.mock import MagicMock
        from llm_router.router import _resolve_profile

        mock_config = MagicMock()
        mock_config.llm_router_profile = RoutingProfile.BALANCED

        profile, complexity, use_thinking = _resolve_profile(
            profile=None,
            complexity_hint="complex",
            classification_data=None,
            prompt="architect a distributed system",
            model_override=None,
            config=mock_config,
        )

        assert use_thinking is False, "use_thinking must be False for complex (not deep_reasoning)"
        assert profile == RoutingProfile.PREMIUM
        assert complexity == Complexity.COMPLEX
