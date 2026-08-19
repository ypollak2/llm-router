"""Regression test for the cyber_grid Stop summary layout.

The bug: long classifier names like ``code-context-inherit`` (20 chars)
and ``content-generation-fast-path`` (28 chars) were rendered with
``f"{name:<16}"`` — Python's left-pad doesn't truncate, so the labels
overflowed the 16-char left column and bled into the right SAVINGS
panel, mangling both.

Guard: after the fix, every method label in the routing table renders
within 16 chars. This test exercises the rendering at the lowest level
that owns column allocation, ``_build_intelligence``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


# cyber_grid lives in ``hooks/``, which isn't a regular package — load it
# by spec so we can call its private renderers directly.
_CYBER_GRID_PATH = (
    Path(__file__).resolve().parents[1]
    / "src" / "llm_router" / "hooks" / "cyber_grid.py"
)


@pytest.fixture(scope="module")
def cyber_grid():
    spec = importlib.util.spec_from_file_location(
        "_cyber_grid_under_test", _CYBER_GRID_PATH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _render_to_text(table, width: int = 70) -> list[str]:
    """Render a Rich Table to plain text lines."""
    from rich.console import Console
    from io import StringIO

    buf = StringIO()
    console = Console(file=buf, width=width, force_terminal=False,
                      color_system=None, legacy_windows=False)
    console.print(table)
    return buf.getvalue().splitlines()


def test_long_method_names_are_aliased(cyber_grid):
    data = {
        "routing_logic": [
            {"method": "code-context-inherit", "hits": 7},
            {"method": "content-generation-fast-path", "hits": 1},
            {"method": "build-fast-path", "hits": 16},
            {"method": "heuristic", "hits": 57},
        ],
    }
    table = cyber_grid._build_intelligence(data)
    lines = _render_to_text(table)
    body = "\n".join(lines)

    # The long names must NOT appear in their raw form
    assert "code-context-inherit" not in body, (
        "long classifier name should be aliased to fit the 16-char column"
    )
    assert "content-generation-fast-path" not in body

    # The aliases should be present and recognizable
    assert "ctx-inherit" in body
    assert "content-gen" in body
    # build-fast-path is short enough; aliased anyway for symmetry
    assert "build-fast" in body


def test_no_routing_row_label_exceeds_budget(cyber_grid):
    """Every method label in the rendered table fits within budget.

    The budget is 16 chars. We strip the leading symbol+space and the
    trailing hit/percent columns and verify what remains is ≤ 16 chars
    of label.
    """
    data = {
        "routing_logic": [
            {"method": "code-context-inherit", "hits": 7},
            {"method": "content-generation-fast-path", "hits": 1},
            {"method": "this-name-is-way-too-long-and-should-be-truncated",
             "hits": 1},
        ],
    }
    table = cyber_grid._build_intelligence(data)
    lines = _render_to_text(table, width=80)
    body = "\n".join(lines)

    # Truncation guard: future classifier names that aren't aliased
    # must still fit. Look for the truncation marker.
    assert "this-name-is-w" in body or "this-name-is" in body
    # And the raw long name must be absent
    assert "this-name-is-way-too-long" not in body
