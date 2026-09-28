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
import io
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
    entrypoint: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> tuple[int, dict | None]:
    """Run the agent-route hook with given parameters.

    Args:
        prompt: Agent prompt.
        subagent_type: Agent type (e.g. "Explore", "general-purpose").
        session_id: Session ID to write to agent_depth.json (if agent_depth is set).
        agent_depth: Current nesting depth to write to agent_depth.json.
        max_depth: Value for LLM_ROUTER_MAX_AGENT_DEPTH env var.
        tmp_path: Temp directory for HOME.
        entrypoint: Value for CLAUDE_CODE_ENTRYPOINT. ``None`` (the default)
            POPS it from the subprocess env — the headless guard must treat
            "unset" as interactive, and every pre-existing test in this file
            relies on that default so it stays unaffected by the ambient
            shell's own CLAUDE_CODE_ENTRYPOINT (this repo's own dev sessions
            run with it set to "cli").
        extra_env: Additional env vars to set on the subprocess (e.g. the
            LLM_ROUTER_AGENT_ROUTE_HEADLESS override).

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
    # NS3 Codex sub-agent delegation defaults to ON and, if this machine has a
    # real Codex CLI on PATH, would otherwise make a real subprocess/network call
    # for any suitable (research/analyze/code) prompt run through this subprocess
    # helper. Gating tests for that feature use _load_hook_module() + monkeypatch
    # instead (TestCodexSubagentDelegation), so it is disabled here unconditionally.
    env["LLM_ROUTER_AGENT_ROUTE_CODEX"] = "off"
    if entrypoint is None:
        env.pop("CLAUDE_CODE_ENTRYPOINT", None)
    else:
        env["CLAUDE_CODE_ENTRYPOINT"] = entrypoint
    if extra_env:
        env.update(extra_env)
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
        # M-04: LLM_ROUTER_HOME now takes precedence over HOME, and the
        # autouse isolation fixture exports it. Pin it to the same sandbox
        # or the subprocess writes to the fixture's dir, not this one.
        env["LLM_ROUTER_HOME"] = str(tmp_path) + "/.llm-router"
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


def _agent_calls(tmp_path: Path) -> list[dict]:
    calls_file = tmp_path / ".llm-router" / "agent_calls.json"
    if not calls_file.exists():
        return []
    return json.loads(calls_file.read_text()).get("calls", [])


class TestHeadlessGuard:
    """2026-09-28 incident: a `claude -p ... --output-format json` benchmark
    run was silently routed to `codex exec` by this hook (via the default
    `_allow_routed_spawn()` model-pin path), turning a ~40s task into 832s.

    Signal: CLAUDE_CODE_ENTRYPOINT is "sdk-cli"/"sdk-py" for a headless
    (-p / SDK) session and "cli" for an interactive one — verified against a
    real `claude -p ... --output-format json` run (see the guard note in
    hooks/agent-route.py). The hook's own stdin payload carries no equivalent
    field, so the guard reads the environment, not the payload.
    """

    def test_headless_sdk_cli_skips_with_no_decision(self, tmp_path):
        """A reasoning prompt that would normally be blocked/routed is instead
        approved silently (exit 0, no stdout) when the session is headless."""
        code, out = _run(
            "analyze the codebase for architectural issues",
            subagent_type="general-purpose",
            entrypoint="sdk-cli",
            tmp_path=tmp_path,
        )
        assert code == 0
        assert out is None

    def test_headless_sdk_py_variant_also_skips(self, tmp_path):
        """Any sdk-* entrypoint (not just sdk-cli) counts as headless."""
        code, out = _run(
            "analyze the codebase for architectural issues",
            subagent_type="general-purpose",
            entrypoint="sdk-py",
            tmp_path=tmp_path,
        )
        assert code == 0
        assert out is None

    def test_headless_skip_writes_no_session_state(self, tmp_path):
        """A headless skip takes NO routing decision and touches no session
        state (budget/depth files) — it must be a true no-op, not merely an
        'approve but keep tracking' path, or a benchmark run still pays the
        hook's side effects."""
        _run(
            "analyze the codebase for architectural issues",
            subagent_type="general-purpose",
            session_id="test-headless-1",
            entrypoint="sdk-cli",
            tmp_path=tmp_path,
        )
        assert not (tmp_path / ".llm-router" / "session_budget.json").exists()
        assert not _depth_path_for(tmp_path, "test-headless-1").exists()

    def test_headless_skip_is_logged_with_reason(self, tmp_path):
        """Every skip logs why (repo rule) — the agent_calls ledger records
        the headless decision and the entrypoint value that caused it."""
        _run(
            "analyze the codebase for architectural issues",
            subagent_type="general-purpose",
            entrypoint="sdk-cli",
            tmp_path=tmp_path,
        )
        calls = _agent_calls(tmp_path)
        assert calls, "expected the skip to be logged in agent_calls.json"
        assert "skipped_headless" in calls[-1]["decision"]
        assert "entrypoint=sdk-cli" in calls[-1]["decision"]

    def test_interactive_cli_entrypoint_is_not_skipped(self, tmp_path):
        """entrypoint=cli (an ordinary interactive session) must still hit the
        normal depth-breaker/routing logic — the guard is headless-only."""
        code, out = _run(
            "analyze",
            subagent_type="general-purpose",
            session_id="test-headless-2",
            agent_depth=2,
            max_depth="2",
            entrypoint="cli",
            tmp_path=tmp_path,
        )
        assert out is not None
        assert out["decision"] == "block"
        assert "circuit breaker" in out["reason"].lower()

    def test_unset_entrypoint_is_treated_as_interactive(self, tmp_path):
        """No CLAUDE_CODE_ENTRYPOINT at all (stripped-down environment) must
        default to interactive behaviour, not silently disable routing."""
        code, out = _run(
            "analyze",
            subagent_type="general-purpose",
            session_id="test-headless-3",
            agent_depth=2,
            max_depth="2",
            entrypoint=None,
            tmp_path=tmp_path,
        )
        assert out is not None
        assert out["decision"] == "block"
        assert "circuit breaker" in out["reason"].lower()

    def test_claude_desktop_entrypoint_is_not_skipped(self, tmp_path):
        """The desktop app is interactive (a person is driving it), not a
        script/benchmark — it must not be treated as headless."""
        code, out = _run(
            "analyze",
            subagent_type="general-purpose",
            session_id="test-headless-4",
            agent_depth=2,
            max_depth="2",
            entrypoint="claude-desktop",
            tmp_path=tmp_path,
        )
        assert out is not None
        assert out["decision"] == "block"
        assert "circuit breaker" in out["reason"].lower()

    def test_headless_override_env_restores_routing(self, tmp_path):
        """LLM_ROUTER_AGENT_ROUTE_HEADLESS=on is the opt-in escape hatch: a
        headless session with the override set gets the normal decision
        again instead of a silent skip."""
        code, out = _run(
            "analyze",
            subagent_type="general-purpose",
            session_id="test-headless-5",
            agent_depth=2,
            max_depth="2",
            entrypoint="sdk-cli",
            extra_env={"LLM_ROUTER_AGENT_ROUTE_HEADLESS": "on"},
            tmp_path=tmp_path,
        )
        assert out is not None
        assert out["decision"] == "block"
        assert "circuit breaker" in out["reason"].lower()


def _north_star_rows(tmp_path: Path) -> list[dict]:
    ledger = tmp_path / ".llm-router" / "north_star_units.jsonl"
    if not ledger.exists():
        return []
    return [json.loads(line) for line in ledger.read_text().splitlines() if line.strip()]


class TestCodexSubagentDelegation:
    """NS3 — suitable sub-agent spawns are offered to Codex CLI before Claude
    ever spawns anything for them. Root cause this covers: `_allow_routed_spawn()`
    (default ON) used to return unconditionally before `_try_cli_delegation` —
    the only existing caller of Codex — was ever reached, so Codex saw 0 calls
    across 30 days despite the hook being "on". All tests here monkeypatch
    `llm_router.codex_agent.{is_codex_available,run_codex}` — never a real
    subprocess/network call.
    """

    def _fake_codex_result(self, monkeypatch, content="codex answer", success=True, model="gpt-5.5"):
        from llm_router.codex_agent import CodexResult

        async def _fake_run_codex(prompt, timeout=None, **kwargs):
            return CodexResult(
                content=content, model=model,
                exit_code=0 if success else 1, duration_sec=1.5,
            )

        monkeypatch.setattr("llm_router.codex_agent.is_codex_available", lambda: True)
        monkeypatch.setattr("llm_router.codex_agent.run_codex", _fake_run_codex)

    def test_suitable_spawn_delegates_to_codex(self, tmp_path, monkeypatch):
        """A suitable (analyze) task on a general-purpose subagent, with budget
        and Codex available, is delegated — and recorded as a North Star unit."""
        mod = _load_hook_module()
        monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / ".llm-router"))
        monkeypatch.setenv("LLM_ROUTER_AGENT_ROUTE_CODEX", "on")
        self._fake_codex_result(monkeypatch, content="the root cause is X")

        result = mod._try_codex_subagent_delegation(
            "analyze the auth module for bugs", "analyze", "moderate",
            "general-purpose", "sess-1",
        )
        assert result == "the root cause is X"

        rows = _north_star_rows(tmp_path)
        assert len(rows) == 1
        assert rows[0]["lever"] == "agent_route_codex"
        assert rows[0]["outcome"] == "delegated"
        assert rows[0]["model"] == "gpt-5.5"
        assert rows[0]["subagent_type"] == "general-purpose"
        assert rows[0]["task_type"] == "analyze"

    def test_fork_subagent_type_never_delegated(self, tmp_path, monkeypatch):
        """A 'fork' subagent inherits the caller's live context — never suitable
        for an external, context-free process, regardless of task type."""
        mod = _load_hook_module()
        monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / ".llm-router"))
        monkeypatch.setenv("LLM_ROUTER_AGENT_ROUTE_CODEX", "on")

        called = {"run_codex": False}

        async def _should_not_run(*a, **k):
            called["run_codex"] = True
            from llm_router.codex_agent import CodexResult
            return CodexResult(content="x", model="gpt-5.5", exit_code=0, duration_sec=0.1)

        monkeypatch.setattr("llm_router.codex_agent.is_codex_available", lambda: True)
        monkeypatch.setattr("llm_router.codex_agent.run_codex", _should_not_run)

        result = mod._try_codex_subagent_delegation(
            "analyze what we found so far in this conversation", "analyze",
            "moderate", "fork", "sess-2",
        )
        assert result is None
        assert called["run_codex"] is False

        rows = _north_star_rows(tmp_path)
        assert len(rows) == 1
        assert rows[0]["outcome"] == "unsuitable"
        assert rows[0]["subagent_type"] == "fork"

    def test_write_heavy_multi_file_prompt_never_delegated(self, tmp_path, monkeypatch):
        """A write-heavy, multi-file edit is not a Codex-suitable spawn."""
        mod = _load_hook_module()
        monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / ".llm-router"))
        monkeypatch.setenv("LLM_ROUTER_AGENT_ROUTE_CODEX", "on")
        self._fake_codex_result(monkeypatch)

        result = mod._try_codex_subagent_delegation(
            "refactor the entire codebase across multiple files to use the new API",
            "code", "complex", "general-purpose", "sess-3",
        )
        assert result is None
        rows = _north_star_rows(tmp_path)
        assert rows[0]["outcome"] == "unsuitable"

    def test_non_reasoning_task_type_never_delegated(self, tmp_path, monkeypatch):
        """`generate` (writing new content) is outside this lever's suitable
        set (research/analyze/code/query — see test_query_task_type_now_
        delegated below for why `query` moved IN: real production spawns
        classify read-mostly general-purpose delegation that way, and it is
        no more context-dependent than research/analyze/code)."""
        mod = _load_hook_module()
        monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / ".llm-router"))
        monkeypatch.setenv("LLM_ROUTER_AGENT_ROUTE_CODEX", "on")
        self._fake_codex_result(monkeypatch)

        result = mod._try_codex_subagent_delegation(
            "write a short poem about closures", "generate", "simple",
            "general-purpose", "sess-4",
        )
        assert result is None
        rows = _north_star_rows(tmp_path)
        assert rows[0]["outcome"] == "unsuitable"

    def test_query_complex_general_purpose_spawn_now_delegated(self, tmp_path, monkeypatch):
        """Real-shaped production case (~/.llm-router/north_star_units.jsonl,
        2026-09-28): a general-purpose sub-agent classified task_type=query,
        complexity=complex hit "unsuitable" here, so real traffic never
        reached Codex even though the lever was "on". A query is a read-mostly
        question, not a multi-file edit, and does not inherently need the
        parent's live conversational context — it is now Codex-suitable."""
        mod = _load_hook_module()
        monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / ".llm-router"))
        monkeypatch.setenv("LLM_ROUTER_AGENT_ROUTE_CODEX", "on")
        self._fake_codex_result(monkeypatch, content="the answer is X")

        result = mod._try_codex_subagent_delegation(
            "what does this function do and is it thread-safe", "query",
            "complex", "general-purpose", "sess-9",
        )
        assert result == "the answer is X"

        rows = _north_star_rows(tmp_path)
        assert rows[0]["outcome"] == "delegated"
        assert rows[0]["task_type"] == "query"
        assert rows[0]["subagent_type"] == "general-purpose"

    def test_fork_subagent_type_still_excluded_for_query(self, tmp_path, monkeypatch):
        """Widening task_type eligibility to include `query` must not undo
        the `fork` exclusion — a fork inherits the caller's live context
        regardless of task type."""
        mod = _load_hook_module()
        monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / ".llm-router"))
        monkeypatch.setenv("LLM_ROUTER_AGENT_ROUTE_CODEX", "on")
        self._fake_codex_result(monkeypatch)

        result = mod._try_codex_subagent_delegation(
            "what did we conclude earlier in this conversation", "query",
            "simple", "fork", "sess-10",
        )
        assert result is None
        rows = _north_star_rows(tmp_path)
        assert rows[0]["outcome"] == "unsuitable"
        assert rows[0]["subagent_type"] == "fork"

    def test_write_heavy_query_prompt_still_excluded(self, tmp_path, monkeypatch):
        """Widening task_type eligibility to include `query` must not undo
        the write-heavy/multi-file-edit exclusion — the prompt content, not
        just the task_type, decides that."""
        mod = _load_hook_module()
        monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / ".llm-router"))
        monkeypatch.setenv("LLM_ROUTER_AGENT_ROUTE_CODEX", "on")
        self._fake_codex_result(monkeypatch)

        result = mod._try_codex_subagent_delegation(
            "refactor the entire codebase across multiple files and tell me "
            "what changed", "query", "complex", "general-purpose", "sess-11",
        )
        assert result is None
        rows = _north_star_rows(tmp_path)
        assert rows[0]["outcome"] == "unsuitable"

    def test_budget_exhausted_falls_back_with_logged_reason(self, tmp_path, monkeypatch):
        """When today's Codex sub-agent budget is spent, delegation is skipped
        and the fallback reason is recorded — Codex is never even probed."""
        mod = _load_hook_module()
        home = tmp_path / ".llm-router"
        monkeypatch.setenv("LLM_ROUTER_HOME", str(home))
        monkeypatch.setenv("LLM_ROUTER_AGENT_ROUTE_CODEX", "on")
        monkeypatch.setenv("LLM_ROUTER_AGENT_ROUTE_CODEX_DAILY_BUDGET", "1")

        probed = {"is_available": False}

        def _should_not_probe():
            probed["is_available"] = True
            return True

        monkeypatch.setattr("llm_router.codex_agent.is_codex_available", _should_not_probe)

        # Pre-spend the (budget=1) daily allowance for today.
        home.mkdir(parents=True, exist_ok=True)
        today = mod.datetime.now(mod.timezone.utc).date().isoformat()
        (home / "agent_route_codex_budget.json").write_text(
            json.dumps({"date": today, "count": 1})
        )

        result = mod._try_codex_subagent_delegation(
            "research the current best practice for X", "research", "moderate",
            "general-purpose", "sess-5",
        )
        assert result is None
        assert probed["is_available"] is False  # budget gate short-circuits before the probe

        rows = _north_star_rows(tmp_path)
        assert len(rows) == 1
        assert rows[0]["outcome"] == "budget_exhausted"
        assert "budget" in rows[0]["reason"].lower()

    def test_codex_failure_falls_back(self, tmp_path, monkeypatch):
        """A failed Codex run (non-zero exit / empty output) falls back cleanly
        and the failure is recorded, not silently swallowed."""
        mod = _load_hook_module()
        monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / ".llm-router"))
        monkeypatch.setenv("LLM_ROUTER_AGENT_ROUTE_CODEX", "on")
        self._fake_codex_result(monkeypatch, content="codex: empty completion", success=False)

        result = mod._try_codex_subagent_delegation(
            "research recent papers on X", "research", "moderate",
            "general-purpose", "sess-6",
        )
        assert result is None
        rows = _north_star_rows(tmp_path)
        assert rows[0]["outcome"] == "codex_failed"

    def test_disabled_kill_switch_returns_none_without_ledger_row(self, tmp_path, monkeypatch):
        """LLM_ROUTER_AGENT_ROUTE_CODEX=off — fully disabled, nothing attempted,
        nothing logged (mirrors the existing DIRECT/CLI-delegation kill switches)."""
        mod = _load_hook_module()
        monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / ".llm-router"))
        monkeypatch.setenv("LLM_ROUTER_AGENT_ROUTE_CODEX", "off")

        result = mod._try_codex_subagent_delegation(
            "analyze the auth module", "analyze", "moderate",
            "general-purpose", "sess-7",
        )
        assert result is None
        assert _north_star_rows(tmp_path) == []

    def test_ledger_row_is_append_only_jsonl(self, tmp_path, monkeypatch):
        """Two decisions in a row both land as separate JSON lines (not one
        overwritten record) — the append-only shape NS1 depends on."""
        mod = _load_hook_module()
        monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / ".llm-router"))
        monkeypatch.setenv("LLM_ROUTER_AGENT_ROUTE_CODEX", "on")
        self._fake_codex_result(monkeypatch)

        mod._try_codex_subagent_delegation(
            "analyze module A", "analyze", "moderate", "general-purpose", "sess-8")
        mod._try_codex_subagent_delegation(
            "write a short poem about module A", "generate", "simple",
            "general-purpose", "sess-8")

        rows = _north_star_rows(tmp_path)
        assert len(rows) == 2
        assert rows[0]["outcome"] == "delegated"
        assert rows[1]["outcome"] == "unsuitable"


class TestAgentRouteEndToEnd:
    """In-process (not subprocess) test proving main() actually reaches the
    NS3 Codex branch and returns its output as the subagent's block reason
    with the NS1 attribution marker — the wiring, not just the gating logic."""

    def test_main_uses_codex_result_as_subagent_output(self, tmp_path, monkeypatch, capsys):
        mod = _load_hook_module()
        (tmp_path / ".llm-router").mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / ".llm-router"))
        monkeypatch.setenv("LLM_ROUTER_AGENT_ROUTE_CODEX", "on")
        monkeypatch.setenv("LLM_ROUTER_SUBAGENT_DIRECT", "off")
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

        monkeypatch.setattr(
            mod, "_try_codex_subagent_delegation",
            lambda *a, **k: "codex says: fixed at line 42",
        )

        payload = json.dumps({
            "hook_event_name": "PreToolUse",
            "tool_name": "Agent",
            "tool_input": {
                "prompt": "analyze this bug and explain the root cause",
                "subagent_type": "general-purpose",
            },
        })
        monkeypatch.setattr(sys, "stdin", io.StringIO(payload))

        mod.main()

        out = json.loads(capsys.readouterr().out)
        assert out["decision"] == "block"
        assert "codex says: fixed at line 42" in out["reason"]
        assert "agent_route_codex" in out["reason"]
        assert "[NS1]" in out["reason"]
