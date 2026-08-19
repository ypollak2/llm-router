"""WP-15 / RED8-06 — one task→tool map, one fallback.

Three independently-maintained maps existed, with TWO DIFFERENT fallbacks for an
unrecognised task type:

    hooks/auto-route.py::TOOL_MAP   8 keys, fallback llm_route
    hooks/agent-route.py::_TOOL_MAP 5 keys, fallback llm_analyze   <-- diverges
    service.py inline dict          5 keys, fallback llm_route

The five shared keys agree, so the maps look consistent on inspection. The defect
is in what happens to everything ELSE: an ambiguous prompt that auto-route sends
to `llm_route` (which can pick a tool) is sent by agent-route to `llm_analyze` —
a completion door that cannot run tools. NORTH_STAR names that exact outcome as
its first anti-goal: "enforcing a completion tool on a task that needs to run
tools — a structural dead-end".

Nothing drove one prompt through all three and compared, which is why three maps
could drift apart while every unit test passed.

The canonical map lives in tool_surface.py because that module is deliberately
dependency-free and loadable by path — a hook running under a bare `python3` with
no llm_router on sys.path can still read it, which is why the duplicates existed.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from llm_router import tool_surface as ts

_SRC = Path(ts.__file__).resolve().parent


def _literal_map_in(path: Path, name: str) -> dict | None:
    """Extract a module-level dict literal by name, or None if absent."""
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    try:
                        return ast.literal_eval(node.value)
                    except ValueError:
                        return None
    return None


def test_canonical_map_exists():
    assert isinstance(ts.TASK_TOOL_MAP, dict) and ts.TASK_TOOL_MAP
    assert ts.DEFAULT_TASK_TOOL


def test_the_fallback_can_run_tools():
    """The whole point. A fallback that cannot invoke tools turns an ambiguous
    prompt into a structural dead-end."""
    assert ts.DEFAULT_TASK_TOOL == "llm_route", (
        f"fallback is {ts.DEFAULT_TASK_TOOL!r}; llm_analyze and friends are "
        "completion doors and cannot run tools"
    )


def test_no_hook_keeps_a_private_task_tool_map():
    """Guards re-divergence. A second literal map is how these drifted apart."""
    offenders = []
    for rel, name in (
        ("hooks/auto-route.py", "TOOL_MAP"),
        ("hooks/agent-route.py", "_TOOL_MAP"),
    ):
        p = _SRC / rel
        if not p.exists():
            continue
        if _literal_map_in(p, name) is not None:
            offenders.append(f"{rel}::{name}")
    assert not offenders, (
        "private task->tool map(s) still defined as literals: " + ", ".join(offenders)
    )


@pytest.mark.parametrize("task_type", ["research", "generate", "analyze", "code", "query"])
def test_shared_keys_are_unchanged(task_type):
    """The five keys all three maps already agreed on must not shift while
    consolidating — this change is about the FALLBACK and the missing keys."""
    expected = {
        "research": "llm_research", "generate": "llm_generate",
        "analyze": "llm_analyze", "code": "llm_code", "query": "llm_query",
    }
    assert ts.TASK_TOOL_MAP[task_type] == expected[task_type]


def test_every_mapped_tool_actually_exists():
    """A map entry naming a tool nothing implements is the Q3(c) defect again."""
    implemented = ts.implemented_tools()
    phantom = sorted(
        v for v in set(ts.TASK_TOOL_MAP.values()) | {ts.DEFAULT_TASK_TOOL}
        if v not in implemented
    )
    assert not phantom, f"task map names unimplemented tool(s): {phantom}"
