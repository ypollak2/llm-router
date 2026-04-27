"""Tests for the agent-route PreToolUse hook — circuit breaker for Agent loop prevention.

Tests verify:
1. Depth guard blocks when nesting ≥ max_depth
2. Explore agents are always exempt
3. Session ID reset clears depth counter
4. Depth is incremented on approval
5. Missing/malformed state files handled gracefully
6. Environment variable LLM_ROUTER_MAX_AGENT_DEPTH overrides default
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HOOK_PATH = Path(__file__).parent.parent / "src" / "llm_router" / "hooks" / "agent-route.py"


def _run(
    prompt: str,
    subagent_type: str = "general-purpose",
    session_id: str | None = None,
    agent_depth: int | None = None,
    max_depth: str | None = None,
    tmp_path: Path | None = None,
) -> tuple[int, dict | None]:
    """Run the agent-route hook with given parameters.

    Args:
        prompt: Agent prompt.
        subagent_type: Agent type (e.g. "Explore", "general-purpose").
        session_id: Session ID to write to agent_depth.json (if agent_depth is set).
        agent_depth: Current nesting depth to write to agent_depth.json.
        max_depth: Value for LLM_ROUTER_MAX_AGENT_DEPTH env var.
        tmp_path: Temp directory for HOME.

    Returns:
        (exit_code, parsed_stdout_dict_or_None)
    """
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

        # Write agent_depth.json if depth is specified
        if agent_depth is not None and session_id is not None:
            (llmr_dir / "agent_depth.json").write_text(json.dumps({
                "depth": agent_depth,
                "session_id": session_id,
                "ts": 0,
            }))

        # Write session_id.txt
        if session_id is not None:
            (llmr_dir / "session_id.txt").write_text(session_id)

        env = {**os.environ, "HOME": str(tmp_path)}

    if max_depth is not None:
        if env is None:
            env = os.environ.copy()
        env["LLM_ROUTER_MAX_AGENT_DEPTH"] = max_depth

    result = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
    )

    parsed = None
    if result.stdout.strip():
        parsed = json.loads(result.stdout)
    return result.returncode, parsed


class TestDepthGuardBlocks:
    """Test that depth guard blocks when nesting exceeds max_depth."""

    def test_at_max_depth_blocks(self, tmp_path):
        """When current_depth == max_depth, new Agent calls are blocked."""
        code, out = _run(
            "analyze the codebase",
            subagent_type="general-purpose",
            session_id="test-session-1",
            agent_depth=3,  # at max (3)
            max_depth="3",
            tmp_path=tmp_path,
        )
        assert code == 0
        assert out is not None
        assert out["decision"] == "block"
        assert "circuit breaker" in out["reason"].lower()
        assert "3/3" in out["reason"]

    def test_above_max_depth_blocks(self, tmp_path):
        """When current_depth > max_depth, new Agent calls are blocked."""
        code, out = _run(
            "analyze the codebase",
            subagent_type="general-purpose",
            session_id="test-session-2",
            agent_depth=4,  # above max (3)
            max_depth="3",
            tmp_path=tmp_path,
        )
        assert code == 0
        assert out is not None
        assert out["decision"] == "block"
        assert "circuit breaker" in out["reason"].lower()
        assert "4/3" in out["reason"]

    def test_below_max_depth_approves_then_increments(self, tmp_path):
        """When current_depth < max_depth, Agent calls are approved and depth increments."""
        code, out = _run(
            "list all files in src/",
            subagent_type="general-purpose",
            session_id="test-session-3",
            agent_depth=0,  # below max (3)
            max_depth="3",
            tmp_path=tmp_path,
        )
        # Retrieval-only task → approved with sys.exit(0)
        assert code == 0
        assert out is None

        # Verify depth was incremented
        depth_file = tmp_path / ".llm-router" / "agent_depth.json"
        assert depth_file.exists()
        data = json.loads(depth_file.read_text())
        assert data["depth"] == 1


class TestDepthGuardExemptExplore:
    """Test that Explore agents are always exempt from depth guard."""

    def test_explore_at_max_depth_approved(self, tmp_path):
        """Explore agents are approved even at max depth."""
        code, out = _run(
            "find all test files",
            subagent_type="Explore",
            session_id="test-session-4",
            agent_depth=3,  # at max
            max_depth="3",
            tmp_path=tmp_path,
        )
        assert code == 0
        # Explore is approved before depth check
        assert out is None

    def test_explore_at_very_high_depth_approved(self, tmp_path):
        """Explore agents approved even if depth is 100."""
        code, out = _run(
            "search for references to foo",
            subagent_type="Explore",
            session_id="test-session-5",
            agent_depth=100,
            max_depth="3",
            tmp_path=tmp_path,
        )
        assert code == 0
        assert out is None


class TestSessionReset:
    """Test that different sessions reset depth to 0."""

    def test_new_session_resets_depth(self, tmp_path):
        """When session_id in agent_depth.json differs, depth resets to 0."""
        # Write depth for session-old
        (tmp_path / ".llm-router").mkdir(parents=True, exist_ok=True)
        (tmp_path / ".llm-router" / "agent_depth.json").write_text(json.dumps({
            "depth": 5,
            "session_id": "session-old",
            "ts": 0,
        }))
        (tmp_path / ".llm-router" / "session_id.txt").write_text("session-new")

        # Run with session-new
        code, out = _run(
            "list files",
            subagent_type="general-purpose",
            session_id="session-new",
            max_depth="3",
            tmp_path=tmp_path,
        )

        # Should be approved (depth=0 < max=3, retrieval-only task)
        assert code == 0
        assert out is None

        # Verify depth was reset and incremented
        depth_file = tmp_path / ".llm-router" / "agent_depth.json"
        data = json.loads(depth_file.read_text())
        assert data["session_id"] == "session-new"
        assert data["depth"] == 1


class TestDepthIncrement:
    """Test that approval increments depth counter."""

    def test_depth_incremented_on_reasoning_approval(self, tmp_path):
        """When a reasoning task is approved, depth is incremented."""
        code, out = _run(
            "implement the foo function",
            subagent_type="general-purpose",
            session_id="test-session-6",
            agent_depth=0,
            max_depth="3",
            tmp_path=tmp_path,
        )
        # Reasoning task not retrieval-only → classified and may block or approve
        # But depth should be incremented before that decision
        assert code == 0

        # Check depth file — should be incremented
        depth_file = tmp_path / ".llm-router" / "agent_depth.json"
        assert depth_file.exists()
        data = json.loads(depth_file.read_text())
        assert data["depth"] == 1


class TestMissingFiles:
    """Test handling of missing/malformed state files."""

    def test_missing_agent_depth_json_defaults_to_0(self, tmp_path):
        """Missing agent_depth.json defaults to depth=0."""
        code, out = _run(
            "search for references to foo",
            subagent_type="general-purpose",
            session_id="test-session-7",
            agent_depth=None,  # Don't write agent_depth.json
            max_depth="3",
            tmp_path=tmp_path,
        )
        # Retrieval-only → approved
        assert code == 0
        assert out is None

        # Verify it was created
        depth_file = tmp_path / ".llm-router" / "agent_depth.json"
        assert depth_file.exists()
        data = json.loads(depth_file.read_text())
        assert data["depth"] == 1

    def test_missing_session_id_txt_defaults_to_unknown(self, tmp_path):
        """Missing session_id.txt defaults to 'unknown'."""
        code, out = _run(
            "list files in src/",
            subagent_type="general-purpose",
            session_id=None,  # Don't write session_id.txt
            agent_depth=0,
            max_depth="3",
            tmp_path=tmp_path,
        )
        # Retrieval-only → approved
        assert code == 0
        assert out is None

        # Depth file should use 'unknown' as session_id
        depth_file = tmp_path / ".llm-router" / "agent_depth.json"
        assert depth_file.exists()
        data = json.loads(depth_file.read_text())
        assert data["session_id"] == "unknown"

    def test_malformed_agent_depth_json_defaults_to_0(self, tmp_path):
        """Malformed agent_depth.json defaults to depth=0."""
        (tmp_path / ".llm-router").mkdir(parents=True, exist_ok=True)
        (tmp_path / ".llm-router" / "agent_depth.json").write_text("not valid json {{{")
        (tmp_path / ".llm-router" / "session_id.txt").write_text("test-session-8")

        code, out = _run(
            "find files",
            subagent_type="general-purpose",
            session_id="test-session-8",
            max_depth="3",
            tmp_path=tmp_path,
        )
        # Should still work (depth defaults to 0)
        assert code == 0
        assert out is None


class TestEnvVarOverride:
    """Test that LLM_ROUTER_MAX_AGENT_DEPTH env var overrides default."""

    def test_env_var_max_depth_5(self, tmp_path):
        """LLM_ROUTER_MAX_AGENT_DEPTH=5 blocks at depth=5."""
        code, out = _run(
            "analyze the codebase",
            subagent_type="general-purpose",
            session_id="test-session-9",
            agent_depth=5,  # at max (5)
            max_depth="5",
            tmp_path=tmp_path,
        )
        assert code == 0
        assert out is not None
        assert out["decision"] == "block"
        assert "5/5" in out["reason"]

    def test_env_var_max_depth_1(self, tmp_path):
        """LLM_ROUTER_MAX_AGENT_DEPTH=1 blocks at depth=1."""
        code, out = _run(
            "analyze",
            subagent_type="general-purpose",
            session_id="test-session-10",
            agent_depth=1,  # at max (1)
            max_depth="1",
            tmp_path=tmp_path,
        )
        assert code == 0
        assert out is not None
        assert out["decision"] == "block"
        assert "1/1" in out["reason"]

    def test_env_var_invalid_defaults_to_3(self, tmp_path):
        """Invalid LLM_ROUTER_MAX_AGENT_DEPTH defaults to 3."""
        code, out = _run(
            "analyze",
            subagent_type="general-purpose",
            session_id="test-session-11",
            agent_depth=3,  # at default (3)
            max_depth="not_a_number",
            tmp_path=tmp_path,
        )
        assert code == 0
        assert out is not None
        assert out["decision"] == "block"
        assert "3/3" in out["reason"]


class TestDecisionReason:
    """Test that block reasons are clear and actionable."""

    def test_block_reason_includes_depth(self, tmp_path):
        """Block reason includes current and max depth."""
        code, out = _run(
            "analyze",
            subagent_type="general-purpose",
            session_id="test-session-12",
            agent_depth=2,
            max_depth="2",
            tmp_path=tmp_path,
        )
        assert out is not None
        assert "circuit breaker" in out["reason"].lower()
        assert "2/2" in out["reason"]
        assert "Too many nested agents" in out["reason"]
        assert "llm_* MCP tools" in out["reason"]


class TestNonAgentTool:
    """Test that non-Agent tools are approved (not intercepted)."""

    def test_non_agent_tool_exits_cleanly(self, tmp_path):
        """Non-Agent tools exit with code 0 (approved)."""
        payload = json.dumps({
            "hook_event_name": "PreToolUse",
            "tool_name": "Read",  # Not Agent
            "tool_input": {"file_path": "/some/file.txt"},
        })
        env = {**os.environ, "HOME": str(tmp_path)}
        result = subprocess.run(
            [sys.executable, str(HOOK_PATH)],
            input=payload,
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""  # No output (approved)
