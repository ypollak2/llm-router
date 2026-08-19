"""Regression: "today"/period SQL must convert UTC-stored timestamps to LOCAL
before comparing to the local day, or non-UTC users lose the last N hours of
savings/usage near midnight (same bug class as the test_sidecar tz fix).
"""

from __future__ import annotations

import inspect
from pathlib import Path


def test_digest_today_period_uses_localtime():
    from llm_router import digest
    today_sql = digest._PERIOD_SQL["today"]
    assert "localtime" in today_sql, today_sql
    # both sides converted: column AND 'now'
    assert today_sql.count("localtime") >= 2, today_sql


def test_digest_spike_query_uses_localtime():
    from llm_router import digest
    src = inspect.getsource(digest)
    # the daily-spike "today" comparison must be localtime on both sides
    assert "date(timestamp,'localtime') = date('now','localtime')" in src


def _cost_src() -> str:
    return (Path(__file__).resolve().parents[1] / "src" / "llm_router" / "cost.py").read_text()


def test_cost_period_maps_use_localtime():
    src = _cost_src()
    # No bare UTC "today" boundary should remain in the period maps.
    assert '"today": "date(\'now\')"' not in src
    # The localtime today boundary is present (both period maps).
    assert src.count('"today": "date(\'now\',\'localtime\')"') >= 2


def test_cost_where_clauses_convert_column_to_localtime():
    src = _cost_src()
    # The savings/usage period WHERE clauses compare the localtime-converted column.
    assert "date(timestamp,'localtime') >=" in src
    # And the old bare-UTC column comparison is gone from those clauses.
    assert "WHERE date(timestamp) >=" not in src


# ── Dashboard / statusline / cost "today" & daily follow-ups (localtime) ──────
def _read(rel: str) -> str:
    return (Path(__file__).resolve().parents[1] / "src" / "llm_router" / rel).read_text()


def test_dashboards_group_daily_by_localtime():
    for rel in ("tools/dashboard.py", "dashboard/server.py", "dashboard/tui.py"):
        src = _read(rel)
        assert "date(timestamp) as day" not in src, rel          # bare UTC grouping gone
        assert "date(timestamp,'localtime') as day" in src, rel  # local grouping present


def test_today_filters_no_bare_utc_start_of_day():
    # cost.py + dashboards must not gate "today" on a UTC start-of-day boundary.
    for rel in ("cost.py", "dashboard/server.py", "dashboard/tui.py"):
        src = _read(rel)
        assert "datetime('now', 'start of day')" not in src, rel
        assert "datetime('now','start of day')" not in src, rel


def test_statusline_today_savings_use_localtime():
    """"Today" must mean the user's local day, not UTC.

    The statusline no longer carries its own date filter — it delegates to
    dashboard_data.query_window, which under-reported nothing and owns the
    boundary at dashboard_data.py:91:

        "today": "date(timestamp,'localtime')=date('now','localtime')"

    So this asserts the concern MOVED rather than vanished: the script must not
    reintroduce a UTC boundary, and the module it delegates to must still use
    localtime. Asserting the old inline query here would now fail on correct
    code and push someone to re-add the hand-rolled SQL that under-reported by
    2.7x — the defect this replaced.
    """
    sl = _read("hooks/statusline-command.sh")
    assert "date -u +" not in sl, "forced-UTC day boundary reintroduced"
    assert "datetime.datetime.utcnow()" not in sl, "UTC log filter reintroduced"
    assert "query_window" in sl, (
        "statusline no longer delegates — whoever re-added its own savings query "
        "also re-took ownership of the timezone boundary"
    )

    from pathlib import Path as _P
    agg = (_P(__file__).resolve().parents[1]
           / "src" / "llm_router" / "dashboard_data.py").read_text()
    assert "date(timestamp,'localtime')=date('now','localtime')" in agg, (
        "the canonical aggregation stopped using localtime for 'today' — every "
        "surface that delegates now shows a UTC day"
    )
