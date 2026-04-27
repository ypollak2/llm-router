"""Tests for the agent-error PostToolUse[Agent] hook — error recovery and fallback suggestions.

Tests verify:
1. Failure type classification (timeout, OOM, parse, memory, unknown)
2. Task type detection (retrieval, analysis, code, generate)
3. Appropriate fallback suggestions based on failure + task type
4. Explore agents exempted (no suggestion)
5. Call history tracking and session handling
6. Edge cases (missing files, malformed JSON, success)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HOOK_PATH = Path(__file__).parent.parent / "src" / "llm_router" / "hooks" / "agent-error.py"


def _run(
    tool_result: str,
    subagent_type: str = "general-purpose",
    prompt: str | None = None,
    tmp_path: Path | None = None,
) -> tuple[int, dict | None]:
    """Run the agent-error hook with given parameters.

    Args:
        tool_result: The error output from the agent (or None for success).
        subagent_type: Agent type (e.g., "Explore", "general-purpose").
        prompt: The original agent prompt (to write to agent_calls.json).
        tmp_path: Temp directory for HOME.

    Returns:
        (exit_code, parsed_stdout_dict_or_None)
    """
    if tmp_path is not None:
        llmr_dir = tmp_path / ".llm-router"
        llmr_dir.mkdir(parents=True, exist_ok=True)

        # Write agent_calls.json if prompt is provided
        if prompt is not None:
            (llmr_dir / "agent_calls.json").write_text(json.dumps({
                "calls": [
                    {
                        "timestamp": 1000000000.0,
                        "subagent_type": subagent_type,
                        "prompt": prompt[:500],
                        "decision": "approved",
                        "session_id": "test-session",
                    }
                ],
                "version": 1,
            }))

    payload = json.dumps({
        "hook_event_name": "PostToolUse",
        "tool_name": "Agent",
        "tool_result": tool_result,
    })

    env = None
    if tmp_path is not None:
        env = {**os.environ, "HOME": str(tmp_path)}

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


class TestErrorClassification:
    """Test failure type classification from error messages."""

    def test_timeout_failure(self, tmp_path):
        """Timeout errors are classified as resource_limit."""
        code, out = _run(
            tool_result="Error: Agent timed out after 120 seconds",
            prompt="find all python files",
            tmp_path=tmp_path,
        )
        assert code == 0
        assert out is not None
        assert out["failure_type"] == "resource_limit"

    def test_quota_exceeded_failure(self, tmp_path):
        """Quota exceeded errors are classified as resource_limit."""
        code, out = _run(
            tool_result="Error: Token limit exceeded",
            prompt="analyze the entire codebase",
            tmp_path=tmp_path,
        )
        assert code == 0
        assert out is not None
        assert out["failure_type"] == "resource_limit"

    def test_parse_error_failure(self, tmp_path):
        """JSON/parse errors are classified as unparseable_output."""
        code, out = _run(
            tool_result="Error: Invalid JSON in agent output",
            prompt="implement a function",
            tmp_path=tmp_path,
        )
        assert code == 0
        assert out is not None
        assert out["failure_type"] == "unparseable_output"

    def test_memory_failure(self, tmp_path):
        """OOM/memory errors are classified as resource_crash."""
        code, out = _run(
            tool_result="Error: Out of memory",
            prompt="process large dataset",
            tmp_path=tmp_path,
        )
        assert code == 0
        assert out is not None
        assert out["failure_type"] == "resource_crash"

    def test_unknown_failure(self, tmp_path):
        """Unknown errors default to unknown_failure."""
        code, out = _run(
            tool_result="Error: Something went wrong",
            prompt="analyze code",
            tmp_path=tmp_path,
        )
        assert code == 0
        assert out is not None
        assert out["failure_type"] == "unknown_failure"


class TestRetrievalFallback:
    """Test fallback suggestions for retrieval tasks."""

    def test_find_files_timeout(self, tmp_path):
        """Retrieval task failure suggests Read/Grep/Glob."""
        code, out = _run(
            tool_result="Error: Agent timed out",
            prompt="find all typescript files in src/",
            tmp_path=tmp_path,
        )
        assert code == 0
        assert out is not None
        assert "Read" in out["suggestion"]
        assert "Glob" in out["suggestion"]
        assert "Grep" in out["suggestion"]

    def test_search_parse_error(self, tmp_path):
        """Retrieval failure with parse error still suggests file tools."""
        code, out = _run(
            tool_result="Error: Invalid JSON",
            prompt="search for references to MyClass",
            tmp_path=tmp_path,
        )
        assert code == 0
        assert out is not None
        assert "Read" in out["suggestion"] or "file-access tools" in out["suggestion"]

    def test_list_files_oom(self, tmp_path):
        """Retrieval with memory issue suggests file tools."""
        code, out = _run(
            tool_result="Error: Out of memory",
            prompt="list all files in the project",
            tmp_path=tmp_path,
        )
        assert code == 0
        assert out is not None
        # Should suggest file tools or breaking into chunks
        assert "Read" in out["suggestion"] or "chunks" in out["suggestion"]

    def test_grep_failure(self, tmp_path):
        """Grep-like task failure suggests appropriate tools."""
        code, out = _run(
            tool_result="Error: Agent failed",
            prompt="grep for all function definitions",
            tmp_path=tmp_path,
        )
        assert code == 0
        assert out is not None
        assert "Grep" in out["suggestion"] or "file" in out["suggestion"]


class TestReasoningFallback:
    """Test fallback suggestions for reasoning/analysis tasks."""

    def test_analysis_timeout(self, tmp_path):
        """Analysis task timeout suggests breaking into chunks."""
        code, out = _run(
            tool_result="Error: Timeout after 120s",
            prompt="analyze the performance characteristics of this codebase",
            tmp_path=tmp_path,
        )
        assert code == 0
        assert out is not None
        assert "chunks" in out["suggestion"].lower() or "llm_analyze" in out["suggestion"]

    def test_code_generation_parse_error(self, tmp_path):
        """Code generation failure with parse error suggests llm_code."""
        code, out = _run(
            tool_result="Error: Invalid JSON",
            prompt="implement a binary search function",
            tmp_path=tmp_path,
        )
        assert code == 0
        assert out is not None
        assert "llm_code" in out["suggestion"]

    def test_design_decision_memory_error(self, tmp_path):
        """Reasoning task with memory error suggests MCP tools."""
        code, out = _run(
            tool_result="Error: Out of memory",
            prompt="design the architecture for a distributed system",
            tmp_path=tmp_path,
        )
        assert code == 0
        assert out is not None
        assert "llm_analyze" in out["suggestion"] or "chunks" in out["suggestion"]


class TestResourceLimitFallback:
    """Test fallback suggestions for resource limit failures."""

    def test_timeout_suggests_breaking_into_chunks(self, tmp_path):
        """Timeout suggests breaking into smaller tasks."""
        code, out = _run(
            tool_result="Error: Timeout exceeded",
            prompt="analyze the entire codebase",
            tmp_path=tmp_path,
        )
        assert code == 0
        assert out is not None
        assert "chunks" in out["suggestion"].lower() or "smaller" in out["suggestion"].lower()

    def test_quota_exceeded_suggests_mcp_tools(self, tmp_path):
        """Quota exceeded suggests using MCP tools."""
        code, out = _run(
            tool_result="Error: Quota limit exceeded",
            prompt="implement a new feature",
            tmp_path=tmp_path,
        )
        assert code == 0
        assert out is not None
        assert "llm_" in out["suggestion"]

    def test_limit_exceeded_offers_alternatives(self, tmp_path):
        """Limit exceeded offers specific alternative tools."""
        code, out = _run(
            tool_result="Error: Resource limit hit",
            prompt="write comprehensive documentation",
            tmp_path=tmp_path,
        )
        assert code == 0
        assert out is not None
        # Should mention alternatives
        assert "try" in out["suggestion"].lower() or "llm_" in out["suggestion"]


class TestExploreExemption:
    """Test that Explore agents are exempt from suggestions."""

    def test_explore_no_suggestion_on_failure(self, tmp_path):
        """Explore agent failures don't get suggestions."""
        code, out = _run(
            tool_result="Error: Explore agent timed out",
            subagent_type="Explore",
            prompt="find all test files",
            tmp_path=tmp_path,
        )
        assert code == 0
        assert out is None  # No suggestion for Explore

    def test_explore_parse_error_no_suggestion(self, tmp_path):
        """Even with parse error, Explore gets no suggestion."""
        code, out = _run(
            tool_result="Error: Invalid JSON",
            subagent_type="Explore",
            prompt="list files matching *.py",
            tmp_path=tmp_path,
        )
        assert code == 0
        assert out is None  # No suggestion


class TestCallTracking:
    """Test call history tracking and session handling."""

    def test_call_history_logged(self, tmp_path):
        """Agent calls are logged to agent_calls.json."""
        llmr_dir = tmp_path / ".llm-router"
        llmr_dir.mkdir(parents=True, exist_ok=True)

        # Create initial agent_calls.json
        (llmr_dir / "agent_calls.json").write_text(json.dumps({
            "calls": [
                {
                    "timestamp": 1000000000.0,
                    "subagent_type": "general-purpose",
                    "prompt": "first call",
                    "decision": "approved",
                    "session_id": "session-1",
                }
            ],
            "version": 1,
        }))

        # Run hook (it reads the calls but doesn't modify them in this flow)
        code, out = _run(
            tool_result="Error: Agent failed",
            subagent_type="general-purpose",
            prompt="find files",  # This reads from agent_calls.json
            tmp_path=tmp_path,
        )

        # Verify the hook ran (got a suggestion)
        assert code == 0
        assert out is not None
        assert out["last_agent_type"] == "general-purpose"

    def test_last_call_read_correctly(self, tmp_path):
        """Hook reads the last call from agent_calls.json."""
        llmr_dir = tmp_path / ".llm-router"
        llmr_dir.mkdir(parents=True, exist_ok=True)

        # Create agent_calls.json with multiple calls
        (llmr_dir / "agent_calls.json").write_text(json.dumps({
            "calls": [
                {
                    "timestamp": 999999999.0,
                    "subagent_type": "old-type",
                    "prompt": "old prompt",
                    "decision": "approved",
                    "session_id": "session-1",
                },
                {
                    "timestamp": 1000000000.0,
                    "subagent_type": "general-purpose",
                    "prompt": "find latest call",
                    "decision": "approved",
                    "session_id": "session-1",
                },
            ],
            "version": 1,
        }))

        code, out = _run(
            tool_result="Error: timeout",
            tmp_path=tmp_path,
        )

        # Should use the last call (general-purpose, find latest call)
        assert out is not None
        assert out["last_agent_type"] == "general-purpose"

    def test_missing_call_history(self, tmp_path):
        """Hook handles missing agent_calls.json gracefully."""
        # Don't create agent_calls.json
        code, out = _run(
            tool_result="Error: Agent failed",
            tmp_path=tmp_path,
        )

        # Should not crash, just won't have call info
        assert code == 0
        # May or may not output (depends on implementation), but shouldn't crash


class TestEdgeCases:
    """Test edge cases and error conditions."""

    def test_success_no_output(self, tmp_path):
        """Successful agent run (no error) produces no output."""
        code, out = _run(
            tool_result=None,  # Success
            prompt="find files",
            tmp_path=tmp_path,
        )
        assert code == 0
        assert out is None

    def test_non_agent_tool_ignored(self, tmp_path):
        """Non-Agent tools are ignored."""
        payload = json.dumps({
            "hook_event_name": "PostToolUse",
            "tool_name": "Read",  # Not Agent
            "tool_result": "Error: file not found",
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
        assert result.stdout.strip() == ""  # No output for non-Agent

    def test_malformed_hook_input(self, tmp_path):
        """Malformed hook input is ignored gracefully."""
        env = {**os.environ, "HOME": str(tmp_path)}
        result = subprocess.run(
            [sys.executable, str(HOOK_PATH)],
            input="not valid json {{{",
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""  # No crash, no output

    def test_empty_tool_result(self, tmp_path):
        """Empty tool result (not an error) produces no suggestion."""
        code, out = _run(
            tool_result="",  # Empty, not an error
            prompt="find files",
            tmp_path=tmp_path,
        )
        assert code == 0
        assert out is None  # Empty result is not an error
