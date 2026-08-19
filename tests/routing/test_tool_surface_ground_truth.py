"""WP-14 / Q3(c): tier constants must match tools that actually EXIST.

The audit injected a bogus canonical tool name (``llm_query`` -> ``llm_bogus_xyz``)
and found lint clean and 106 tests green. That was recorded as closed by WP-08.
It was not: re-running the injection as a PRECISE single-site mutation on the
``CORE_TOOLS`` binding survives the FULL suite -- pytest exit 0, zero failures.
The earlier "closed" verdict came from a sloppy injection that replaced all
twenty occurrences of the string in the file, and the resulting failures were
collateral damage, not a gate doing its job.

The reason nothing catches it is structural, and worth stating plainly because it
looks like validation:

    registered_tools(slim) -> _TIERS[slim] -> CORE_TOOLS / ROUTING_TOOLS / ...
    unregistered(names)    -> [n for n in names if resolve(n).name not in reg]

``unregistered()`` checks the tier constants against ``_TIERS``, which IS the tier
constants. Rename a tool inside ``CORE_TOOLS`` and the "registered" set contains
the new name too, so the check reports clean. It is a consistency check wearing a
validation check's clothes -- the same shape as ``lint_tool_surface.py``, whose
real job (per its own docstring) is "no emitter may hardcode a tool name into
prose".

These tests break the circle by resolving against INDEPENDENT ground truth: the
``async def llm_*`` implementations in ``src/llm_router/tools/``. A tier may legally
offer a subset of the implemented tools; it may never offer a name nothing
implements.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from llm_router import tool_surface as ts

_TOOLS_DIR = Path(ts.__file__).resolve().parent / "tools"


#: Tool names begin with one of these. The first version of this scan matched
#: only ``llm_*`` and so reported six REAL tools as phantom -- the consolidated
#: tier's ``llm_router_admin``, ``llm_router_status``, the bare ``llm``, and the agent
#: entry points. A ground-truth check whose ground truth is incomplete
#: manufactures failures, which gets it deleted rather than fixed.
_TOOL_PREFIXES = ("llm", "llm_router_")


def _implemented_tool_names() -> frozenset[str]:
    """Every tool coroutine defined under llm_router/tools/, by AST.

    Parsed rather than imported: importing the tool modules pulls in the whole
    server stack, and a test that can only run when the world is healthy is a
    poor guard for the case where it is not.
    """
    names: set[str] = set()
    for path in sorted(_TOOLS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                if node.name.startswith(_TOOL_PREFIXES):
                    names.add(node.name)
    return frozenset(names)


def test_there_are_implemented_tools_to_compare_against():
    """Guards the guard: if the AST scan finds nothing, every assertion below
    passes vacuously and this file becomes decorative."""
    implemented = _implemented_tool_names()
    assert len(implemented) >= 20, f"AST scan found only {len(implemented)}: {sorted(implemented)}"
    assert "llm_query" in implemented


@pytest.mark.parametrize("tier", ["core", "routing", "consolidated"])
def test_every_tier_entry_is_actually_implemented(tier):
    """A tier may offer a SUBSET of implemented tools; never a name that does
    not exist. This is the assertion the bogus-tool-name mutation must fail."""
    implemented = _implemented_tool_names()
    offered = ts.registered_tools(tier)
    assert offered is not None, tier

    phantom = sorted(n for n in offered if n not in implemented)
    assert not phantom, (
        f"tier {tier!r} offers tools nothing implements: {phantom}\n"
        f"implemented: {sorted(implemented)}"
    )


def test_core_tier_still_offers_the_primary_query_tool():
    """CORE is the maximum-savings tier most users land on. Losing the main
    query tool from it is a silent, severe downgrade -- the mutation removed
    llm_query from CORE entirely and nothing noticed."""
    core = ts.registered_tools("core")
    assert core is not None
    assert "llm_query" in core, sorted(core)


def test_known_tools_contains_no_phantom_names():
    """KNOWN_TOOLS feeds resolution for every emitter, so a phantom here is
    routable-looking but uncallable."""
    implemented = _implemented_tool_names()
    # DEPRECATED_TOOLS are deliberately absent implementations (they map to
    # replacements), so exclude them rather than pretend they must exist.
    deprecated = frozenset(ts.DEPRECATED_TOOLS)
    phantom = sorted(
        n for n in ts.KNOWN_TOOLS
        if n not in implemented and n not in deprecated
    )
    assert not phantom, f"KNOWN_TOOLS names nothing implements: {phantom}"
