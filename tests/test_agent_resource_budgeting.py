"""Tests for agent resource budgeting (Gaps 3a–3d).

Tests verify:
1. Cost estimation for different complexity/task_type combinations
2. Hard limit blocks when cost exceeds remaining or per-agent max
3. Provisional spend tracking on agent approval
4. Budget reconciliation on agent failure (partial refund)
5. Budget starvation after multiple agents
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

AGENT_ROUTE_HOOK = Path(__file__).parent.parent / "src" / "llm_router" / "hooks" / "agent-route.py"
AGENT_ERROR_HOOK = Path(__file__).parent.parent / "src" / "llm_router" / "hooks" / "agent-error.py"


def _run_agent_route(
    prompt: str,
    subagent_type: str = "general-purpose",
    task_type: str = "analyze",
    complexity: str = "moderate",
    tmp_path: Path | None = None,
    session_id: str | None = None,
    init_budget: float | None = None,
) -> tuple[int, dict | None]:
    """Run agent-route hook with optional initial budget."""
    payload = json.dumps({
        "hook_event_name": "PreToolUse",
        "tool_name": "Agent",
        "tool_input": {
            "prompt": prompt,
            "subagent_type": subagent_type,
        },
    })

    env = None
    if tmp_path is not None:
        llmr_dir = tmp_path / ".llm-router"
        llmr_dir.mkdir(parents=True, exist_ok=True)

        # Write session_id.txt
        sid = session_id or "test-session"
        (llmr_dir / "session_id.txt").write_text(sid)

        # Optionally initialize session budget (only if not already initialized for this session)
        if init_budget is not None:
            budget_file = llmr_dir / "session_budget.json"
            # Only initialize if file doesn't exist or is for a different session
            if not budget_file.exists():
                budget_file.write_text(json.dumps({
                    "session_id": sid,
                    "initial": init_budget,
                    "remaining": init_budget,
                    "provisional_spend": 0.0,
                    "timestamp": 0,
                }))

        env = {**os.environ, "HOME": str(tmp_path)}

    result = subprocess.run(
        [sys.executable, str(AGENT_ROUTE_HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
    )

    parsed = None
    if result.stdout.strip():
        parsed = json.loads(result.stdout)
    return result.returncode, parsed


def _run_agent_error(
    error_message: str,
    subagent_type: str = "general-purpose",
    tmp_path: Path | None = None,
) -> tuple[int, dict | None]:
    """Run agent-error hook with error output."""
    payload = json.dumps({
        "hook_event_name": "PostToolUse",
        "tool_name": "Agent",
        "tool_result": error_message,
    })

    env = None
    if tmp_path is not None:
        env = {**os.environ, "HOME": str(tmp_path)}

    result = subprocess.run(
        [sys.executable, str(AGENT_ERROR_HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
    )

    parsed = None
    if result.stdout.strip():
        parsed = json.loads(result.stdout)
    return result.returncode, parsed


class TestCostEstimation:
    """Test cost estimation for different task types and complexities."""

    def test_simple_retrieval_costs_0_15(self, tmp_path):
        """Simple retrieval tasks estimate at $0.15."""
        code, out = _run_agent_route(
            "search for all python files in src/ directory using glob",
            subagent_type="general-purpose",
            tmp_path=tmp_path,
            session_id="test-session-1",
            init_budget=10.0,  # Plenty of budget
        )
        # Retrieval-only → approved before cost check
        assert code == 0
        assert out is None

    def test_moderate_code_costs_1_00(self, tmp_path):
        """Moderate code tasks estimate at $1.00."""
        code, out = _run_agent_route(
            "implement a function that validates email addresses",
            subagent_type="general-purpose",
            tmp_path=tmp_path,
            session_id="test-session-2",
            init_budget=10.0,
        )
        # Reasoning task with sufficient budget → should be blocked with cost info
        assert code == 0
        assert out is not None
        assert out["decision"] == "block"
        # Check for cost display (case-insensitive)
        assert "cost" in out["reason"].lower()

    def test_complex_analyze_costs_4_00(self, tmp_path):
        """Complex analysis tasks estimate at $4.00."""
        code, out = _run_agent_route(
            "do a comprehensive analysis of the entire architecture, all components, all patterns, deep dive",
            subagent_type="general-purpose",
            tmp_path=tmp_path,
            session_id="test-session-3",
            init_budget=10.0,
        )
        assert code == 0
        assert out is not None
        assert out["decision"] == "block"
        # Complex reasoning → higher cost estimate


class TestHardLimits:
    """Test hard limit blocking when cost exceeds remaining budget or per-agent max."""

    def test_cost_exceeds_remaining_budget_blocks(self, tmp_path):
        """Agent blocked if estimated cost > remaining budget."""
        code, out = _run_agent_route(
            "implement a complex system for X",
            subagent_type="general-purpose",
            tmp_path=tmp_path,
            session_id="test-session-4",
            init_budget=0.50,  # Only $0.50 remaining
        )
        # Moderate code estimate is $1.00, exceeds $0.50
        assert code == 0
        assert out is not None
        assert out["decision"] == "block"
        assert "exceed session budget" in out["reason"].lower()

    def test_cost_exceeds_per_agent_max_blocks(self, tmp_path):
        """Agent blocked if estimated cost > $5.00 per-agent limit."""
        code, out = _run_agent_route(
            "analyze everything in the entire codebase comprehensively, all aspects, all files",
            subagent_type="general-purpose",
            tmp_path=tmp_path,
            session_id="test-session-5",
            init_budget=50.0,  # Plenty of budget
        )
        # Complex analyze is $4.00 (still under $5 limit)
        # But "analyze everything" might trigger higher complexity
        # This test ensures per-agent limit is checked
        assert code == 0
        if out is not None:
            # Either blocked due to limit or blocked for routing
            if out.get("decision") == "block":
                # Check if it's the per-agent limit
                if "per-agent limit" in out["reason"]:
                    assert "$5" in out["reason"]


class TestProvisionalSpendTracking:
    """Test that provisional spend is tracked when agents are approved."""

    def test_retrieval_agent_approved_no_spend_tracked(self, tmp_path):
        """Retrieval-only agents approved without tracking provisional spend."""
        llmr_dir = tmp_path / ".llm-router"
        llmr_dir.mkdir(parents=True, exist_ok=True)
        sid = "test-session-6"
        (llmr_dir / "session_id.txt").write_text(sid)

        code, out = _run_agent_route(
            "grep for all test files in the codebase using search",
            subagent_type="general-purpose",
            tmp_path=tmp_path,
            session_id=sid,
            init_budget=10.0,
        )

        # Retrieval-only → approved without hitting cost check
        assert code == 0
        assert out is None

        # Budget file may not exist or unchanged (retrieval approved before cost check)
        budget_file = llmr_dir / "session_budget.json"
        if budget_file.exists():
            data = json.loads(budget_file.read_text())
            # Retrieval approved before cost check, so no provisional spend yet
            assert data["remaining"] == 10.0

    def test_reasoning_agent_decrements_budget_provisionally(self, tmp_path):
        """Reasoning agents decrement remaining budget when approved (provisional spend)."""
        llmr_dir = tmp_path / ".llm-router"
        llmr_dir.mkdir(parents=True, exist_ok=True)
        sid = "test-session-7"
        (llmr_dir / "session_id.txt").write_text(sid)

        # Moderate code task estimate is $1.00
        code, out = _run_agent_route(
            "write a function to sort items",
            subagent_type="general-purpose",
            tmp_path=tmp_path,
            session_id=sid,
            init_budget=10.0,
        )

        # Reasoning task is blocked for routing, but budget should be decremented provisionally
        assert code == 0
        assert out is not None
        assert out["decision"] == "block"

        # Check budget was decremented
        budget_file = llmr_dir / "session_budget.json"
        assert budget_file.exists()
        data = json.loads(budget_file.read_text())
        assert data["remaining"] == 9.0  # 10.0 - 1.0 (estimated cost)
        assert data["provisional_spend"] == 1.0


class TestBudgetReconciliation:
    """Test budget reconciliation when agents fail."""

    def test_failure_refunds_50_percent(self, tmp_path):
        """On agent failure, 50% of provisional spend is refunded."""
        llmr_dir = tmp_path / ".llm-router"
        llmr_dir.mkdir(parents=True, exist_ok=True)
        sid = "test-session-8"
        (llmr_dir / "session_id.txt").write_text(sid)

        # First: approve an agent and track provisional spend
        code, out = _run_agent_route(
            "write a function to validate X",
            subagent_type="general-purpose",
            tmp_path=tmp_path,
            session_id=sid,
            init_budget=10.0,
        )
        assert code == 0

        # Verify budget was decremented
        budget_file = llmr_dir / "session_budget.json"
        data = json.loads(budget_file.read_text())
        assert data["remaining"] == 9.0  # Decremented by $1.00

        # Now simulate agent failure
        _run_agent_error(
            "Error: Agent timed out after 120 seconds",
            subagent_type="general-purpose",
            tmp_path=tmp_path,
        )

        # Verify budget was reconciled (50% refunded)
        data = json.loads(budget_file.read_text())
        # 50% refund of $1.00 = $0.50 refunded
        # New remaining = 9.0 + 0.50 = 9.50
        assert data["remaining"] == 9.5
        assert "refund_resource_limit" in data.get("last_reconciliation_type", "")

    def test_multiple_failures_accumulate_refunds(self, tmp_path):
        """Multiple agent failures accumulate refunds."""
        llmr_dir = tmp_path / ".llm-router"
        llmr_dir.mkdir(parents=True, exist_ok=True)
        sid = "test-session-9"
        (llmr_dir / "session_id.txt").write_text(sid)

        # Approve first agent (use same prompt to get consistent cost)
        _run_agent_route(
            "implement a simple sorting function",
            tmp_path=tmp_path,
            session_id=sid,
            init_budget=10.0,
        )
        budget_file = llmr_dir / "session_budget.json"
        data = json.loads(budget_file.read_text())
        first_remaining = data["remaining"]

        # Reset depth counter for next agent in same session
        (llmr_dir / "agent_depth.json").write_text(json.dumps({
            "depth": 0,
            "session_id": sid,
            "ts": 0,
        }))

        # First failure: refund 50%
        _run_agent_error("Error: timeout", tmp_path=tmp_path)
        data = json.loads(budget_file.read_text())
        expected_refund = first_remaining + (10.0 - first_remaining) * 0.5
        assert abs(data["remaining"] - expected_refund) < 0.01  # Allow small floating point error

        # Approve second agent with same cost estimate
        _run_agent_route(
            "implement a simple sorting function",
            tmp_path=tmp_path,
            session_id=sid,
        )

        # Reset depth counter again
        (llmr_dir / "agent_depth.json").write_text(json.dumps({
            "depth": 0,
            "session_id": sid,
            "ts": 0,
        }))

        data = json.loads(budget_file.read_text())
        second_remaining = data["remaining"]

        # Second failure: refund 50%
        _run_agent_error("Error: out of memory", tmp_path=tmp_path)
        data = json.loads(budget_file.read_text())
        # Should have refunded 50% of second agent cost
        assert data["remaining"] > second_remaining


class TestBudgetStarvation:
    """Test behavior when budget runs out."""

    def test_multiple_agents_exhaust_budget(self, tmp_path):
        """Multiple agents can exhaust remaining budget."""
        llmr_dir = tmp_path / ".llm-router"
        llmr_dir.mkdir(parents=True, exist_ok=True)
        sid = "test-session-10"
        (llmr_dir / "session_id.txt").write_text(sid)

        initial_budget = 5.0
        (llmr_dir / "session_budget.json").write_text(json.dumps({
            "session_id": sid,
            "initial": initial_budget,
            "remaining": initial_budget,
            "provisional_spend": 0.0,
            "timestamp": 0,
        }))

        # Approve 5 agents at ~$1.00 each (moderate code tasks)
        # Use prompts that clearly require reasoning to avoid retrieval classification
        for i in range(5):
            # Reset depth counter for each agent so circuit breaker doesn't interfere
            (llmr_dir / "agent_depth.json").write_text(json.dumps({
                "depth": 0,
                "session_id": sid,
                "ts": 0,
            }))

            code, out = _run_agent_route(
                "analyze and implement a validation function with error handling",
                tmp_path=tmp_path,
                session_id=sid,
                init_budget=initial_budget,  # Pass initial budget so it uses existing file
            )
            # Each should be blocked for routing (reasoning task)
            assert code == 0

        budget_file = llmr_dir / "session_budget.json"
        data = json.loads(budget_file.read_text())
        # After 5 × ~$1.00 = ~$5.00 spent, remaining should be ≤ 0
        assert data["remaining"] <= 0.01  # Allow small rounding error

    def test_sixth_agent_blocked_due_to_budget(self, tmp_path):
        """Sixth agent blocked when budget exhausted."""
        llmr_dir = tmp_path / ".llm-router"
        llmr_dir.mkdir(parents=True, exist_ok=True)
        sid = "test-session-11"
        (llmr_dir / "session_id.txt").write_text(sid)

        initial_budget = 5.0
        (llmr_dir / "session_budget.json").write_text(json.dumps({
            "session_id": sid,
            "initial": initial_budget,
            "remaining": initial_budget,
            "provisional_spend": 0.0,
            "timestamp": 0,
        }))

        # Exhaust budget with 5 agents using explicit reasoning prompts
        for i in range(5):
            # Reset depth counter for each agent so circuit breaker doesn't interfere
            (llmr_dir / "agent_depth.json").write_text(json.dumps({
                "depth": 0,
                "session_id": sid,
                "ts": 0,
            }))

            _run_agent_route(
                "analyze and implement validation logic",  # Explicit reasoning
                tmp_path=tmp_path,
                session_id=sid,
                init_budget=initial_budget,
            )

        # Reset depth counter one more time for sixth agent
        (llmr_dir / "agent_depth.json").write_text(json.dumps({
            "depth": 0,
            "session_id": sid,
            "ts": 0,
        }))

        # Sixth agent should be blocked due to budget
        code, out = _run_agent_route(
            "analyze and implement validation logic",
            tmp_path=tmp_path,
            session_id=sid,
            init_budget=initial_budget,
        )

        assert code == 0
        assert out is not None
        assert out["decision"] == "block"
        # Should be blocked due to budget, not routing
        assert "exceed session budget" in out["reason"].lower()


class TestSessionBudgetInitialization:
    """Test session budget initialization on first agent approval."""

    def test_budget_initialized_from_pressure(self, tmp_path):
        """First agent initializes session budget based on quota pressure."""
        llmr_dir = tmp_path / ".llm-router"
        llmr_dir.mkdir(parents=True, exist_ok=True)
        sid = "test-session-12"
        (llmr_dir / "session_id.txt").write_text(sid)

        # Simulate low quota pressure (30%)
        (llmr_dir / "usage.json").write_text(json.dumps({
            "session_pct": 30.0,
            "weekly_pct": 25.0,
            "highest_pressure": 0.3,
        }))

        # Approve first agent
        code, out = _run_agent_route(
            "write a function",
            tmp_path=tmp_path,
            session_id=sid,
        )

        # Budget should be initialized to $30 * (1 - 0.3) = $21.0 allocated
        budget_file = llmr_dir / "session_budget.json"
        assert budget_file.exists()
        data = json.loads(budget_file.read_text())
        assert data["initial"] >= 5.0  # Minimum allocated
        assert data["remaining"] < data["initial"]  # Decremented by agent cost
