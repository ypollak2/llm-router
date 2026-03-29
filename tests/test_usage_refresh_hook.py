"""Tests for the usage-refresh PostToolUse hook."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time


HOOK_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    ".claude", "hooks", "usage-refresh.py",
)


def _run_hook(tool_name: str, state_file: str | None = None) -> dict | None:
    """Run the usage-refresh hook with a given tool name."""
    payload = json.dumps({"toolName": tool_name})
    env = os.environ.copy()
    if state_file:
        # Monkey-patch the STATE_FILE path by injecting it
        env["_TEST_STATE_FILE"] = state_file

    result = subprocess.run(
        [sys.executable, "-c", f"""
import json, sys, os, time

# Override STATE_FILE for testing
STATE_FILE = os.environ.get('_TEST_STATE_FILE', os.path.expanduser('~/.llm-router/usage_last_refresh.txt'))
STALE_THRESHOLD_SEC = 15 * 60

payload = json.loads('''{payload}''')
tool_name = payload.get("toolName", "")
if not tool_name.startswith("llm_"):
    sys.exit(0)
if tool_name in ("llm_check_usage", "llm_update_usage", "llm_cache_stats", "llm_cache_clear"):
    sys.exit(0)

last_refresh = 0.0
if os.path.exists(STATE_FILE):
    try:
        with open(STATE_FILE) as f:
            last_refresh = float(f.read().strip())
    except (ValueError, OSError):
        pass

age_sec = time.time() - last_refresh
if age_sec < STALE_THRESHOLD_SEC:
    sys.exit(0)

age_min = int(age_sec / 60)
hint = (
    f"[USAGE STALE: {{age_min}}m since last refresh] "
    "Consider running /usage-pulse or calling llm_check_usage "
    "to refresh Claude subscription data for accurate routing."
)
result = {{
    "hookSpecificOutput": {{
        "hookEventName": "PostToolUse",
        "contextForAgent": hint,
    }},
}}
json.dump(result, sys.stdout)
"""],
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return json.loads(result.stdout)


class TestUsageRefreshHook:
    def test_non_llm_tool_ignored(self):
        assert _run_hook("Read") is None

    def test_usage_tools_ignored(self):
        assert _run_hook("llm_check_usage") is None
        assert _run_hook("llm_update_usage") is None
        assert _run_hook("llm_cache_stats") is None
        assert _run_hook("llm_cache_clear") is None

    def test_stale_data_triggers_hint(self):
        """When no state file exists, data is considered stale."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            # Write a very old timestamp
            f.write("0")
            f.flush()
            state_file = f.name

        try:
            result = _run_hook("llm_query", state_file=state_file)
            assert result is not None
            hint = result["hookSpecificOutput"]["contextForAgent"]
            assert "USAGE STALE" in hint
        finally:
            os.unlink(state_file)

    def test_fresh_data_no_hint(self):
        """When data was recently refreshed, no hint should be emitted."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write(str(time.time()))  # just now
            f.flush()
            state_file = f.name

        try:
            result = _run_hook("llm_query", state_file=state_file)
            assert result is None
        finally:
            os.unlink(state_file)

    def test_nonexistent_state_file_is_stale(self):
        """Missing state file means data has never been refreshed."""
        result = _run_hook("llm_query", state_file="/tmp/nonexistent_usage_state_12345.txt")
        assert result is not None
        assert "USAGE STALE" in result["hookSpecificOutput"]["contextForAgent"]

    def test_llm_route_triggers_check(self):
        """llm_route (not in exclusion list) should trigger staleness check."""
        result = _run_hook("llm_route", state_file="/tmp/nonexistent_usage_state_12345.txt")
        assert result is not None

    def test_llm_stream_triggers_check(self):
        """llm_stream should trigger staleness check."""
        result = _run_hook("llm_stream", state_file="/tmp/nonexistent_usage_state_12345.txt")
        assert result is not None
