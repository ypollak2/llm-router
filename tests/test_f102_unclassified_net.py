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
    assert top.splitlines()[1] == b"# llm_router-hook-version: 48"


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
