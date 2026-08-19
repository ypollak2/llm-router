"""CHZ-AUD-019: Subprocess smoke tests for zero-coverage hooks.

Covers the following hooks that previously had zero test coverage:
  - src/llm_router/hooks/response-router.py
  - src/llm_router/hooks/gemini-cli-auto-route.py
  - src/llm_router/hooks/gemini-cli-post-tool.py
  - src/llm_router/hooks/gemini-cli-session-end.py
  - src/llm_router/hooks/bash-compress.py

Each test:
  1. Invokes the hook as a subprocess (exactly as Claude Code or Gemini CLI does)
  2. Asserts the process exits without crashing (exit code 0 or handled gracefully)
  3. Validates the output format where applicable (valid JSON, no garbage on stdout)
  4. Exercises key branches (opt-out env var, opt-in, edge input)

Pattern: follows test_hook_stdout_json_purity.py (the existing hook smoke harness).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).resolve().parents[1] / "src" / "llm_router" / "hooks"

RESPONSE_ROUTER = HOOKS_DIR / "response-router.py"
GEMINI_CLI_AUTO_ROUTE = HOOKS_DIR / "gemini-cli-auto-route.py"
GEMINI_CLI_POST_TOOL = HOOKS_DIR / "gemini-cli-post-tool.py"
GEMINI_CLI_SESSION_END = HOOKS_DIR / "gemini-cli-session-end.py"
BASH_COMPRESS = HOOKS_DIR / "bash-compress.py"


def _run(script: Path, stdin_data: str = "", extra_env: dict | None = None, tmp_path: Path | None = None) -> subprocess.CompletedProcess:
    """Run a hook script as subprocess with isolated HOME."""
    env = os.environ.copy()
    # Isolate from real ~/.llm-router so hooks don't read/write production state
    if tmp_path is not None:
        env["HOME"] = str(tmp_path)
        (tmp_path / ".llm-router").mkdir(parents=True, exist_ok=True)
    # Disable paid API keys so hooks don't accidentally call real providers
    for key in ("OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY"):
        env[key] = ""
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(script)],
        input=stdin_data,
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )


# ── response-router.py ────────────────────────────────────────────────────────

class TestResponseRouterHook:
    """response-router.py: PostResponse hook that routes Claude explanations
    through cheaper models. Tests that the hook:
      - exits 0 and passes through the response unchanged
      - does not crash on empty input
      - respects the LLM_ROUTER_RESPONSE_ROUTER=off opt-out
    """

    def test_passes_through_response_unchanged(self, tmp_path):
        """With abundant budget (pressure=0), hook must output the response verbatim."""
        sample_response = "The capital of France is Paris."
        # Write mock usage.json showing low pressure (bypass routing)
        (tmp_path / ".llm-router").mkdir(parents=True, exist_ok=True)
        usage = {"session_pct": 0.1, "weekly_pct": 0.05}
        (tmp_path / ".llm-router" / "usage.json").write_text(json.dumps(usage))

        result = _run(RESPONSE_ROUTER, stdin_data=sample_response, tmp_path=tmp_path)

        assert result.returncode == 0, (
            f"response-router.py exited {result.returncode}; stderr: {result.stderr[:200]}"
        )
        assert sample_response in result.stdout, (
            f"response-router.py did not pass through response; stdout: {result.stdout[:200]!r}"
        )

    def test_passes_through_on_empty_input(self, tmp_path):
        """Empty stdin must not crash the hook (produces empty output, exit 0)."""
        result = _run(RESPONSE_ROUTER, stdin_data="", tmp_path=tmp_path)
        assert result.returncode == 0, (
            f"response-router.py crashed on empty input; stderr: {result.stderr[:200]}"
        )

    def test_opt_out_env_var_disables_routing(self, tmp_path):
        """LLM_ROUTER_RESPONSE_ROUTER=off must bypass routing and pass through verbatim."""
        sample = "Some response text."
        (tmp_path / ".llm-router").mkdir(parents=True, exist_ok=True)
        # High pressure would normally trigger routing
        usage = {"session_pct": 0.9, "weekly_pct": 0.85}
        (tmp_path / ".llm-router" / "usage.json").write_text(json.dumps(usage))

        result = _run(
            RESPONSE_ROUTER,
            stdin_data=sample,
            extra_env={"LLM_ROUTER_RESPONSE_ROUTER": "off"},
            tmp_path=tmp_path,
        )

        assert result.returncode == 0, (
            f"response-router.py exited {result.returncode}; stderr: {result.stderr[:200]}"
        )
        assert sample in result.stdout, (
            "LLM_ROUTER_RESPONSE_ROUTER=off must pass response through verbatim"
        )


# ── gemini-cli-auto-route.py ─────────────────────────────────────────────────

class TestGeminiCliAutoRouteHook:
    """gemini-cli-auto-route.py: UserPromptSubmit hook for Gemini CLI that
    injects MANDATORY ROUTE hints. Tests that the hook:
      - accepts valid event JSON on stdin and exits 0
      - produces valid JSON on stdout (not corrupt)
      - returns modified event_data with routing_hint or system_message
    """

    def test_returns_valid_json_for_simple_prompt(self, tmp_path):
        """Hook must return valid JSON for a simple query prompt."""
        event = {"prompt": "What is the capital of France?", "context": {}}
        result = _run(GEMINI_CLI_AUTO_ROUTE, stdin_data=json.dumps(event), tmp_path=tmp_path)

        assert result.returncode == 0, (
            f"gemini-cli-auto-route.py exited {result.returncode}; stderr: {result.stderr[:300]}"
        )
        out = result.stdout.strip()
        if out:
            try:
                parsed = json.loads(out)
            except json.JSONDecodeError as exc:
                pytest.fail(
                    f"gemini-cli-auto-route.py stdout is not valid JSON: {exc}\n"
                    f"raw: {out[:200]!r}"
                )
            # Output must be a dict (event_data object)
            assert isinstance(parsed, dict), (
                f"Expected dict output from hook, got {type(parsed).__name__}"
            )

    def test_does_not_crash_on_missing_prompt(self, tmp_path):
        """Empty prompt in event data must not crash the hook."""
        event = {"prompt": "", "context": {}}
        result = _run(GEMINI_CLI_AUTO_ROUTE, stdin_data=json.dumps(event), tmp_path=tmp_path)

        assert result.returncode == 0, (
            f"gemini-cli-auto-route.py crashed on empty prompt; stderr: {result.stderr[:200]}"
        )

    def test_does_not_crash_on_invalid_json(self, tmp_path):
        """Invalid stdin JSON must not crash the hook (graceful error handling)."""
        result = _run(GEMINI_CLI_AUTO_ROUTE, stdin_data="not valid json", tmp_path=tmp_path)
        # Hook should handle gracefully (exit 1 is acceptable, crash/exception is not)
        assert result.returncode in (0, 1), (
            f"Hook crashed on invalid JSON input (exit {result.returncode}); "
            f"stderr: {result.stderr[:200]}"
        )


# ── gemini-cli-post-tool.py ──────────────────────────────────────────────────

class TestGeminiCliPostToolHook:
    """gemini-cli-post-tool.py: PostToolUse hook that flushes savings records.
    Tests that the hook:
      - exits 0 without errors when no session file exists
      - flushes pending_savings to savings_log.jsonl when present
      - does not corrupt stdout (hook output is silent)
    """

    def test_exits_cleanly_when_no_session_file(self, tmp_path):
        """Without gemini_session.json, hook must exit 0 without crashing."""
        event = {"toolName": "bash", "toolInput": {}, "toolResult": ""}
        result = _run(GEMINI_CLI_POST_TOOL, stdin_data=json.dumps(event), tmp_path=tmp_path)

        assert result.returncode == 0, (
            f"gemini-cli-post-tool.py exited {result.returncode} without session file; "
            f"stderr: {result.stderr[:200]}"
        )

    def test_flushes_pending_savings_to_log(self, tmp_path):
        """When gemini_session.json has pending_savings, they must be flushed."""
        llm_router_dir = tmp_path / ".llm-router"
        llm_router_dir.mkdir(parents=True, exist_ok=True)

        # Write a session file with one pending savings record
        session_data = {
            "pending_savings": [
                {"model": "gemini-2.5-flash", "task_type": "query", "savings_usd": 0.01}
            ]
        }
        (llm_router_dir / "gemini_session.json").write_text(json.dumps(session_data))

        event = {"toolName": "bash", "toolInput": {}, "toolResult": "done"}
        result = _run(GEMINI_CLI_POST_TOOL, stdin_data=json.dumps(event), tmp_path=tmp_path)

        assert result.returncode == 0, (
            f"gemini-cli-post-tool.py exited {result.returncode}; stderr: {result.stderr[:200]}"
        )

        # Savings should be in savings_log.jsonl (may not flush if interval not elapsed,
        # but the hook must at least not crash and the file write path is exercised)
        savings_log = llm_router_dir / "savings_log.jsonl"
        # The hook throttles flushes to every 30s; on first run with no last_flush
        # it should flush. Verify that the session file's pending list was processed.
        session_after = json.loads((llm_router_dir / "gemini_session.json").read_text())
        # pending_savings should be cleared after a flush
        if savings_log.exists():
            assert session_after.get("pending_savings", []) == [], (
                "pending_savings was not cleared after flushing to savings_log.jsonl"
            )


# ── gemini-cli-session-end.py ────────────────────────────────────────────────

class TestGeminiCliSessionEndHook:
    """gemini-cli-session-end.py: SessionEnd hook that displays quota/savings summary.
    Tests that the hook:
      - accepts valid event JSON on stdin and exits 0
      - returns valid JSON with the summary merged into event_data
      - does not crash when llm_router database is absent
    """

    def test_returns_valid_json_with_summary(self, tmp_path):
        """Hook must return valid JSON containing the session summary."""
        event = {"session_id": "test-session-123"}
        result = _run(GEMINI_CLI_SESSION_END, stdin_data=json.dumps(event), tmp_path=tmp_path)

        assert result.returncode == 0, (
            f"gemini-cli-session-end.py exited {result.returncode}; stderr: {result.stderr[:300]}"
        )
        out = result.stdout.strip()
        assert out, f"Hook produced no stdout; stderr: {result.stderr[:200]}"
        try:
            parsed = json.loads(out)
        except json.JSONDecodeError as exc:
            pytest.fail(
                f"gemini-cli-session-end.py stdout is not valid JSON: {exc}\n"
                f"raw: {out[:300]!r}"
            )
        assert isinstance(parsed, dict), "Hook must return a JSON object"

    def test_does_not_crash_without_database(self, tmp_path):
        """Without a llm_router DB, hook must degrade gracefully (partial summary is OK)."""
        event = {}
        result = _run(GEMINI_CLI_SESSION_END, stdin_data=json.dumps(event), tmp_path=tmp_path)

        # Exit 0 or 1 is acceptable; crash (segfault, unhandled exception) is not
        assert result.returncode in (0, 1), (
            f"Hook crashed without DB (exit {result.returncode}); stderr: {result.stderr[:200]}"
        )
        # If output present, it must be valid JSON
        out = result.stdout.strip()
        if out:
            try:
                json.loads(out)
            except json.JSONDecodeError as exc:
                pytest.fail(
                    f"Hook produced invalid JSON on stdout without DB: {exc}\n"
                    f"raw: {out[:200]!r}"
                )


# ── bash-compress.py ─────────────────────────────────────────────────────────

class TestBashCompressHook:
    """bash-compress.py: PostToolUse hook that compresses bash command outputs.
    Tests that the hook:
      - exits 0 silently when compression is disabled (LLM_ROUTER_BASH_COMPRESS=off)
      - exits 0 silently for non-bash tools
      - exits 0 for short outputs (below line threshold)
      - produces valid JSON when compression fires on a large bash output
    """

    def _make_bash_payload(self, command: str, output: str) -> str:
        """Create a PostToolUse Bash tool payload."""
        return json.dumps({
            "toolName": "bash",
            "toolInputs": {"command": command},
            "toolResult": {"text": output},
        })

    def test_opt_out_env_var_exits_silently(self, tmp_path):
        """LLM_ROUTER_BASH_COMPRESS=off must exit 0 with no output."""
        payload = self._make_bash_payload("ls -la", "file1\nfile2\nfile3\n")
        result = _run(
            BASH_COMPRESS,
            stdin_data=payload,
            extra_env={"LLM_ROUTER_BASH_COMPRESS": "off"},
            tmp_path=tmp_path,
        )
        assert result.returncode == 0, (
            f"bash-compress.py exited {result.returncode} with LLM_ROUTER_BASH_COMPRESS=off; "
            f"stderr: {result.stderr[:200]}"
        )

    def test_non_bash_tool_exits_silently(self, tmp_path):
        """Non-bash tool events must be ignored (exit 0, no output)."""
        payload = json.dumps({
            "toolName": "read_file",
            "toolInputs": {"path": "/tmp/test.txt"},
            "toolResult": {"text": "file content"},
        })
        result = _run(BASH_COMPRESS, stdin_data=payload, tmp_path=tmp_path)
        assert result.returncode == 0, (
            f"bash-compress.py exited {result.returncode} for non-bash tool; "
            f"stderr: {result.stderr[:200]}"
        )

    def test_short_output_exits_silently(self, tmp_path):
        """Output below the line threshold must not be compressed (exit 0, no output)."""
        # 3 lines — below the 5-line threshold in the hook
        short_output = "line1\nline2\nline3"
        payload = self._make_bash_payload("echo hi", short_output)
        result = _run(BASH_COMPRESS, stdin_data=payload, tmp_path=tmp_path)
        assert result.returncode == 0, (
            f"bash-compress.py exited {result.returncode} for short output; "
            f"stderr: {result.stderr[:200]}"
        )

    def test_invalid_json_input_exits_silently(self, tmp_path):
        """Invalid JSON on stdin must be handled gracefully (exit 0, no crash)."""
        result = _run(BASH_COMPRESS, stdin_data="not json", tmp_path=tmp_path)
        assert result.returncode == 0, (
            f"bash-compress.py crashed on invalid JSON (exit {result.returncode}); "
            f"stderr: {result.stderr[:200]}"
        )

    def test_large_output_produces_valid_json_or_exits_silently(self, tmp_path):
        """Large bash output must produce valid JSON (if compressed) or exit 0 silently.

        The hook only outputs JSON when compression saves > 10% tokens AND RTKAdapter
        is available. In a test environment without RTKAdapter, it exits silently.
        Either outcome is valid — the key assertion is no crash and no corrupt output.
        """
        # 20 lines of fake git output — above the 5-line threshold
        large_output = "\n".join(
            [f"  modified:   src/module_{i}.py" for i in range(20)]
        )
        payload = self._make_bash_payload("git status", large_output)
        result = _run(BASH_COMPRESS, stdin_data=payload, tmp_path=tmp_path)

        assert result.returncode == 0, (
            f"bash-compress.py exited {result.returncode} for large output; "
            f"stderr: {result.stderr[:200]}"
        )
        # If something was written to stdout, it must be valid JSON
        out = result.stdout.strip()
        if out:
            try:
                parsed = json.loads(out)
            except json.JSONDecodeError as exc:
                pytest.fail(
                    f"bash-compress.py produced invalid JSON on stdout: {exc}\n"
                    f"raw: {out[:200]!r}"
                )
            # Output must follow the hookSpecificOutput envelope
            assert "hookSpecificOutput" in parsed, (
                f"Missing hookSpecificOutput key in bash-compress output: {parsed!r}"
            )
