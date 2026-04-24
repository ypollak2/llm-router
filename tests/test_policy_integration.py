"""Integration tests for policy system with routing hooks."""

import pytest
from llm_router.policy import RoutingPolicy, PolicyManager, get_active_policy


class TestPolicyEnvironmentIntegration:
    """Test policy loading from environment variables."""

    def test_policy_from_env_var(self, monkeypatch):
        """Load policy from LLM_ROUTER_POLICY environment variable."""
        monkeypatch.setenv("LLM_ROUTER_POLICY", "aggressive")
        pm = PolicyManager()
        policy = pm.get_active_policy()
        assert policy.name == "aggressive"

    def test_policy_fallback_to_balanced(self, monkeypatch):
        """Fallback to balanced if env var not set."""
        monkeypatch.delenv("LLM_ROUTER_POLICY", raising=False)
        pm = PolicyManager()
        policy = pm.get_active_policy()
        assert policy is not None

    def test_policy_invalid_env_fallback(self, monkeypatch):
        """Fallback to balanced if env var points to missing policy."""
        monkeypatch.setenv("LLM_ROUTER_POLICY", "nonexistent_xyz")
        pm = PolicyManager()
        # Should not raise, should fallback
        policy = pm.get_active_policy()
        assert policy.name == "balanced"


class TestPolicyPresetBehavior:
    """Test behavior of preset policies."""

    def test_aggressive_routes_acknowledgements(self):
        """Aggressive policy routes acknowledgements."""
        pm = PolicyManager()
        policy = pm.load_policy("aggressive")
        assert not policy.skip_prompt("yes")
        assert not policy.skip_prompt("ok")
        assert policy.route_coordination is True

    def test_aggressive_low_threshold(self):
        """Aggressive policy has low confidence threshold."""
        pm = PolicyManager()
        policy = pm.load_policy("aggressive")
        assert policy.confidence_threshold == 2

    def test_balanced_skips_acknowledgements(self):
        """Balanced policy skips acknowledgements."""
        pm = PolicyManager()
        policy = pm.load_policy("balanced")
        assert policy.skip_prompt("yes")
        assert policy.skip_prompt("ok")
        assert policy.route_coordination is False

    def test_conservative_skips_everything(self):
        """Conservative policy is most restrictive."""
        pm = PolicyManager()
        policy = pm.load_policy("conservative")
        assert policy.skip_prompt("yes")
        assert policy.skip_prompt("ok")
        assert policy.route_coordination is False
        assert policy.confidence_threshold == 6


class TestMultiplePolicySessions:
    """Test managing multiple policies in one session."""

    def test_switch_policies_updates_behavior(self):
        """Switching policies changes skip behavior."""
        pm = PolicyManager()
        
        # Start with aggressive
        pm.set_active_policy("aggressive")
        agg_policy = pm.get_active_policy()
        assert not agg_policy.skip_prompt("yes")
        
        # Switch to conservative
        pm.set_active_policy("conservative")
        con_policy = pm.get_active_policy()
        assert con_policy.skip_prompt("yes")
        
        # Back to aggressive
        pm.set_active_policy("aggressive")
        agg_policy2 = pm.get_active_policy()
        assert not agg_policy2.skip_prompt("yes")

    def test_policy_consistency_across_calls(self):
        """Policy remains consistent if not switched."""
        pm = PolicyManager()
        pm.set_active_policy("balanced")
        
        # Multiple calls should get same policy
        for _ in range(5):
            policy = pm.get_active_policy()
            assert policy.name == "balanced"
            assert policy.skip_prompt("yes") is True


class TestPolicySkipPatterns:
    """Test skip pattern matching in policies."""

    def test_system_commands_always_skipped(self):
        """System commands like /help are skipped."""
        pm = PolicyManager()
        
        for policy_name in ["aggressive", "balanced", "conservative"]:
            policy = pm.load_policy(policy_name)
            # All policies skip /help, /login, etc
            assert policy.skip_prompt("/help")
            assert policy.skip_prompt("/login")
            assert policy.skip_prompt("/clear")

    def test_coordination_pattern_in_aggressive(self):
        """Aggressive policy routes coordination tasks."""
        pm = PolicyManager()
        agg = pm.load_policy("aggressive")
        
        # Should not skip coordination tasks
        assert not agg.skip_prompt("git push origin main")
        assert agg.route_coordination is True

    def test_coordination_pattern_not_in_conservative(self):
        """Conservative policy doesn't route coordination."""
        pm = PolicyManager()
        con = pm.load_policy("conservative")
        
        # route_coordination=false means skip or don't route
        assert con.route_coordination is False


class TestPolicyGlobalFunction:
    """Test global get_active_policy() function."""

    def test_get_active_policy_returns_policy(self):
        """Global function returns RoutingPolicy."""
        policy = get_active_policy()
        assert isinstance(policy, RoutingPolicy)
        assert policy.name in ("aggressive", "balanced", "conservative")

    def test_get_active_policy_uses_env(self, monkeypatch):
        """Global function respects LLM_ROUTER_POLICY env var."""
        monkeypatch.setenv("LLM_ROUTER_POLICY", "aggressive")
        # Force reload of policy manager
        import llm_router.policy
        llm_router.policy._policy_manager = llm_router.policy.PolicyManager()
        
        policy = get_active_policy()
        assert policy.name == "aggressive"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
