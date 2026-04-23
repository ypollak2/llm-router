"""SAFEGUARD #5: Static analysis tests ensuring policy invariants are maintained.

These tests verify that Claude Opus never appears in BALANCED or BUDGET
profiles, and that reorder_for_pressure() respects profile constraints.

This is the primary safeguard preventing the regression of the April 20-22
Claude Opus policy violation.
"""

import pytest
from llm_router.profiles import (
    ROUTING_TABLE,
    RoutingProfile,
    TaskType,
    reorder_for_pressure,
    get_model_chain,
    _validate_chain_invariants,
    MODELS_PER_PROFILE,
)


class TestOpusNotInBudgetProfile:
    """SAFEGUARD: Opus must never appear in BUDGET profile chains."""

    def test_opus_not_in_static_budget_chains(self):
        """Verify static ROUTING_TABLE has no Opus in BUDGET chains."""
        for (profile, task_type), chain in ROUTING_TABLE.items():
            if profile != RoutingProfile.BUDGET:
                continue

            assert "anthropic/claude-opus-4-6" not in chain, (
                f"POLICY VIOLATION: Opus found in BUDGET/{task_type.name} static chain. "
                f"Chain: {chain}"
            )

    def test_opus_not_in_dynamic_budget_chains(self):
        """Verify dynamic reordering doesn't introduce Opus into BUDGET chains."""
        for task_type in TaskType:
            if task_type in {TaskType.IMAGE, TaskType.VIDEO, TaskType.AUDIO}:
                continue  # Media tasks don't use pressure reordering

            chain = get_model_chain(RoutingProfile.BUDGET, task_type)
            assert "anthropic/claude-opus-4-6" not in chain, (
                f"POLICY VIOLATION: Opus appeared in BUDGET/{task_type.name} "
                f"after dynamic reordering. Chain: {chain}"
            )


class TestOpusNotInBalancedProfile:
    """SAFEGUARD: Opus must never appear in BALANCED profile chains."""

    def test_opus_not_in_static_balanced_chains(self):
        """Verify static ROUTING_TABLE has no Opus in BALANCED chains."""
        for (profile, task_type), chain in ROUTING_TABLE.items():
            if profile != RoutingProfile.BALANCED:
                continue

            assert "anthropic/claude-opus-4-6" not in chain, (
                f"POLICY VIOLATION: Opus found in BALANCED/{task_type.name} static chain. "
                f"Chain: {chain}"
            )

    def test_opus_not_in_dynamic_balanced_chains_low_pressure(self):
        """Verify Opus doesn't appear in BALANCED chains at low pressure."""
        for task_type in TaskType:
            if task_type in {TaskType.IMAGE, TaskType.VIDEO, TaskType.AUDIO}:
                continue

            chain = get_model_chain(RoutingProfile.BALANCED, task_type)
            assert "anthropic/claude-opus-4-6" not in chain, (
                f"POLICY VIOLATION: Opus appeared in BALANCED/{task_type.name} "
                f"at low pressure. Chain: {chain}"
            )

    def test_reorder_for_pressure_removes_opus_at_high_pressure(self):
        """At ≥99% pressure, reorder_for_pressure should remove ALL Claude (including Opus)."""
        # If someone manually tries to include Opus in a BALANCED chain and then
        # applies pressure reordering at 99%+, the hard cap should remove it.
        test_chain = [
            "ollama/qwen3.5:latest",
            "anthropic/claude-opus-4-6",  # hypothetically included (shouldn't be)
            "gemini/gemini-2.5-pro",
        ]

        result = reorder_for_pressure(test_chain, pressure=0.99, profile=RoutingProfile.BALANCED)

        assert "anthropic/claude-opus-4-6" not in result, (
            f"POLICY VIOLATION: Opus was not removed at 99% pressure. "
            f"Result: {result}"
        )


class TestOpusAllowedInPremiumProfile:
    """SAFEGUARD: Opus IS allowed in PREMIUM — verify it's not removed."""

    def test_opus_in_static_premium_chains(self):
        """Verify Opus is present in at least some PREMIUM chains."""
        opus_found_anywhere = False
        for (profile, task_type), chain in ROUTING_TABLE.items():
            if profile != RoutingProfile.PREMIUM:
                continue
            if "anthropic/claude-opus-4-6" in chain:
                opus_found_anywhere = True
                break

        assert opus_found_anywhere, (
            "Opus should appear in some PREMIUM profile chains for quality."
        )

    def test_opus_not_removed_in_premium_at_low_pressure(self):
        """At low pressure, Opus should remain in PREMIUM chains."""
        for task_type in TaskType:
            if task_type in {TaskType.IMAGE, TaskType.VIDEO, TaskType.AUDIO}:
                continue

            chain = get_model_chain(RoutingProfile.PREMIUM, task_type)
            # Opus should be present (though may not be first depending on benchmarks)
            assert "anthropic/claude-opus-4-6" in chain, (
                f"Opus should be in PREMIUM/{task_type.name} at low pressure. "
                f"Chain: {chain}"
            )


class TestInvariantValidation:
    """Test the _validate_chain_invariants() function directly."""

    def test_valid_budget_chain_passes(self):
        """Valid BUDGET chain should pass validation."""
        chain = [
            "ollama/qwen3.5:latest",
            "codex/gpt-4o-mini",
            "anthropic/claude-haiku-4-5-20251001",
        ]
        # Should not raise
        _validate_chain_invariants(chain, RoutingProfile.BUDGET, "test")

    def test_opus_in_budget_fails(self):
        """Opus in BUDGET chain should fail validation."""
        chain = [
            "ollama/qwen3.5:latest",
            "anthropic/claude-opus-4-6",
            "anthropic/claude-haiku-4-5-20251001",
        ]
        with pytest.raises(AssertionError, match="POLICY VIOLATION"):
            _validate_chain_invariants(chain, RoutingProfile.BUDGET, "test")

    def test_opus_in_balanced_fails(self):
        """Opus in BALANCED chain should fail validation."""
        chain = [
            "ollama/qwen3.5:latest",
            "anthropic/claude-opus-4-6",
            "anthropic/claude-sonnet-4-6",
        ]
        with pytest.raises(AssertionError, match="POLICY VIOLATION"):
            _validate_chain_invariants(chain, RoutingProfile.BALANCED, "test")

    def test_valid_premium_chain_passes(self):
        """Valid PREMIUM chain with Opus should pass."""
        chain = [
            "anthropic/claude-opus-4-6",
            "anthropic/claude-sonnet-4-6",
            "openai/gpt-4o",
        ]
        # Should not raise
        _validate_chain_invariants(chain, RoutingProfile.PREMIUM, "test")

    def test_sonnet_first_in_budget_logs_warning(self, capsys):
        """Sonnet first (not last) in BUDGET should log warning (discouraged)."""
        chain = [
            "anthropic/claude-sonnet-4-6",  # Should be last, not first
            "ollama/qwen3.5:latest",
        ]
        # Should not raise (discouraged, not forbidden)
        _validate_chain_invariants(chain, RoutingProfile.BUDGET, "test")
        # Should have logged a warning to stdout (structlog uses structured logging)
        captured = capsys.readouterr()
        assert "POLICY MISMATCH" in captured.out or "POLICY MISMATCH" in captured.err


class TestConstraintStructures:
    """Verify constraint structures are properly defined."""

    def test_models_per_profile_has_all_profiles(self):
        """All routing profiles should have constraints defined."""
        assert RoutingProfile.BUDGET in MODELS_PER_PROFILE
        assert RoutingProfile.BALANCED in MODELS_PER_PROFILE
        assert RoutingProfile.PREMIUM in MODELS_PER_PROFILE

    def test_forbidden_models_list_includes_opus(self):
        """Opus should be in forbidden list for BUDGET and BALANCED."""
        assert "anthropic/claude-opus-4-6" in MODELS_PER_PROFILE[
            RoutingProfile.BUDGET
        ]["forbidden"]
        assert "anthropic/claude-opus-4-6" in MODELS_PER_PROFILE[
            RoutingProfile.BALANCED
        ]["forbidden"]

    def test_opus_not_in_premium_forbidden(self):
        """Opus should NOT be in forbidden list for PREMIUM."""
        assert "anthropic/claude-opus-4-6" not in MODELS_PER_PROFILE[
            RoutingProfile.PREMIUM
        ]["forbidden"]


class TestReorderForPressureConstraints:
    """Test that reorder_for_pressure respects constraints at each pressure level."""

    def test_low_pressure_respects_constraints(self):
        """At low pressure (<0.85), chains should still respect constraints."""
        test_chain = [
            "ollama/qwen3.5:latest",
            "anthropic/claude-sonnet-4-6",
        ]
        # Reordering at low pressure moves Claude to front, but it should be safe
        result = reorder_for_pressure(
            test_chain, pressure=0.5, profile=RoutingProfile.BALANCED
        )
        assert "anthropic/claude-opus-4-6" not in result

    def test_medium_pressure_respects_constraints(self):
        """At medium pressure (0.85-0.98), chains should respect constraints."""
        test_chain = [
            "ollama/qwen3.5:latest",
            "anthropic/claude-sonnet-4-6",
        ]
        result = reorder_for_pressure(
            test_chain, pressure=0.9, profile=RoutingProfile.BALANCED
        )
        assert "anthropic/claude-opus-4-6" not in result

    def test_hard_cap_removes_all_claude(self):
        """At ≥0.99 pressure (hard cap), ALL Claude should be removed."""
        test_chain = [
            "ollama/qwen3.5:latest",
            "anthropic/claude-haiku-4-5-20251001",
            "anthropic/claude-sonnet-4-6",
            "gemini/gemini-2.5-pro",
        ]
        result = reorder_for_pressure(
            test_chain, pressure=0.99, profile=RoutingProfile.BALANCED
        )
        # Hard cap should remove ALL anthropic models
        assert "anthropic/claude-opus-4-6" not in result
        assert "anthropic/claude-haiku-4-5-20251001" not in result
        assert "anthropic/claude-sonnet-4-6" not in result


class TestIntegrationWithRouter:
    """Integration tests to ensure invariants hold through the full routing chain."""

    @pytest.mark.parametrize(
        "profile,task_type",
        [
            (RoutingProfile.BUDGET, TaskType.QUERY),
            (RoutingProfile.BUDGET, TaskType.CODE),
            (RoutingProfile.BUDGET, TaskType.ANALYZE),
            (RoutingProfile.BALANCED, TaskType.QUERY),
            (RoutingProfile.BALANCED, TaskType.CODE),
            (RoutingProfile.BALANCED, TaskType.ANALYZE),
        ],
    )
    def test_no_opus_in_non_premium_after_full_routing(self, profile, task_type):
        """Full routing through get_model_chain should never produce Opus in non-PREMIUM."""
        chain = get_model_chain(profile, task_type)
        assert "anthropic/claude-opus-4-6" not in chain, (
            f"POLICY VIOLATION: Opus appeared in {profile.name}/{task_type.name} "
            f"after full routing. Chain: {chain}"
        )
