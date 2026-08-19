"""Every surface showing "savings" must delegate to the canonical aggregation.

WHY THIS EXISTS (INV-COST-004)
==============================

execution_ledger.py declares it: "the aggregation functions are the ONLY cost
totals; surfaces delegate." Three surfaces did not, and each showed a different
number for the same day:

    statusline        $102.31    own SQL over savings_stats
    status-bar.py      $73.97    own SQL over usage
    Stop line         $205.19    delegated to dashboard_data.query_window

They were not disagreeing about arithmetic. dashboard_data.py's own docstring
says why: "Every consumer that wants to show today's calls / tokens / savings
must UNION across all sources or under-report." It unions five tables. Measured:

    usage alone             840 rows    $78.68
    savings_stats alone   1,109 rows   $102.88
    query_window (union)  2,215 rows   $205.19   <- the total

Each hand-rolled query read a SUBSET and presented it as the whole. A reader
comparing two surfaces had no way to tell which was right, and the largest was
2.7x the smallest.

WHAT THIS CHECKS, AND WHAT IT CANNOT

It greps shipped surfaces for SQL that sums a savings column directly. That
catches the shape that actually occurred three times — a `SUM(saved_usd)` or
`SUM(estimated_claude_cost_saved)` written inline in a renderer.

It cannot prove a surface delegates CORRECTLY, only that it is not obviously
computing its own. A surface that calls query_window and then mangles the result
passes this. That limit is stated rather than papered over: this is a guard
against the recurrence of a known shape, not a proof of correctness.

dashboard_data.py itself is exempt — it IS the aggregation.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src" / "llm_router"

#: The module that owns the UNION. Exempt because it is the thing others delegate to.
_CANONICAL = {"dashboard_data.py"}

#: Columns that represent money saved. Summing one of these in a renderer is the
#: defect: the value lives in several tables and one table is never the total.
_SAVINGS_COLUMNS = ("saved_usd", "estimated_claude_cost_saved", "cost_saved_usd")

#: Files that render something to a user. Library/aggregation code may query.
_SURFACE_GLOBS = ("hooks/*.sh", "hooks/*.py", "ui/*.py", "tools/dashboard.py")


def _surfaces() -> list[Path]:
    out: list[Path] = []
    for pattern in _SURFACE_GLOBS:
        out.extend(p for p in _SRC.glob(pattern) if p.name not in _CANONICAL)
    return sorted(out)


def test_there_are_surfaces_to_check():
    """Guards the guard: if the globs stop matching, everything below is vacuous."""
    found = _surfaces()
    assert found, (
        f"no surface files matched {_SURFACE_GLOBS} under {_SRC}. Either the "
        f"layout moved and this test needs updating, or it is now checking "
        f"nothing while reporting success."
    )


@pytest.mark.parametrize(
    "surface", _surfaces(), ids=lambda p: f"{p.parent.name}/{p.name}"
)
def test_surface_does_not_sum_savings_itself(surface: Path):
    # FAIL on unreadable, do not skip. G4's ratchet flagged the skip version and
    # was right: a surface this guard cannot read is a surface it is not
    # guarding, and a skip reports that as success. If a file under
    # src/llm_router/hooks/ stops being readable, that is itself the finding.
    try:
        text = surface.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        pytest.fail(
            f"cannot read {surface.relative_to(_ROOT)}: {exc}. This guard "
            f"checks that surfaces do not compute savings themselves; a file it "
            f"cannot read is unchecked, and reporting that as a skip would make "
            f"the guard quietly partial."
        )

    # A GROUP BY query is a BREAKDOWN, not a total, and the invariant is about
    # totals. session-end.py legitimately does `SELECT model, ..., SUM(saved)
    # ... GROUP BY model` for its per-model panel while delegating the headline
    # figure to query_window. Flagging that would have pushed someone to remove
    # a correct query, so the rule is narrowed to statements with no GROUP BY.
    #
    # Cost of the narrowing, stated: a surface could compute a day total via a
    # GROUP BY and sum the rows itself. That is not the shape that occurred
    # three times, and a guard that fails on correct code gets disabled.
    statements = re.split(r";", text)
    offenders = []
    for stmt in statements:
        if re.search(r"\bGROUP\s+BY\b", stmt, re.I):
            continue
        for column in _SAVINGS_COLUMNS:
            if re.search(rf"SUM\s*\(\s*(COALESCE\s*\(\s*)?{re.escape(column)}\b", stmt, re.I):
                offenders.append(column)
    offenders = sorted(set(offenders))

    assert not offenders, (
        f"{surface.relative_to(_ROOT)} sums {offenders} directly.\n"
        f"INV-COST-004: surfaces delegate. Savings live across five tables "
        f"(claude_usage, codex_usage, gemini_usage, usage, savings_stats) and "
        f"any one of them is a SUBSET — this is how three renderers showed "
        f"$73.97, $102.31 and $205.19 for the same day.\n"
        f"Use llm_router.dashboard_data.query_window(window) instead."
    )
