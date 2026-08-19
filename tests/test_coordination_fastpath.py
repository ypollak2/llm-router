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


# ── Enum wiring ──────────────────────────────────────────────────────────


def test_tasktype_enum_has_coordinate():
    """The bucket exists in the shared TaskType enum."""
    from llm_router.types import TaskType

    assert TaskType.COORDINATE.value == "coordinate"
    # Text/media/introspect members are untouched.
    assert TaskType.INTROSPECT.value == "introspect"
    assert TaskType.CODE.value == "code"
