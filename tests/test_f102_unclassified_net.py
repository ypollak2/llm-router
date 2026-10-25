"""F102: the Stop box's Net line keeps unclassified providers out of the paid figure.

Evidence (rv-629): ``_query_session_data`` put unclassified providers (no fresh seat, not local,
not known paid) in the paid list and ``_net_session_line`` summed their recorded cost as "paid",
against ``ledger_view``'s contract ("never folded into paid or subscription"). Hook v44 tags
those rows and prints them as their own fragment.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from llm_router import ledger_view as lv
from llm_router import seats as seats_mod

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "src" / "llm_router" / "hooks" / "session-end.py"
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _hook(name: str):
    spec = importlib.util.spec_from_file_location(name, HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(tmp_path / "usage.db"))
    seats = seats_mod.Seats(
        gemini=seats_mod.Seat(kind="google", plan="x"),
        api_keys={"OPENAI_API_KEY": True},
        detected_at=datetime.now(timezone.utc).isoformat())
    (tmp_path / "seats.json").write_text(json.dumps(seats.to_dict()))
    return tmp_path


def _seed(home: Path, rows: list[tuple[str, float | None]]) -> None:
    con = sqlite3.connect(home / "usage.db")
    con.execute(
        "CREATE TABLE usage (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, "
        "task_type TEXT, model TEXT, provider TEXT, complexity TEXT, input_tokens INTEGER, "
        "output_tokens INTEGER, cost_usd REAL, success INTEGER DEFAULT 1, cache_hit INTEGER, "
        "is_simulated INTEGER DEFAULT 0, session_id TEXT)")
    ts = (datetime.now(timezone.utc) - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S")
    for provider, cost in rows:
        con.execute(
            "INSERT INTO usage (timestamp, task_type, model, provider, complexity, input_tokens, "
            "output_tokens, cost_usd, session_id) VALUES (?, 'query', 'm', ?, 'simple', 1000, 500, ?, 'S')",
            (ts, provider, cost))
    con.commit()
    con.close()


def _net(mod) -> str:
    paid, _cc, free = mod._query_session_data(time.time() - 7200)
    line = mod._net_session_line(free, paid)
    assert line is not None
    return ANSI.sub("", line)


def _mod(name: str):
    mod = _hook(name)
    mod._STOP_SESSION_ID = "S"
    return mod


def test_unclassified_cost_is_not_in_the_paid_figure(home):
    _seed(home, [("ollama", 0.0), ("gemini_cli", 0.0), ("openai", 0.25),
                 ("mystery", 0.75), ("mystery", 0.50)])
    text = _net(_mod("f102_mixed"))
    assert "$0.2500 paid" in text, text          # only the row classed paid
    assert "$1.5000 paid" not in text and "$1.2500 paid" not in text
    assert "n=3 session calls" in text           # local + seat + paid; unclassified is outside n
    assert "unclassified n=2 this session, $1.2500 recorded, not in paid" in text, text


def test_unclassified_rows_are_tagged_and_paid_rows_are_not(home):
    _seed(home, [("openai", 0.25), ("mystery", 0.75)])
    paid, _cc, _free = _mod("f102_tags")._query_session_data(time.time() - 7200)
    tags = {r["provider"]: r.get("_class") for r in paid}
    assert tags == {"openai": None, "mystery": "unclassified"}


def test_unclassified_without_recorded_cost_says_cost_na(home):
    _seed(home, [("ollama", 0.0), ("mystery", None)])
    text = _net(_mod("f102_nocost"))
    assert "unclassified n=1 this session, cost n/a, not in paid" in text, text
    assert "$0.0000 recorded" not in text


def test_no_unclassified_rows_means_no_fragment(home):
    _seed(home, [("ollama", 0.0), ("gemini_cli", 0.0), ("openai", 0.25)])
    text = _net(_mod("f102_none"))
    assert "unclassified" not in text and "$0.2500 paid" in text


def test_all_unclassified_has_no_paid_figure_and_no_net(home):
    _seed(home, [("mystery", 0.75), ("mystery", 0.50)])
    text = _net(_mod("f102_all"))
    assert "Net saved" in text and "n/a" in text, text
    assert " paid" not in text.replace("not in paid", "")
    assert "unclassified n=2 this session, $1.2500 recorded, not in paid" in text


def test_classified_rows_with_unclassified_but_no_paid_prints_no_paid_calls(home):
    """zero paid rows read the same with or without unclassified rows: "no paid calls" (F105)."""
    _seed(home, [("ollama", 0.0), ("mystery", 0.75)])
    text = _net(_mod("f102_paid_na"))
    assert "no paid calls" in text and "$0.0000 paid" not in text, text


def test_the_hook_pair_is_identical_at_current_version():
    top = (ROOT / "hooks" / "session-end.py").read_bytes()
    assert top == HOOK.read_bytes()
    assert top.splitlines()[1] == b"# llm_router-hook-version: 51"


def test_provider_class_docstring_names_the_wall_clock_default():
    assert "omits ``now=``" in (lv.provider_class.__doc__ or "")


# -- F105: the Routing panel keeps unclassified rows out of its figures, and says so -------------

def _routing(mod, rows_spec) -> str:
    paid, _cc, _free = mod._query_session_data(time.time() - 7200)
    paid_only, unc = mod._split_unclassified(paid)
    lines = mod._format_routing_section(mod._aggregate(paid_only), subscription=False, unclassified=unc)
    return ANSI.sub("", "\n".join(lines))


def test_routing_panel_counts_only_paid_rows_and_labels_the_unclassified(home):
    _seed(home, [("openai", 0.25), ("mystery", 0.75), ("mystery", None)])
    text = _routing(_mod("f105_panel"), None)
    assert text.splitlines()[0].lstrip().startswith("1 calls") and "$0.2500 actual" in text, text
    assert "unclassified n=2, $0.7500 recorded (1 of 2); not in the figures above" in text, text


def test_routing_panel_unclassified_without_any_cost_is_na_not_zero(home):
    _seed(home, [("openai", 0.25), ("mystery", None)])
    text = _routing(_mod("f105_na"), None)
    assert "unclassified n=1, cost n/a;" in text and "$0.0000" not in text, text


def test_net_line_partial_costs_show_k_of_n(home):
    _seed(home, [("ollama", 0.0), ("mystery", 0.75), ("mystery", None)])
    text = _net(_mod("f105_partial"))
    assert "$0.7500 recorded (1 of 2)" in text, text


def test_zero_paid_rows_read_the_same_with_and_without_unclassified(home):
    _seed(home, [("ollama", 0.0)])
    assert "no paid calls" in _net(_mod("f105_free_only")) and "$0.0000 paid" not in _net(_mod("f105_free_only2"))


def test_no_unclassified_rows_means_no_routing_note(home):
    _seed(home, [("openai", 0.25)])
    assert "unclassified" not in _routing(_mod("f105_none"), None)


# -- F116: a paid row with no recorded cost is unknown, never $0 ---------------------------------

def test_net_line_paid_row_without_cost_is_k_of_n_not_zero(home):
    _seed(home, [("openai", 0.25), ("openai", None)])
    text = _net(_mod("f116_net_partial"))
    assert "$0.2500 paid recorded (1 of 2 paid calls)" in text, text
    assert "1 paid without cost not counted" in text and "n=1 session calls" in text, text


def test_net_line_all_paid_rows_without_cost_is_na(home):
    _seed(home, [("openai", None), ("openai", None)])
    text = _net(_mod("f116_net_none"))
    assert text.split("Net saved")[1].lstrip().startswith("n/a") and "cost not recorded for 2 paid calls" in text, text
    assert "$0.0000" not in text


def test_routing_panel_paid_row_without_cost_is_k_of_n_and_pct_uses_known_rows(home):
    _seed(home, [("openai", 0.25), ("openai", None)])
    paid, _cc, _free = _mod("f116_panel")._query_session_data(time.time() - 7200)
    mod = _mod("f116_panel2")
    tools = mod._aggregate(paid)
    t = tools["query"]
    assert (t["count"], t["cost_n"], t["in"], t["cost_in"]) == (2, 1, 2000, 1000)
    text = ANSI.sub("", "\n".join(mod._format_routing_section(tools, subscription=False)))
    assert "$0.2500 actual recorded (1 of 2)" in text, text
    assert f"${mod._host_baseline(1000, 500):.4f} baseline (same 1)" in text, text
    assert "1 of 2 costed" in text and "$0.2500 (1 of 2)" in text, text


def test_routing_panel_no_recorded_cost_is_na_and_no_pct(home):
    _seed(home, [("openai", None)])
    paid, _cc, _free = _mod("f116_panel_na")._query_session_data(time.time() - 7200)
    mod = _mod("f116_panel_na2")
    text = ANSI.sub("", "\n".join(mod._format_routing_section(mod._aggregate(paid), subscription=False)))
    assert "actual cost n/a" in text and "baseline n/a" in text and "% saved n/a" in text and "cost n/a" in text, text
    assert "$0.0000" not in text and "100% saved" not in text


def test_routing_panel_with_only_unclassified_rows_still_shows_the_line(home):
    mod = _mod("f116_unc_only")
    unc = [{"provider": "mystery", "cost_usd": None, mod._CLASS_KEY: "unclassified"}]
    text = ANSI.sub("", "\n".join(mod._format_routing_section({}, subscription=False, unclassified=unc)))
    assert "unclassified n=1, cost n/a" in text, text
    assert mod._format_routing_section({}, subscription=False) == []


def test_aggregate_known_zero_cost_is_not_unknown(home):
    mod = _mod("f116_zero")
    t = mod._aggregate([{"task_type": "query", "model": "m", "input_tokens": 10, "output_tokens": 5, "cost_usd": 0.0}])["query"]
    assert t["cost_n"] == 1 and t["cost"] == 0.0


# -- F116 / D-247: the cumulative panel and dashboard never print one merged saved figure --------

def test_split_text_segments_each_with_own_n_and_na_never_merged(home):
    mod = _mod("f116_split")
    assert mod._split_text([2.0, 2, 5.0, 4]) == "$2.00 verified (n=2) · ~$5.00 est. (n=4)"
    assert mod._split_text(None) == "verified n/a · est. n/a"
    assert mod._split_text([2.0, 2, None, None]) == "$2.00 verified (n=2) · est. n/a"
    assert "7.00" not in mod._split_text([2.0, 2, 5.0, 4])


def test_query_cumulative_split_reads_both_sides_from_the_ledger(home):
    _seed(home, [("openai", 0.25)])
    mod = _mod("f116_qsplit")
    split = mod._query_cumulative_split()
    assert set(split) == {"today", "this week", "14 days", "this month", "all time"}
    ver_usd, ver_n, est_usd, est_n = split["all time"]
    assert ver_n == 0 and est_n >= 1 and ver_usd == 0.0     # usage rows are estimated, never verified


def test_dashboard_savings_text_is_split_and_unknown_is_na(home, monkeypatch):
    mod = _mod("f116_dash")
    monkeypatch.setattr(mod, "_ledger_cumulative_split", lambda: {"today": [1.0, 1, 2.0, 3]})
    cumulative = [("today", 4, 100, 0, 3.0), ("all time", 9, 900, 0, 9.0)]
    seen: dict = {}

    class _Stop(Exception):
        pass

    class _Dash:
        def __init__(self, console=None):
            pass

        def print_dashboard(self, **kw):
            seen.update(kw["savings"])
            raise _Stop

    import llm_router.ui.session_summary as ss
    monkeypatch.setattr(ss, "SessionSummaryDashboard", _Dash)
    with pytest.raises(_Stop):
        mod._render_rich_summary(time.time() - 60, [], [], [], {}, None, None, False, cumulative)
    assert seen["today_text"] == "$1.00 verified (n=1) · ~$2.00 est. (n=3)" and seen["today"] == 1.0
    assert seen["lifetime_text"] == "verified n/a · est. n/a" and "lifetime" not in seen   # unknown: absent, not 0
    assert 3.0 not in seen.values() and 9.0 not in seen.values()                      # the merged tuple figure is gone


def test_rich_panel_prints_the_text_not_a_single_figure():
    from io import StringIO

    from rich.console import Console

    from llm_router.ui.session_summary import SessionSummaryDashboard

    buf = StringIO()
    SessionSummaryDashboard(console=Console(file=buf, width=200, force_terminal=False)).print_dashboard(
        timestamp="t", decisions=[], savings={"lifetime_text": "verified n/a · est. n/a", "lifetime_tokens": 5,
                                              "today_text": "$1.00 verified (n=1) · ~$2.00 est. (n=3)"})
    out = buf.getvalue()
    top = out.split("14-DAY")[0]          # the 70-column panel wraps the long text; fragments stay whole
    for frag in ("verified n/a ·", "est. n/a", "$1.00 verified", "(n=1) · ~$2.00 est. (n=3)"):
        assert frag in top, (frag, top)
    assert "$0.00" not in top
