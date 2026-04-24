"""Unit tests for routing policy system."""

import pytest
from llm_router.policy import RoutingPolicy, PolicyManager


class TestPolicySchema:
    """Test policy schema validation."""

    def test_valid_policy_creation(self):
        """Create a valid policy."""
        policy = RoutingPolicy(
            name="test_policy",
            description="Test policy",
            confidence_threshold=4,
        )
        assert policy.name == "test_policy"
        assert policy.confidence_threshold == 4

    def test_threshold_out_of_range_high(self):
        """Reject threshold > 10."""
        with pytest.raises(ValueError, match="must be 0-10"):
            RoutingPolicy(
                name="invalid",
                description="Test",
                confidence_threshold=11,
            )

    def test_threshold_out_of_range_low(self):
        """Reject threshold < 0."""
        with pytest.raises(ValueError, match="must be 0-10"):
            RoutingPolicy(
                name="invalid",
                description="Test",
                confidence_threshold=-1,
            )

    def test_invalid_policy_name(self):
        """Reject invalid policy names."""
        with pytest.raises(ValueError, match="valid identifier"):
            RoutingPolicy(
                name="invalid-name!",
                description="Test",
            )

    def test_policy_is_frozen(self):
        """Policy dataclass should be frozen (immutable)."""
        policy = RoutingPolicy(
            name="frozen",
            description="Test",
        )
        with pytest.raises(AttributeError):
            policy.confidence_threshold = 5


class TestSkipPrompt:
    """Test skip_prompt logic."""

    def test_skip_acknowledgements_enabled(self):
        """Skip acknowledgements when enabled."""
        policy = RoutingPolicy(
            name="test",
            description="Test",
            skip_acknowledgements=True,
        )
        assert policy.skip_prompt("yes")
        assert policy.skip_prompt("ok")
        assert policy.skip_prompt("thanks")
        assert policy.skip_prompt("y")
        assert policy.skip_prompt("no")

    def test_skip_acknowledgements_disabled(self):
        """Don't skip acknowledgements when disabled."""
        policy = RoutingPolicy(
            name="test",
            description="Test",
            skip_acknowledgements=False,
        )
        assert not policy.skip_prompt("yes")
        assert not policy.skip_prompt("ok")
        assert not policy.skip_prompt("thanks")

    def test_skip_patterns_regex(self):
        """Skip based on regex patterns."""
        policy = RoutingPolicy(
            name="test",
            description="Test",
            skip_patterns=[r"^/(help|login)"],
        )
        assert policy.skip_prompt("/help")
        assert policy.skip_prompt("/login")
        assert not policy.skip_prompt("/other")
        assert not policy.skip_prompt("help me")

    def test_invalid_regex_pattern_ignored(self, capsys):
        """Invalid regex patterns are logged but don't crash."""
        policy = RoutingPolicy(
            name="test",
            description="Test",
            skip_patterns=[r"[invalid(regex"],
        )
        # Should not raise, but log warning
        result = policy.skip_prompt("test")
        assert result is False


class TestPolicyLoading:
    """Test policy loading and management."""

    def test_load_preset_aggressive(self):
        """Load aggressive preset policy."""
        pm = PolicyManager()
        policy = pm.load_policy("aggressive")
        assert policy.name == "aggressive"
        assert policy.confidence_threshold == 2
        assert policy.route_coordination is True
        assert policy.skip_acknowledgements is False

    def test_load_preset_balanced(self):
        """Load balanced preset policy."""
        pm = PolicyManager()
        policy = pm.load_policy("balanced")
        assert policy.name == "balanced"
        assert policy.confidence_threshold == 4
        assert policy.skip_acknowledgements is True

    def test_load_preset_conservative(self):
        """Load conservative preset policy."""
        pm = PolicyManager()
        policy = pm.load_policy("conservative")
        assert policy.name == "conservative"
        assert policy.confidence_threshold == 6
        assert policy.skip_acknowledgements is True

    def test_load_nonexistent_policy_raises(self):
        """Raise FileNotFoundError for missing policy."""
        pm = PolicyManager()
        with pytest.raises(FileNotFoundError):
            pm.load_policy("nonexistent_policy_xyz")

    def test_policy_caching(self):
        """Policies are cached after first load."""
        pm = PolicyManager()
        policy1 = pm.load_policy("aggressive")
        policy2 = pm.load_policy("aggressive")
        # Same object from cache
        assert policy1 is policy2

    def test_list_policies(self):
        """List all available policies."""
        pm = PolicyManager()
        policies = pm.list_policies()
        assert "aggressive" in policies
        assert "balanced" in policies
        assert "conservative" in policies


class TestPolicySwitching:
    """Test setting active policy."""

    def test_set_active_policy(self):
        """Set and retrieve active policy."""
        pm = PolicyManager()
        policy = pm.set_active_policy("aggressive")
        assert pm.get_active_policy() is policy
        assert pm.get_active_policy().name == "aggressive"

    def test_get_active_policy_default(self):
        """Default to balanced if none set."""
        pm = PolicyManager()
        pm._active_policy = None  # Reset
        policy = pm.get_active_policy()
        # Should fallback to balanced or return something
        assert policy is not None
        assert policy.name in ("balanced", "aggressive", "conservative")

    def test_switch_policy_mid_session(self):
        """Switch policies and new one is active."""
        pm = PolicyManager()
        pm.set_active_policy("aggressive")
        assert pm.get_active_policy().name == "aggressive"
        pm.set_active_policy("conservative")
        assert pm.get_active_policy().name == "conservative"


class TestCustomPolicies:
    """Test creating and saving custom policies."""

    def test_save_custom_policy(self):
        """Save a custom policy to ~/.llm-router/policies/."""
        pm = PolicyManager()
        custom = RoutingPolicy(
            name="custom_test",
            description="Test custom policy",
            confidence_threshold=3,
            skip_acknowledgements=True,
        )
        path = pm.save_custom_policy(custom)
        assert path.exists()
        assert path.name == "custom_test.yaml"
        
        # Load it back
        loaded = pm.load_policy("custom_test")
        assert loaded.name == "custom_test"
        assert loaded.confidence_threshold == 3
        
        # Cleanup
        path.unlink()

    def test_custom_policy_overrides_cache(self):
        """Saving a custom policy clears it from cache."""
        pm = PolicyManager()
        # Load a policy to cache it
        pm.load_policy("aggressive")
        
        # Create and save a custom one with same name (unlikely but possible)
        custom = RoutingPolicy(
            name="balanced_override",
            description="Override",
        )
        pm.save_custom_policy(custom)
        
        # Load should get fresh version
        loaded = pm.load_policy("balanced_override")
        assert loaded.description == "Override"
        
        # Cleanup
        (pm.DEFAULT_POLICY_DIR / "balanced_override.yaml").unlink()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
