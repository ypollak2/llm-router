"""Regression: "today"/period SQL must convert UTC-stored timestamps to LOCAL
before comparing to the local day, or non-UTC users lose the last N hours of
savings/usage near midnight (same bug class as the test_sidecar tz fix).
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ast_assert import (  # noqa: E402
    assert_in_strings,
    string_constants,
)

# R13. These assertions were `"<sql>" in inspect.getsource(module)`, which a
# comment or docstring quoting the SQL satisfies just as well as the SQL does —
# and this file's own module docstring discusses the exact fragments it checks.
# `string_constants` reads the AST, where comments do not exist and docstrings
# are excluded, so a match means the text is in a value the program USES.


def _read(rel: str) -> str:
    """Raw text. Kept ONLY for non-Python files.

    A shell script has no AST, so the A-10 evasion this file's other
    assertions were converted to avoid does not apply: there is no "call site"
    to break while leaving a matching comment behind. Reading the text is the
    honest check for `statusline-command.sh`, and using it for a .py file
    would be the defect coming back.
    """
    path = Path(__file__).resolve().parents[1] / "src" / "llm_router" / rel
    assert not rel.endswith(".py"), (
        f"{rel} is Python — use _strings(), not _read(). Source-text "
        "assertions on Python are what R13 removed."
    )
    return path.read_text(encoding="utf-8")


def _strings(rel: str) -> list[str]:
    path = Path(__file__).resolve().parents[1] / "src" / "llm_router" / rel
    return string_constants(ast.parse(path.read_text(encoding="utf-8")))


def test_digest_today_period_uses_localtime():
    from llm_router import digest
    today_sql = digest._PERIOD_SQL["today"]
    assert "localtime" in today_sql, today_sql
    # both sides converted: column AND 'now'
    assert today_sql.count("localtime") >= 2, today_sql


def test_digest_spike_query_uses_localtime():
    from llm_router import digest

    # the daily-spike "today" comparison must be localtime on both sides
    assert_in_strings(
        digest, "date(timestamp,'localtime') = date('now','localtime')"
    )


def test_cost_period_maps_use_localtime():
    strings = _strings("cost.py")
    # No bare UTC "today" boundary should remain in the period maps. As a
    # STRING check this is the real claim: the old form could previously be
    # "removed" by deleting a comment that mentioned it.
    assert not [s for s in strings if s == "date('now')"], (
        "a bare UTC date('now') boundary is still used as a value"
    )
    # The localtime today boundary is present in both period maps.
    n = sum(1 for s in strings if "date('now','localtime')" in s)
    assert n >= 2, f"only {n} localtime 'today' boundaries in cost.py"


def test_cost_where_clauses_convert_column_to_localtime():
    strings = _strings("cost.py")
    assert any("date(timestamp,'localtime') >=" in s for s in strings), (
        "no period WHERE clause compares the localtime-converted column"
    )
    assert not [s for s in strings if "WHERE date(timestamp) >=" in s], (
        "a bare-UTC column comparison is still used"
    )


# ── Dashboard / statusline / cost "today" & daily follow-ups (localtime) ──────
def test_dashboards_group_daily_by_localtime():
    for rel in ("tools/dashboard.py", "dashboard/server.py", "dashboard/tui.py"):
        strings = _strings(rel)
        assert not [s for s in strings if "date(timestamp) as day" in s], rel
        assert any("date(timestamp,'localtime') as day" in s for s in strings), rel


def test_today_filters_no_bare_utc_start_of_day():
    # cost.py + dashboards must not gate "today" on a UTC start-of-day boundary.
    for rel in ("cost.py", "dashboard/server.py", "dashboard/tui.py"):
        strings = _strings(rel)
        for bad in ("datetime('now', 'start of day')",
                    "datetime('now','start of day')"):
            assert not [s for s in strings if bad in s], f"{rel}: {bad}"


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
