"""``query_realized_savings`` — a delegating surface, and it must stay one.

WHY THIS EXISTS
===============

Found by ``scripts/check_downstream_superset.py``: the downstream package had a
dashboard-facing realized-savings surface and this tree did not.
``execution_ledger.get_period_accounting`` was here all along, with exactly one
consumer (``retrospective.py``), so the figure existed but no dashboard could
show it.

INV-COST-004 IS THE WHOLE POINT
===============================

This is the *third* savings number in the codebase. Three hand-rolled savings
queries once reported $73.97, $102.31 and $205.19 for the same day — same
database, same window, three implementations. So the risk being managed here is
not "is the arithmetic right" but "is there arithmetic here at all". There must
not be: every field is copied from the accounting object.

``test_it_computes_nothing_itself`` asserts that structurally, because a test
that only checked the returned numbers would pass just as happily against a
reimplementation that happens to agree today and drifts next quarter.

WHAT ACTUALLY PREVENTS THE SIDE EFFECT
======================================

``execution_ledger`` creates the database and the table as a side effect of
connecting, so reaching it on a fresh machine would materialise a usage.db just
by rendering an empty dashboard panel. Reading a figure must not create the
store it reads from.

The guard that prevents this is ``if not db.exists(): return empty``, which
returns before the ``try`` block is entered at all.

It is NOT the ordering of the ``execution_ledger`` import relative to the
``_table_exists`` probe — the first draft of this file claimed it was, and
running that control produced ZERO failures, because on a missing database the
function has already returned. The import ordering is still worth keeping (it
avoids paying an import on the no-table path) but it is not load-bearing, and
recording it as the control would have left the real guard untested.

CONTROL (re-run if edited)
==========================

* Remove ``if not db.exists(): return empty``:
  ``test_reading_does_not_create_the_database`` FAILS — the file appears.
* Remove the ``except Exception`` around the accounting call:
  ``test_fails_open_on_a_corrupt_database`` FAILS.
* Replace any delegated field with a local computation:
  ``test_it_computes_nothing_itself`` FAILS.
"""

from __future__ import annotations

import inspect
import sqlite3

import pytest

from llm_router.dashboard_data import (
    RealizedSavingsTotals,
    _window_epoch_bounds,
    query_realized_savings,
)


class TestFailOpen:
    def test_missing_database_returns_zeros(self, tmp_path):
        result = query_realized_savings("today", db_path=tmp_path / "nope.db")
        assert isinstance(result, RealizedSavingsTotals)
        assert result.realized_savings_usd == 0.0
        assert result.realized_routes == 0

    def test_reading_does_not_create_the_database(self, tmp_path):
        """Reading a figure must not materialise the store it reads from."""
        db = tmp_path / "nope.db"
        query_realized_savings("today", db_path=db)
        assert not db.exists(), (
            "querying realized savings CREATED the database. execution_ledger "
            "creates the file and table on connect, so the existence check must "
            "run and return before that import is reached."
        )

    def test_database_without_the_ledger_table_returns_zeros(self, tmp_path):
        db = tmp_path / "usage.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE unrelated (id INTEGER)")
        conn.commit()
        conn.close()

        result = query_realized_savings("week", db_path=db)
        assert result.potential_savings_usd == 0.0
        assert result.window == "week"

    def test_fails_open_on_a_corrupt_database(self, tmp_path):
        """A savings panel must not be able to take the dashboard down."""
        db = tmp_path / "usage.db"
        db.write_text("this is not a sqlite file")
        result = query_realized_savings("today", db_path=db)
        assert result.realized_savings_usd == 0.0


class TestDelegation:
    def test_it_computes_nothing_itself(self):
        """INV-COST-004, asserted structurally rather than numerically.

        A value test would pass against a reimplementation that agrees today.
        This one fails the moment someone adds arithmetic, which is the actual
        failure mode — three independent savings queries is how the codebase
        got three different answers for one day.
        """
        src = inspect.getsource(query_realized_savings)
        body = src.split("return RealizedSavingsTotals(")[-1]

        for op in ("+", "-", "*", "/", "sum(", "SUM("):
            assert op not in body, (
                f"query_realized_savings performs {op!r} on a delegated field. "
                f"Every figure must come from get_period_accounting unchanged; "
                f"this is the fourth savings calculation waiting to happen."
            )
        assert "get_period_accounting" in src, (
            "the surface no longer delegates to the accounting function"
        )

    def test_every_field_is_delegated(self):
        """No field may be defaulted or invented once the accounting is in hand."""
        src = inspect.getsource(query_realized_savings)
        final = src.split("return RealizedSavingsTotals(")[-1]
        for field in (
            "potential_savings_usd",
            "realized_savings_usd",
            "net_realized_savings_usd",
            "realized_routes",
            "overridden_routes",
            "realization_unknown_routes",
            "likely_used_routes",
            "cost_unknown_attempts",
        ):
            assert f"{field}=accounting.{field}" in final, (
                f"{field} is not delegated to the accounting object"
            )


class TestWindowBounds:
    @pytest.mark.parametrize("window", ["today", "week", "month", "14d", "lifetime"])
    def test_every_supported_window_resolves(self, window):
        start, end = _window_epoch_bounds(window)
        assert start <= end
        assert end > 0

    def test_lifetime_starts_at_the_epoch(self):
        start, _ = _window_epoch_bounds("lifetime")
        assert start == 0.0

    def test_unknown_window_raises(self):
        """Unlike the query itself, the bounds helper is strict on purpose.

        A typo'd window silently returning "lifetime" would produce a plausible
        number for the wrong period, which is worse than an error.
        """
        with pytest.raises(ValueError, match="unknown window"):
            _window_epoch_bounds("fortnight")  # type: ignore[arg-type]

    def test_windows_match_the_sql_side_names(self):
        """The two window vocabularies must not drift apart."""
        from llm_router.dashboard_data import _window_sql

        for window in ("today", "week", "month", "14d", "lifetime"):
            _window_sql(window)  # raises if the SQL side lost the name
            _window_epoch_bounds(window)
