"""Coordination fast-path tests.

Pins that multi-agent orchestration prompts ("coordinate three agents
to build, test, and deploy") classify as ``task_type=coordinate`` and
that the bucket is ADVISORY-ONLY: direct execution must never fire for
them, in any mode. A stateless direct model has no subagents — a
pre-generated answer would fabricate parallel work that never happened.

False-positive guardrails: an orchestration verb alone ("spawn a
background process") or an agent noun alone ("what is a subagent?")
MUST still route normally — only the verb+target pairing triggers.
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
        "_auto_route_coord_under_test",
        Path(__file__).resolve().parents[1]
        / "src" / "llm_router" / "hooks" / "auto-route.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ── True positives — orchestration prompts classify as coordinate ────────


@pytest.mark.parametrize("prompt", [
    "Coordinate three agents to build, test, and deploy the service",
    "orchestrate a swarm of workers to crawl these endpoints",
    "delegate the refactor to two subagents and merge their diffs",
    "spawn four agents and fan out the migration work",
    "parallelize this across worker agents",
    "dispatch specialists for research, coding, and review",
    "split the work between three sub-agents",
    "divide the tasks among your assistants",
])
def test_coordination_prompts_classify(auto_route, prompt):
    """Every prompt must hit the coordination fast-path."""
    assert auto_route._is_coordination_task(prompt), (
        f"prompt should be flagged coordination: {prompt!r}"
    )
    result = auto_route.classify_prompt(prompt)
    assert result is not None, f"coordination prompt missed classifier: {prompt!r}"
    assert result["task_type"] == "coordinate", (
        f"expected coordinate, got {result['task_type']!r} for {prompt!r}"
    )
    assert result["method"] == "coordination-fast-path"


# ── False positives — single-signal prompts must route normally ──────────


@pytest.mark.parametrize("prompt", [
    # Verb without agent target — process/code semantics
    "spawn a background process to tail the log",
    "parallelize this loop with multiprocessing",
    "dispatch the event to the handler",
    "split the work into two functions",
    # Agent target without orchestration verb — knowledge questions
    "what is a subagent?",
    "explain how AI agents use tools",
    "write a bio for a real estate agent",
    "how many workers does gunicorn need?",
])
def test_non_coordination_prompts_route_normally(auto_route, prompt):
    """Single-signal prompts must NOT trigger the fast-path."""
    assert not auto_route._is_coordination_task(prompt), (
        f"false positive — should NOT be coordination: {prompt!r}"
    )
    result = auto_route.classify_prompt(prompt)
    if result is not None:
        assert result.get("task_type") != "coordinate", (
            f"classifier chain mislabelled as coordinate: {prompt!r}"
        )


# ── Session-state / recent-history / continuation / cross-system / ambient ──
#
# routing-golden-v1 (2026-09-28): the verb+target pair above only catches
# literal "spawn agents"-style prompts. 11 of 12 constructed `coordinate`
# cases in that probe were routed to a local model and answered anyway,
# because none of them mention "agents". These are the prompts from that
# probe's constructed set (holdouts-src/routing-golden-v1/constructed_cases
# .jsonl, `task_class == "coordinate"` — a public, checked-in fixture, not
# the private real-prompt slice) plus a few shapes named directly in the
# defect report ("status?", "keep going", "check the running agent",
# "merge them and redeploy").


@pytest.mark.parametrize("prompt", [
    # Cross-system orchestration (no "agents" mentioned at all)
    "Coordinate a plan across the auth service, the billing service, and "
    "the frontend repo to migrate everyone off the old session token "
    "format, and track progress in a shared doc.",
    # Continuation anaphora — "the same X" + "the rest/other"
    "Continue from where we left off and apply the same fix to the other "
    "three files.",
    "Now do the same thing for the rest of the modules in this repo.",
    # Recent-history reference — "the last N PRs/commits/edits"
    "Merge the changes from the last three PRs I opened and resolve any "
    "conflicts.",
    "Go back to the version from before my last edit and reapply just the "
    "logging change.",
    "Undo my last three commits but keep the README change.",
    # Multi-agent, broadened verbs ("spin up")
    "Spin up two sub-agents, have one write the tests and the other "
    "implement the feature, then reconcile their output.",
    # Session/conversation-state reference
    "Take everything we discussed in this conversation so far and turn it "
    "into a single design doc.",
    "Roll out the config change we agreed on to every environment, one at "
    "a time, and report back after each.",
    "Compare this session's approach with the one from yesterday's "
    "session and pick the better one.",
    # Ambient status / bare continuation checks (defect report examples).
    # "status?" alone (7 chars) is below classify_prompt's universal
    # `len(stripped) < 8` short-prompt floor and returns None before any
    # fast-path runs — that floor is pre-existing, untouched by this fix,
    # and a None classification already means "not routed, nothing held".
    "what's the status?",
    "keep going",
    "check the running agent",
])
def test_broadened_coordination_shapes_classify(auto_route, prompt):
    """These previously fell through to the ordinary category scorers and
    were routed to a local model (the golden-probe defect). They must now
    hit the coordination fast-path exactly like the multi-agent prompts."""
    assert auto_route._is_coordination_task(prompt), (
        f"prompt should be flagged coordination: {prompt!r}"
    )
    result = auto_route.classify_prompt(prompt)
    assert result is not None, f"coordination prompt missed classifier: {prompt!r}"
    assert result["task_type"] == "coordinate", (
        f"expected coordinate, got {result['task_type']!r} for {prompt!r}"
    )
    assert result["method"] == "coordination-fast-path"


# ── Precision guardrails for the new shapes — ordinary prompts must NOT ──
# ── become coordinate just because they share a surface word.           ──


@pytest.mark.parametrize("prompt", [
    # Contains "session"/"conversation" as an ordinary code/data noun, not a
    # reference to THIS conversation's state.
    "Add a session timeout of 30 minutes to the auth middleware.",
    "Write a function that serializes the conversation history to JSON.",
    # Contains "same" + "other" but about a fresh, self-contained task.
    "Write a function with the same signature as `parse_config` for the "
    "other file format, YAML.",
    # Contains "last" + a count, but as a slicing/indexing spec, not history.
    "Return the last three elements of the input list.",
    # A single orchestration verb or agent noun alone — the original
    # false-positive guard, still true for the broadened matcher.
    "Deploy the last commit to staging.",
    "Merge the feature branch into main.",
    # Bare short prompt, but NOT one of the ambient status/continuation
    # phrases — ordinary short questions must still route normally.
    "explain recursion",
    "what's 2+2",
    "list the files here",
])
def test_broadened_shapes_do_not_over_fire(auto_route, prompt):
    """New patterns must not swallow ordinary code/query/edit prompts that
    happen to share a keyword ('session', 'same', 'last', 'merge')."""
    assert not auto_route._is_coordination_task(prompt), (
        f"false positive — should NOT be coordination: {prompt!r}"
    )
    result = auto_route.classify_prompt(prompt)
    if result is not None:
        assert result.get("task_type") != "coordinate", (
            f"classifier chain mislabelled as coordinate: {prompt!r}"
        )


# ── Enum wiring ──────────────────────────────────────────────────────────


def test_tasktype_enum_has_coordinate():
    """The bucket exists in the shared TaskType enum."""
    from llm_router.types import TaskType

    assert TaskType.COORDINATE.value == "coordinate"
    # Text/media/introspect members are untouched.
    assert TaskType.INTROSPECT.value == "introspect"
    assert TaskType.CODE.value == "code"


# ── End-to-end: enforce-route lets native tools through for coordinate ──


def test_enforce_route_skips_coordinate(tmp_path):
    """A pending route with task_type=coordinate must not block Bash, even
    under the strictest enforcement modes — mirrors introspect's exemption
    in enforce-route.py. Before this fix, a coordinate-classified prompt
    still had its tools held until a throwaway llm() routing call was made
    (the live symptom in the defect report: 'status of the post-deploy
    run?' held Bash under enforcement)."""
    import json
    import os
    import subprocess
    import sys
    import time

    enforce_hook = (
        Path(__file__).resolve().parents[1]
        / "src" / "llm_router" / "hooks" / "enforce-route.py"
    )
    session_id = "sess-coordinate"
    router_dir = tmp_path / ".llm-router"
    router_dir.mkdir(parents=True, exist_ok=True)
    (router_dir / f"pending_route_{session_id}.json").write_text(
        json.dumps({
            "expected_tool": "llm_query",
            "task_type": "coordinate",
            "complexity": "moderate",
            "method": "coordination-fast-path",
            "issued_at": time.time(),
            "session_id": session_id,
        })
    )

    env = {k: v for k, v in os.environ.items() if k != "LLM_ROUTER_ENFORCE"}
    env["HOME"] = str(tmp_path)
    env["LLM_ROUTER_HOME"] = str(tmp_path) + "/.llm-router"
    env["LLM_ROUTER_ENFORCE"] = "strict"

    result = subprocess.run(
        [sys.executable, str(enforce_hook)],
        input=json.dumps({
            "session_id": session_id,
            "tool_name": "Bash",
            "tool_input": {"command": "echo status"},
        }),
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "", (
        f"coordinate must skip enforcement; got block payload: {result.stdout!r}"
    )
