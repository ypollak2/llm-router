"""Money must be measured, modelled, or absent — never ambiguous.

A user read `💰 $0.67` on the statusline as what their session had cost. It was
estimated savings. Investigating that turned up three compounding defects:

  * the figure was unlabelled and sat beside two quota percentages;
  * there was no cost field at all — every surface could say what was SAVED and
    none could say what anything COST, so the reader was hunting a number that
    did not exist;
  * `session_spend.json`, the only genuinely measured money figure on disk, was
    written by the routing path and read by no surface.

These tests hold the repaired contract.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest

from llm_router.dashboard_data import (
    COVERAGE_CALLOUT_PCT,
    SPEND_FLOOR_USD,
    WindowTotals,
    render_money,
    session_spend_usd,
)

REPO = Path(__file__).resolve().parent.parent


def _totals(**kw) -> WindowTotals:
    base = dict(window="today", calls=10, tokens=1000, saved_usd=0.0)
    base.update(kw)
    return WindowTotals(**base)


# ── measured vs modelled ──────────────────────────────────────────────────────


def test_savings_are_marked_as_an_estimate():
    """A tilde and no cents. Precision is itself a claim."""
    line = render_money(_totals(saved_usd=33.936), session_usd=0.0)
    assert "~$34 saved" in line, line
    assert "33.94" not in line, (
        "savings rendered to the cent asserts precision that a counterfactual "
        f"baseline cannot support: {line!r}"
    )


def test_spend_is_exact_because_it_is_measured():
    line = render_money(_totals(saved_usd=0.0), session_usd=1.2345)
    assert "$1.23 spent" in line, line


def test_both_quantities_carry_a_verb():
    """A bare figure beside a quota percentage is read as money spent."""
    line = render_money(_totals(saved_usd=40.0), session_usd=2.0)
    assert "spent" in line and "saved" in line
    # No naked dollar amount without an adjacent verb.
    for amount in re.findall(r"\$[\d,.]+(?:\s+\w+)?", line):
        assert any(v in amount for v in ("spent", "saved")), (
            f"unlabelled money in {line!r}: {amount!r}"
        )


def test_trivial_spend_is_omitted_on_a_subscription_seat():
    """Below a cent, external spend is noise and quota carries the cost story.

    A `$0.00 spent` segment spends pixels to say nothing.
    """
    line = render_money(_totals(saved_usd=30.0), session_usd=0.004)
    assert "spent" not in line, line
    assert "saved" in line


def test_spend_at_the_floor_is_shown():
    line = render_money(_totals(saved_usd=30.0), session_usd=SPEND_FLOOR_USD)
    assert "spent" in line


def test_rounding_scales_with_magnitude():
    """Whole dollars suit $34 and overstate $0.70.

    The first version of this renderer rounded everything to the dollar, which
    turned $0.70 into "~$1 saved" — a 43% exaggeration, in the direction that
    flatters the product. Below $10 the cents are the number.
    """
    assert "~$0.70 saved" in render_money(_totals(saved_usd=0.70), session_usd=0.0)
    assert "~$9.99 saved" in render_money(_totals(saved_usd=9.99), session_usd=0.0)
    assert "~$34 saved" in render_money(_totals(saved_usd=33.94), session_usd=0.0)


def test_amounts_below_a_cent_are_not_claimed():
    """'~$0.00 saved' is noise asserting a win."""
    line = render_money(_totals(saved_usd=0.004), session_usd=0.0)
    assert "saved" not in line, line


def test_nothing_to_report_renders_empty():
    assert render_money(_totals(), session_usd=0.0) == ""


# ── coverage honesty ──────────────────────────────────────────────────────────


def test_soft_estimates_admit_their_coverage():
    """255 of 382 decisions unobserved is not a footnote, it is the number."""
    line = render_money(
        _totals(saved_usd=34.0, observed_n=127, unobserved_n=255), session_usd=0.0
    )
    assert "observed" in line, (
        f"a two-thirds-unobserved estimate rendered with a hard face: {line!r}"
    )
    assert "33% observed" in line


def test_good_coverage_is_not_nagged_about():
    line = render_money(
        _totals(saved_usd=34.0, observed_n=99, unobserved_n=1), session_usd=0.0
    )
    assert "observed" not in line, f"clean coverage should stay quiet: {line!r}"


def test_unreadable_coverage_makes_no_claim():
    line = render_money(
        _totals(saved_usd=34.0, coverage_readable=False), session_usd=0.0
    )
    assert "observed" not in line
    assert "saved" in line


def test_scope_sits_inside_the_phrase():
    """Appended by the caller, 'today' landed after the coverage note and read
    as though the coverage were today's rather than the saving."""
    line = render_money(
        _totals(saved_usd=34.0, observed_n=1, unobserved_n=9), session_usd=0.0
    )
    assert line.index("today") < line.index("observed")


# ── cost is actually computed ─────────────────────────────────────────────────


def test_window_totals_carries_a_cost_field():
    """There was no cost field for a long time; that was the root defect."""
    assert hasattr(WindowTotals, "__dataclass_fields__")
    assert "cost_usd" in WindowTotals.__dataclass_fields__


def test_uncosted_sources_are_named_not_counted_as_free():
    """`0.00` must not stand in for `not measured`.

    The per-platform tables carry `cost_saved_usd`, a savings column. A source
    contributing calls but no cost says so, the same way the quota placeholder
    distinguishes unknown from zero.
    """
    from llm_router.dashboard_data import query_window

    db = Path(pytest.importorskip("tempfile").mkdtemp()) / "usage.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE claude_usage (timestamp TEXT, tokens_used INT, cost_saved_usd REAL)"
    )
    # Store UTC, as the real writers do. The window filter is
    # `date(timestamp,'localtime') = date('now','localtime')`, so a row written
    # in localtime gets converted a SECOND time and lands on the wrong day
    # whenever the doubled offset crosses midnight — which is how this passed
    # all afternoon and failed near a boundary.
    conn.execute("INSERT INTO claude_usage VALUES (datetime('now'), 100, 1.5)")
    conn.commit()
    conn.close()

    totals = query_window("today", db_path=db)
    assert totals.saved_usd == pytest.approx(1.5)
    assert totals.cost_usd == 0.0
    assert "claude_usage" in totals.uncosted_sources, (
        "a table contributing calls but no cost must be named, so a $0.00 total "
        "is never read as 'this was free'"
    )


def test_session_spend_distinguishes_unknown_from_zero(tmp_path):
    assert session_spend_usd(state_dir=tmp_path) is None
    (tmp_path / "session_spend.json").write_text(json.dumps({"total_usd": 0.0}))
    assert session_spend_usd(state_dir=tmp_path) == 0.0


# ── one aggregation, one renderer ─────────────────────────────────────────────


def test_statusline_delegates_both_total_and_format():
    """INV-COST-004 said surfaces delegate the aggregation. It was a comment,
    and the statusline violated its spirit for a year by resolving no
    interpreter and printing nothing at all. Make it a test.

    Delegating the FORMAT matters as much as the total: every surface invented
    its own money format, and the two that mattered disagreed.
    """
    src = (REPO / "src" / "llm_router" / "hooks" / "statusline-command.sh").read_text()

    assert "render_money" in src, (
        "the statusline formats money itself instead of delegating to "
        "render_money(), which is how the surfaces drifted apart"
    )
    assert "query_window" in src, "statusline does not use the canonical aggregation"
    assert not re.search(r"SUM\(\s*cost", src, re.I), (
        "the statusline is running its own cost SQL again — the v9.3 drift class"
    )


def test_coverage_callout_threshold_is_reachable():
    """A threshold nothing can trip is decoration."""
    assert 0 < COVERAGE_CALLOUT_PCT < 100
    assert SPEND_FLOOR_USD > 0
