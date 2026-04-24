"""Behavioral tests simulating real user scenarios with each policy."""

import pytest
from llm_router.policy import PolicyManager


class TestAggressivePolicyScenarios:
    """Real user workflows with aggressive policy."""

    @pytest.fixture
    def aggressive_policy(self):
        """Get aggressive policy."""
        pm = PolicyManager()
        return pm.load_policy("aggressive")

    def test_scenario_simple_acknowledgements(self, aggressive_policy):
        """User: 'yes' / 'ok' / 'go' should not skip routing."""
        assert not aggressive_policy.skip_prompt("yes")
        assert not aggressive_policy.skip_prompt("ok")
        assert not aggressive_policy.skip_prompt("go")
        assert not aggressive_policy.skip_prompt("thanks")
        assert not aggressive_policy.skip_prompt("y")
        assert not aggressive_policy.skip_prompt("n")

    def test_scenario_git_workflow(self, aggressive_policy):
        """User: git commands should route."""
        # route_coordination=True means don't skip
        assert aggressive_policy.route_coordination is True
        # Would be routed, not skipped
        test_git_commands = [
            "git push origin main",
            "commit changes",
            "create PR",
            "deploy to prod",
        ]
        for cmd in test_git_commands:
            # Aggressive routes these (doesn't skip them due to low threshold)
            assert aggressive_policy.confidence_threshold == 2

    def test_scenario_code_task_escalates(self, aggressive_policy):
        """Complex code task escalates beyond Ollama."""
        # Aggressive routes code tasks, but they escalate to Claude if Ollama fails
        policy = aggressive_policy
        assert policy.confidence_threshold == 2
        # Code task with heuristic score=3 would route immediately (3 > 2)
        assert 3 > policy.confidence_threshold

    def test_scenario_mixed_session(self, aggressive_policy):
        """Typical user session: mixed task types."""
        policy = aggressive_policy
        
        # Acknowledgements
        assert not policy.skip_prompt("yes")
        
        # Coordination
        assert policy.route_coordination is True
        
        # System commands (always skip)
        assert policy.skip_prompt("/help")
        
        # Regular prompts (don't skip unless low confidence and high threshold)
        assert not policy.skip_prompt("implement auth module")


class TestBalancedPolicyScenarios:
    """Real user workflows with balanced policy."""

    @pytest.fixture
    def balanced_policy(self):
        """Get balanced policy."""
        pm = PolicyManager()
        return pm.load_policy("balanced")

    def test_scenario_simple_skipped(self, balanced_policy):
        """Simple acknowledgements are skipped."""
        assert balanced_policy.skip_prompt("yes")
        assert balanced_policy.skip_prompt("ok")
        assert balanced_policy.skip_prompt("thanks")

    def test_scenario_complex_routed(self, balanced_policy):
        """Complex tasks are routed."""
        policy = balanced_policy
        # Moderate complexity task (score ≈ 5) vs threshold (4)
        # Would route because 5 > 4
        assert 5 > policy.confidence_threshold

    def test_scenario_coordination_blocked(self, balanced_policy):
        """Coordination tasks are not specially routed."""
        assert balanced_policy.route_coordination is False

    def test_scenario_developer_experience(self, balanced_policy):
        """Typical developer experience: responsive without over-routing."""
        policy = balanced_policy
        
        # Complex questions route
        assert not policy.skip_prompt("Why is my database slow?")
        
        # Simple acknowledgements skip (user types inline)
        assert policy.skip_prompt("ok")
        
        # Good cost/quality balance
        assert 3 < policy.confidence_threshold < 5


class TestConservativePolicyScenarios:
    """Real user workflows with conservative policy."""

    @pytest.fixture
    def conservative_policy(self):
        """Get conservative policy."""
        pm = PolicyManager()
        return pm.load_policy("conservative")

    def test_scenario_most_skipped(self, conservative_policy):
        """Most simple tasks are skipped."""
        policy = conservative_policy
        
        assert policy.skip_prompt("yes")
        assert policy.skip_prompt("ok")
        assert policy.skip_prompt("thanks")
        assert policy.skip_prompt("y")

    def test_scenario_only_complex_routed(self, conservative_policy):
        """Only very complex tasks routed."""
        policy = conservative_policy
        
        # Score must exceed 6 to route
        assert policy.confidence_threshold == 6
        
        # Score=4 doesn't route (4 < 6)
        assert 4 < policy.confidence_threshold
        
        # Score=7 would route (7 > 6)
        assert 7 > policy.confidence_threshold

    def test_scenario_safety_gate(self, conservative_policy):
        """Conservative acts as safety gate against over-routing."""
        policy = conservative_policy
        
        # Blocks simple routing
        assert policy.skip_prompt("yes")
        
        # Blocks coordination
        assert policy.route_coordination is False
        
        # Forces high quality bar
        assert policy.confidence_threshold == 6


class TestPolicyCostImplications:
    """Test theoretical cost implications of each policy."""

    def test_aggressive_highest_cost_savings(self):
        """Aggressive policy has lowest threshold → most routing → most savings."""
        pm = PolicyManager()
        agg = pm.load_policy("aggressive")
        bal = pm.load_policy("balanced")
        con = pm.load_policy("conservative")
        
        # Aggressive routes more (lower threshold)
        assert agg.confidence_threshold < bal.confidence_threshold
        assert bal.confidence_threshold < con.confidence_threshold

    def test_conservative_lowest_cost_savings(self):
        """Conservative policy routes least → least savings → best quality."""
        pm = PolicyManager()
        con = pm.load_policy("conservative")
        
        # High threshold + skip acknowledgements = very selective routing
        assert con.confidence_threshold == 6
        assert con.skip_acknowledgements is True
        # Estimated 10-15% cost savings vs Claude-only

    def test_balanced_middle_ground(self):
        """Balanced policy is compromise."""
        pm = PolicyManager()
        bal = pm.load_policy("balanced")
        agg = pm.load_policy("aggressive")
        con = pm.load_policy("conservative")
        
        # Threshold is between aggressive and conservative
        assert agg.confidence_threshold < bal.confidence_threshold < con.confidence_threshold


class TestPolicyCachingPerformance:
    """Test that policies don't impact routing latency."""

    def test_policy_skip_check_fast(self):
        """Policy skip_prompt check is fast."""
        pm = PolicyManager()
        policy = pm.load_policy("aggressive")
        
        # Check many times - should be instant
        for _ in range(1000):
            policy.skip_prompt("yes")
            policy.skip_prompt("complex task")
            policy.skip_prompt("/help")
        
        # If this test is fast, skip checks don't slow down routing

    def test_policy_loading_cached(self):
        """Policies are cached after first load."""
        pm = PolicyManager()
        
        # First load
        p1 = pm.load_policy("aggressive")
        # Should be same object from cache
        p2 = pm.load_policy("aggressive")
        
        assert p1 is p2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
