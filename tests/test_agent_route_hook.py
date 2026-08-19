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

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

HOOK_PATH = Path(__file__).parent.parent / "src" / "llm_router" / "hooks" / "agent-route.py"


def _depth_path_for(tmp_path: Path, session_id: str) -> Path:
    """Mirror agent-route.py's _depth_file() exactly: depth state now lives in
    a PER-SESSION file (agent_depth_<session_id>.json), not one shared
    agent_depth.json — two concurrent Claude Code sessions used to read/write
    the same file and could trip each other's circuit breaker. Replicated
    here (rather than importing the hook module) to keep _run() lightweight;
    the sanitization must stay byte-for-byte identical to the hook's own.
    """
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", session_id) or "unknown"
    return tmp_path / ".llm-router" / f"agent_depth_{safe}.json"


def _load_hook_module():
    """Import the hyphenated hook file as a module for white-box unit tests.

    The hook calls _load_dotenv() at import, which mutates os.environ. Snapshot
    and restore it so loading the module for a test doesn't leak ~/.llm-router/.env
    vars (e.g. OLLAMA_BUDGET_MODELS) into env-sensitive tests elsewhere in the
    same pytest process.
    """
    spec = importlib.util.spec_from_file_location("agent_route_hook", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    saved = dict(os.environ)
    try:
        spec.loader.exec_module(mod)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return mod


def _run(
    prompt: str,
    subagent_type: str = "general-purpose",
    session_id: str | None = None,
    agent_depth: int | None = None,
    max_depth: str | None = None,
    tmp_path: Path | None = None,
    subagent_direct: bool = False,
    model_pin: bool = False,
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

    env = os.environ.copy()
    # DIRECT subagent execution makes live model calls — non-deterministic and
    # network-dependent. Classification/depth/budget tests assert the pre-routing
    # decision, so disable it unless a test explicitly opts in.
    env["LLM_ROUTER_SUBAGENT_DIRECT"] = "on" if subagent_direct else "off"
    env["LLM_ROUTER_SUBAGENT_MODEL_PIN"] = "on" if model_pin else "off"
    if tmp_path is not None:
        llmr_dir = tmp_path / ".llm-router"
        llmr_dir.mkdir(parents=True, exist_ok=True)

        # Write the per-session depth file if depth is specified
        if agent_depth is not None and session_id is not None:
            _depth_path_for(tmp_path, session_id).write_text(json.dumps({
                "depth": agent_depth,
                "session_id": session_id,
                "ts": 0,
            }))

        # Write session_id.txt (legacy fallback — only consulted when
        # CLAUDE_CODE_SESSION_ID isn't set in the environment, which it never
        # is in this subprocess test harness)
        if session_id is not None:
            (llmr_dir / "session_id.txt").write_text(session_id)

        env["HOME"] = str(tmp_path)
        env.pop("CLAUDE_CODE_SESSION_ID", None)

    if max_depth is not None:
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
        depth_file = _depth_path_for(tmp_path, "test-session-3")
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
    """Test that different sessions get independent depth counters.

    Depth state now lives in a per-session file, not one shared
    agent_depth.json compared by an embedded session_id field — a new
    session starts fresh simply because its own file doesn't exist yet,
    and (unlike the old shared-file design) an unrelated session's depth
    is never touched at all, not just reset. Two concurrent Claude Code
    sessions used to share one file and could trip each other's circuit
    breaker; this is the direct regression test for that fix.
    """

    def test_new_session_starts_fresh_and_does_not_touch_other_sessions(self, tmp_path):
        """A new session's depth starts at 0 regardless of another
        session's recorded depth, and that other session's file is left
        completely untouched — true isolation, not reset-by-comparison."""
        # Write depth for session-old, in ITS OWN per-session file.
        (tmp_path / ".llm-router").mkdir(parents=True, exist_ok=True)
        _depth_path_for(tmp_path, "session-old").write_text(json.dumps({
            "depth": 5,
            "session_id": "session-old",
            "ts": 0,
        }))

        # Run with session-new — no pre-existing file for it.
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

        # The new session started at 0 and incremented to 1.
        new_depth_file = _depth_path_for(tmp_path, "session-new")
        data = json.loads(new_depth_file.read_text())
        assert data["session_id"] == "session-new"
        assert data["depth"] == 1

        # session-old's file is completely untouched, still depth=5 — this
        # is the isolation guarantee the old shared-file design didn't have.
        old_data = json.loads(_depth_path_for(tmp_path, "session-old").read_text())
        assert old_data["depth"] == 5


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
        depth_file = _depth_path_for(tmp_path, "test-session-6")
        assert depth_file.exists()
        data = json.loads(depth_file.read_text())
        assert data["depth"] == 1


class TestSubagentDirectGating:
    """The DIRECT subagent router must gate cleanly before any network call."""

    def test_disabled_returns_none(self, monkeypatch):
        """LLM_ROUTER_SUBAGENT_DIRECT=off → no execution, no network, returns None."""
        mod = _load_hook_module()
        monkeypatch.setenv("LLM_ROUTER_SUBAGENT_DIRECT", "off")
        assert mod._try_direct_subagent("implement foo", "code", "simple", "s1") is None

    def test_complexity_ceiling_blocks_complex(self, monkeypatch):
        """Tasks above the complexity ceiling are not DIRECT-executed."""
        mod = _load_hook_module()
        monkeypatch.setenv("LLM_ROUTER_SUBAGENT_DIRECT", "on")
        monkeypatch.setenv("LLM_ROUTER_SUBAGENT_DIRECT_MAX_COMPLEXITY", "moderate")
        # complex > moderate → must return None before importing executors
        assert mod._try_direct_subagent("x" * 600, "code", "complex", "s1") is None

    def test_complexity_rank_ordering(self):
        """Ranking is monotonic simple < moderate < complex (ceiling logic)."""
        mod = _load_hook_module()
        r = mod._COMPLEXITY_RANK
        assert r["simple"] < r["moderate"] < r["complex"]


class TestCliDelegationGating:
    """Phase 2 CLI delegation must gate before invoking any external CLI."""

    def test_disabled_returns_none(self, monkeypatch):
        mod = _load_hook_module()
        monkeypatch.setenv("LLM_ROUTER_SUBAGENT_CLI_DELEGATION", "off")
        assert mod._try_cli_delegation("refactor and run tests", "code", "complex", "s1") is None

    def test_non_tool_non_complex_skipped(self, monkeypatch):
        """A simple Q&A task is not delegated — DIRECT already covers it."""
        mod = _load_hook_module()
        monkeypatch.setenv("LLM_ROUTER_SUBAGENT_CLI_DELEGATION", "on")
        # 'what is X' is not a tool task and not complex → no CLI invocation, returns None
        assert mod._try_cli_delegation("what is a closure", "query", "simple", "s1") is None

    def test_env_loaded_into_environ(self):
        """The hook loads ~/.llm-router/.env so OLLAMA_BUDGET_MODELS reaches build_chain."""
        mod = _load_hook_module()
        assert callable(mod._load_dotenv)
        saved = dict(os.environ)
        try:
            mod._load_dotenv()  # no-override re-run must not raise
        finally:
            os.environ.clear()
            os.environ.update(saved)  # don't leak .env into other tests


class TestGovernance:
    """Phase 3 — routed runs become governed agents/ sessions."""

    def test_governance_disabled_is_noop(self, tmp_path, monkeypatch):
        mod = _load_hook_module()
        monkeypatch.setenv("LLM_ROUTER_SUBAGENT_GOVERNANCE", "off")
        monkeypatch.setenv("LLM_ROUTER_SESSIONS_PATH", str(tmp_path / "s.db"))
        mod._govern_run("general-purpose", "ollama", "hermes3:8b", 130, 70, "moderate")
        assert not (tmp_path / "s.db").exists()  # no session written

    def test_governance_records_session(self, tmp_path, monkeypatch):
        """A routed run creates one completed session: cap=baseline, consumed=external."""
        import sqlite3
        mod = _load_hook_module()
        monkeypatch.setenv("LLM_ROUTER_SUBAGENT_GOVERNANCE", "on")
        db = tmp_path / "s.db"
        monkeypatch.setenv("LLM_ROUTER_SESSIONS_PATH", str(db))
        mod._govern_run("general-purpose", "ollama", "hermes3:8b", 130, 70, "moderate")
        rows = sqlite3.connect(str(db)).execute(
            "SELECT agent_id, state, consumed_usd, budget_cap_usd FROM sessions"
        ).fetchall()
        assert len(rows) == 1
        agent_id, state, consumed, cap = rows[0]
        assert agent_id == "subagent:general-purpose"
        assert state == "completed"
        assert consumed == 0.0           # ollama is free
        assert cap > 0                    # Claude-equivalent baseline envelope


class TestModelPin:
    """Phase 4 — lightweight spawned subagents are pinned to a cheaper Claude tier."""

    def test_explore_pinned_to_haiku(self, tmp_path):
        code, out = _run("find all callers of foo()", subagent_type="Explore",
                         tmp_path=tmp_path, model_pin=True)
        assert code == 0
        hso = out["hookSpecificOutput"]
        assert hso["permissionDecision"] == "allow"
        assert hso["updatedInput"]["model"] == "haiku"
        # original input preserved (only model added/overridden)
        assert hso["updatedInput"]["prompt"] == "find all callers of foo()"
        assert hso["updatedInput"]["subagent_type"] == "Explore"

    def test_retrieval_pinned_to_haiku(self, tmp_path):
        code, out = _run("search for every import of requests across the repo",
                         subagent_type="general-purpose", tmp_path=tmp_path, model_pin=True)
        assert code == 0
        assert out["hookSpecificOutput"]["updatedInput"]["model"] == "haiku"

    def test_pin_disabled_is_silent_allow(self, tmp_path):
        """With the pin off, Explore approval is a silent allow (no stdout) as before."""
        code, out = _run("find all callers of foo()", subagent_type="Explore",
                         tmp_path=tmp_path, model_pin=False)
        assert code == 0
        assert out is None


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
        depth_file = _depth_path_for(tmp_path, "test-session-7")
        assert depth_file.exists()
        data = json.loads(depth_file.read_text())
        assert data["depth"] == 1

    def test_missing_session_id_txt_defaults_to_unknown(self, tmp_path):
        """Missing session_id.txt (and no CLAUDE_CODE_SESSION_ID env var)
        defaults to 'unknown'."""
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
        depth_file = _depth_path_for(tmp_path, "unknown")
        assert depth_file.exists()
        data = json.loads(depth_file.read_text())
        assert data["session_id"] == "unknown"

    def test_malformed_agent_depth_json_defaults_to_0(self, tmp_path):
        """Malformed per-session depth file defaults to depth=0."""
        (tmp_path / ".llm-router").mkdir(parents=True, exist_ok=True)
        _depth_path_for(tmp_path, "test-session-8").write_text("not valid json {{{")
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
