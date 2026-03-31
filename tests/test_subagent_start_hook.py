"""Tests for the SubagentStart hook (subagent-start.py)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HOOK_PATH = Path(__file__).parent.parent / "src" / "llm_router" / "hooks" / "subagent-start.py"


def _run(agent_type: str, usage_json: dict | None = None, tmp_path: Path | None = None) -> tuple[int, dict | None]:
    """Run the hook with a given agent_type and optional usage.json content.

    Returns (exit_code, parsed_stdout_or_None).
    """
    payload = json.dumps({"hook_event_name": "SubagentStart", "agent_id": "test-id", "agent_type": agent_type})

    env = None
    if usage_json is not None and tmp_path is not None:
        import os
        llmr_dir = tmp_path / ".llm-router"
        llmr_dir.mkdir(parents=True, exist_ok=True)
        (llmr_dir / "usage.json").write_text(json.dumps(usage_json))
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


class TestExploreSkip:
    def test_explore_exits_cleanly_no_output(self):
        code, out = _run("Explore")
        assert code == 0
        assert out is None

    def test_explore_case_sensitive(self, tmp_path):
        """Only exact 'Explore' is skipped; other retrieval agents get context."""
        code, out = _run("explore", usage_json={"session_pct": 10.0, "sonnet_pct": 10.0, "weekly_pct": 10.0}, tmp_path=tmp_path)
        assert code == 0
        assert out is not None  # lowercase 'explore' is NOT skipped


class TestOutputFormat:
    def test_output_has_correct_structure(self, tmp_path):
        code, out = _run("general", usage_json={"session_pct": 10.0, "sonnet_pct": 10.0, "weekly_pct": 10.0}, tmp_path=tmp_path)
        assert code == 0
        assert out is not None
        assert "hookSpecificOutput" in out
        hs = out["hookSpecificOutput"]
        assert hs["hookEventName"] == "SubagentStart"
        assert "additionalContext" in hs
        assert len(hs["additionalContext"]) > 0

    def test_additional_context_not_context_for_agent(self, tmp_path):
        """Must use additionalContext, not contextForAgent — runAgent.ts reads this field."""
        _, out = _run("general", usage_json={"session_pct": 10.0, "sonnet_pct": 10.0, "weekly_pct": 10.0}, tmp_path=tmp_path)
        assert out is not None
        hs = out["hookSpecificOutput"]
        assert "additionalContext" in hs
        assert "contextForAgent" not in hs

    def test_context_contains_pressure_values(self, tmp_path):
        _, out = _run("general", usage_json={"session_pct": 23.0, "sonnet_pct": 31.0, "weekly_pct": 45.0}, tmp_path=tmp_path)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "23%" in ctx
        assert "31%" in ctx
        assert "45%" in ctx

    def test_context_contains_llm_router_label(self, tmp_path):
        _, out = _run("general", usage_json={"session_pct": 10.0, "sonnet_pct": 10.0, "weekly_pct": 10.0}, tmp_path=tmp_path)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "llm-router" in ctx.lower() or "llm_router" in ctx.lower() or "[llm-router]" in ctx


class TestPressureStatus:
    def test_low_pressure(self, tmp_path):
        _, out = _run("general", usage_json={"session_pct": 20.0, "sonnet_pct": 30.0, "weekly_pct": 40.0}, tmp_path=tmp_path)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "LOW" in ctx

    def test_medium_pressure_session(self, tmp_path):
        _, out = _run("general", usage_json={"session_pct": 65.0, "sonnet_pct": 30.0, "weekly_pct": 40.0}, tmp_path=tmp_path)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "MEDIUM" in ctx

    def test_high_pressure_session(self, tmp_path):
        _, out = _run("general", usage_json={"session_pct": 86.0, "sonnet_pct": 30.0, "weekly_pct": 40.0}, tmp_path=tmp_path)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "HIGH" in ctx

    def test_high_pressure_sonnet(self, tmp_path):
        _, out = _run("general", usage_json={"session_pct": 50.0, "sonnet_pct": 96.0, "weekly_pct": 40.0}, tmp_path=tmp_path)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "HIGH" in ctx

    def test_critical_pressure_weekly(self, tmp_path):
        _, out = _run("general", usage_json={"session_pct": 50.0, "sonnet_pct": 50.0, "weekly_pct": 96.0}, tmp_path=tmp_path)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "CRITICAL" in ctx

    def test_critical_pressure_session(self, tmp_path):
        _, out = _run("general", usage_json={"session_pct": 96.0, "sonnet_pct": 50.0, "weekly_pct": 50.0}, tmp_path=tmp_path)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "CRITICAL" in ctx

    def test_low_pressure_uses_subscription_routing(self, tmp_path):
        _, out = _run("general", usage_json={"session_pct": 10.0, "sonnet_pct": 10.0, "weekly_pct": 10.0}, tmp_path=tmp_path)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "Haiku" in ctx or "haiku" in ctx.lower()
        assert "Opus" in ctx or "opus" in ctx.lower()

    def test_high_pressure_uses_external_routing(self, tmp_path):
        _, out = _run("general", usage_json={"session_pct": 90.0, "sonnet_pct": 50.0, "weekly_pct": 50.0}, tmp_path=tmp_path)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "llm_query" in ctx or "llm_analyze" in ctx or "external" in ctx.lower()


class TestMissingUsageData:
    def test_no_usage_json_exits_cleanly(self, tmp_path):
        """When usage.json is missing, hook runs with 0% pressure (LOW)."""
        import os
        # Point HOME at empty tmp_path (no .llm-router dir)
        env = {**os.environ, "HOME": str(tmp_path)}
        payload = json.dumps({"hook_event_name": "SubagentStart", "agent_id": "x", "agent_type": "general"})
        result = subprocess.run(
            [sys.executable, str(HOOK_PATH)],
            input=payload, capture_output=True, text=True, env=env,
        )
        assert result.returncode == 0
        out = json.loads(result.stdout)
        assert out["hookSpecificOutput"]["hookEventName"] == "SubagentStart"
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "LOW" in ctx  # defaults to 0% → LOW

    def test_malformed_usage_json_exits_cleanly(self, tmp_path):
        import os
        llmr_dir = tmp_path / ".llm-router"
        llmr_dir.mkdir()
        (llmr_dir / "usage.json").write_text("not valid json {{{")
        env = {**os.environ, "HOME": str(tmp_path)}
        payload = json.dumps({"hook_event_name": "SubagentStart", "agent_id": "x", "agent_type": "general"})
        result = subprocess.run(
            [sys.executable, str(HOOK_PATH)],
            input=payload, capture_output=True, text=True, env=env,
        )
        assert result.returncode == 0
        out = json.loads(result.stdout)
        assert out["hookSpecificOutput"]["hookEventName"] == "SubagentStart"

    def test_fractional_pressure_values(self, tmp_path):
        """usage.json can store values as 0–1 fractions instead of 0–100 percentages."""
        _, out = _run("general", usage_json={"session_pct": 0.86, "sonnet_pct": 0.30, "weekly_pct": 0.40}, tmp_path=tmp_path)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "HIGH" in ctx
