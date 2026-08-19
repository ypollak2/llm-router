"""Introspection fast-path tests.

Pins that introspection prompts (questions about LOCAL LLM Router / project
state) bypass routing — so the user can reach their own data with
native tools instead of being trapped behind an enforcement block.

False-positive guardrails: generic "show me X" questions about
external knowledge MUST still route. Otherwise the fast-path becomes
a routing-evasion loophole.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def auto_route():
    """Dynamic-import the hook script (not an importable module path)."""
    spec = importlib.util.spec_from_file_location(
        "_auto_route_under_test",
        Path(__file__).resolve().parents[1]
        / "src" / "llm_router" / "hooks" / "auto-route.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ── True positives — introspection prompts skip routing ─────────────────


@pytest.mark.parametrize("prompt", [
    "Show me the distribution of routings you've done for today?",
    "show me my routing decisions",
    "list my recent commits",
    "how many sidecars are in ~/.llm-router?",
    "what's in usage.db",
    "tally today's routes",
    "dump the routing_decisions table",
    "what did I route this session",
    "let me see the enforcement log",
])
def test_introspection_prompts_skip_routing(auto_route, prompt):
    """Every prompt in the list must classify as task_type=introspect.

    Returns a dict (so the route indicator + telemetry stay visible)
    but the type is recognised by enforce-route.py and short-circuits
    enforcement — native tools work without being blocked.
    """
    assert auto_route._is_introspection_task(prompt), (
        f"prompt should be flagged introspection: {prompt!r}"
    )
    result = auto_route.classify_prompt(prompt)
    assert result is not None, f"introspection prompt missed classifier: {prompt!r}"
    assert result.get("task_type") == "introspect", (
        f"expected task_type=introspect, got {result!r}"
    )
    assert result.get("method") == "introspection-fast-path", (
        f"expected introspection-fast-path method, got {result!r}"
    )


def test_path_reference_alone_is_sufficient(auto_route):
    """A direct path reference is a strong enough signal — nobody asks
    the cloud about a path they didn't type."""
    assert auto_route._is_introspection_task(
        "Tell me about ~/.llm-router/usage.db structure"
    )


# ── False-positive guards — generic questions still route ─────────────


@pytest.mark.parametrize("prompt", [
    "Show me a Python example of decorators",
    "What is the capital of France?",
    "Explain how B-trees work in databases",
    "List the top 5 Linux distributions",
    "How many sides does a hexagon have",
    "Show me how to write a unit test in pytest",
])
def test_generic_questions_still_route(auto_route, prompt):
    """Generic 'show me / list / how many' prompts that don't mention
    LOCAL state must still go through normal classification."""
    assert not auto_route._is_introspection_task(prompt), (
        f"generic prompt mis-flagged as introspection: {prompt!r}"
    )
    # classify_prompt should NOT return None for these (they classify
    # into one of the real task types via the heuristic chain).
    result = auto_route.classify_prompt(prompt)
    assert result is None or result.get("method") != "introspection-fast-path"


def test_empty_and_short_prompts_skip_introspection_check(auto_route):
    """Very short prompts return None from classify_prompt regardless;
    introspection logic must not crash on those edge cases."""
    assert auto_route.classify_prompt("") is None
    assert auto_route.classify_prompt("hi") is None


# ── End-to-end: enforce-route lets native tools through for introspect ──


def test_enforce_route_skips_introspect(tmp_path):
    """A pending route with task_type=introspect must not block Bash,
    even under the strictest enforcement modes."""
    import json
    import os
    import subprocess
    import sys
    import time

    enforce_hook = (
        Path(__file__).resolve().parents[1]
        / "src" / "llm_router" / "hooks" / "enforce-route.py"
    )
    session_id = "sess-introspect"
    router_dir = tmp_path / ".llm-router"
    router_dir.mkdir(parents=True, exist_ok=True)
    (router_dir / f"pending_route_{session_id}.json").write_text(
        json.dumps({
            "expected_tool": "llm_query",
            "task_type": "introspect",
            "complexity": "simple",
            "method": "introspection-fast-path",
            "issued_at": time.time(),
            "session_id": session_id,
        })
    )

    env = {k: v for k, v in os.environ.items() if k != "LLM_ROUTER_ENFORCE"}
    env["HOME"] = str(tmp_path)
    # Even strict shouldn't block — introspection by definition needs
    # local tools.
    env["LLM_ROUTER_ENFORCE"] = "strict"

    result = subprocess.run(
        [sys.executable, str(enforce_hook)],
        input=json.dumps({
            "session_id": session_id,
            "tool_name": "Bash",
            "tool_input": {"command": "sqlite3 ~/.llm-router/usage.db '.tables'"},
        }),
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "", (
        f"introspect must skip enforcement; got block payload: {result.stdout!r}"
    )
