"""Regression: the N-day activity chart x-axis must not collide date labels.

The old logic ran the 4-char start label ("23/6") into the mid label ("27") →
"23/627" for a ~9-day chart. Guard start↔mid and drop the mid label when it
can't fit with a gap on each side.
"""

from __future__ import annotations

import datetime
import re

import pytest

from llm_router.ui.session_summary import _date_axis_label_row, _fmt_tok_axis, _tok_axis_unit


def _labels(row: str) -> list[str]:
    """Whitespace-separated date-label tokens on the axis."""
    return [t for t in re.split(r"\s+", row.strip()) if t]


def test_nine_day_axis_no_collision():
    # The exact case from the bug report (today 2026-07-01, 9 days of data).
    row = _date_axis_label_row(9, datetime.date(2026, 7, 1))
    assert "23/627" not in row                 # the old broken output
    tokens = _labels(row)
    # start and end are day/month, clearly separated
    assert tokens[0] == "23/6" and tokens[-1] == "1/7"
    assert all("/" in t or t.isdigit() for t in tokens)


@pytest.mark.parametrize("n", [2, 3, 5, 7, 9, 11, 14, 30])
def test_no_label_ever_collides(n):
    row = _date_axis_label_row(n, datetime.date(2026, 7, 1))
    tokens = _labels(row)
    # Every token is a clean date/day — nothing like "23/627" (a d/m glued to a day).
    for t in tokens:
        assert re.fullmatch(r"\d{1,2}(/\d{1,2})?", t), f"n={n}: mangled token {t!r} in {row!r}"
    # At least the two endpoints are present.
    assert len(tokens) >= 2


def test_wide_chart_shows_midpoint():
    # A full 2-week chart has room for the mid marker.
    tokens = _labels(_date_axis_label_row(14, datetime.date(2026, 7, 1)))
    assert len(tokens) == 3        # start · mid · end


def test_narrow_chart_drops_midpoint_cleanly():
    # A 9-day chart can't fit 3 labels without collision → just the 2 endpoints.
    tokens = _labels(_date_axis_label_row(9, datetime.date(2026, 7, 1)))
    assert len(tokens) == 2


def test_token_axis_uses_one_unit_for_million_scale():
    divisor, suffix = _tok_axis_unit(3_200_000)

    assert suffix == "M"
    assert _fmt_tok_axis(3_200_000, divisor, suffix) == "3.2M"
    assert _fmt_tok_axis(901_300, divisor, suffix) == "0.9M"


def test_token_axis_uses_one_unit_for_thousand_scale():
    divisor, suffix = _tok_axis_unit(950_000)

    assert suffix == "k"
    assert _fmt_tok_axis(950_000, divisor, suffix) == "950.0k"
    assert _fmt_tok_axis(12_500, divisor, suffix) == "12.5k"
