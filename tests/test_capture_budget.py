"""Tool capture must keep the token a later prompt will point at.

N10. `context-capture.py` truncated every tool's input at 200 chars and result at
500, uniformly. Measured 2026-09-15 across 371 stored events: p50 650, p90 710,
max 782 — **29% sat at the ceiling**. What gets cut at a fixed boundary is the
tail, and for a Bash call the tail is the exit code, the failing test name, the
branch. Those are exactly what a continuation prompt refers back to.

Widened only where the OUTPUT is what gets cited. A `Read` of a 2,000-line file
keeps the narrow budget on purpose: its value is the path, which lives in the
input, not the body. Storing the body would spend the context budget on bulk no
prompt ever points at.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / "src/llm_router/hooks/context-capture.py"
spec = importlib.util.spec_from_file_location("_cc", HOOK)
cc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cc)


@pytest.mark.parametrize("tool", ["Bash", "Write", "Edit", "MultiEdit", "NotebookEdit"])
def test_high_signal_tools_keep_more_of_their_output(tool):
    _, out = cc._capture_budget(tool)
    assert out > 700, (
        f"{tool} output is still capped below the ceiling 29% of real events "
        "were already hitting"
    )


@pytest.mark.parametrize("tool", ["Read", "Glob", "Grep", "WebFetch"])
def test_bulk_tools_keep_the_narrow_budget(tool):
    inp, out = cc._capture_budget(tool)
    assert (inp, out) == (200, 500), (
        f"{tool} was widened. Its value is the path in the input, not the body; "
        "storing the body spends context budget on bulk no prompt points at"
    )


def test_a_bash_result_survives_past_the_old_ceiling():
    result = "FAILED tests/test_x.py::test_y\n" * 40 + "exit code: 1"
    kept = cc._stringify(result, cc._capture_budget("Bash")[1])
    assert "exit code: 1" not in kept or len(kept) > 700
    assert len(kept) > 700, (
        f"a Bash result was cut to {len(kept)} chars; the tail of a command is "
        "where its verdict lives"
    )


def test_the_widening_is_bounded():
    for tool in ("Bash", "Write", "Read"):
        inp, out = cc._capture_budget(tool)
        assert inp <= 800 and out <= 2000, (
            f"{tool} budget {inp}/{out} is unbounded growth; the whole payload "
            "competes with a ~37s first-model budget"
        )


def test_an_unknown_tool_gets_the_conservative_budget():
    assert cc._capture_budget("SomeFutureTool") == (200, 500), (
        "a new tool must default to narrow, not wide — widening is a decision "
        "made per tool with a reason"
    )
