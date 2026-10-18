"""P3.4-B4b: the session-end hook's remaining table scans read through ``ledger_view``.

Evidence: the B4 PR left eight session-end functions with private ``FROM usage`` /
``FROM routing_decisions`` reads (pinned by name in the lint). Each used its own window and
printed no n. The tests below seed a real database and fail on that code: the figures must come
back with their n and window, an unknown figure must be absent (not 0), and the session filter
must be the session id's rows, not every row since a start time.

F94: the first-forty test's "today" disjunction passed when "today" was absent; the
``session_id`` branch of ``_query_session_data`` had no test.
"""
from __future__ import annotations

import importlib.util
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


def _ts(minutes_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M:%S")


def _load_hook(name: str):
    spec = importlib.util.spec_from_file_location(name, HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(tmp_path / "usage.db"))
    return tmp_path


def _usage_db(home: Path, rows: list[tuple], *, session_id_column: bool = True) -> Path:
    """rows: (minutes_ago, session_id, provider, model, complexity, in_tok, out_tok, cost, cache_hit)"""
    db = home / "usage.db"
    con = sqlite3.connect(db)
    extra = ", session_id TEXT" if session_id_column else ""
    con.execute(
        "CREATE TABLE usage (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, "
        "task_type TEXT, model TEXT, provider TEXT, complexity TEXT, input_tokens INTEGER, "
        "output_tokens INTEGER, cost_usd REAL, success INTEGER DEFAULT 1, cache_hit INTEGER, "
        f"is_simulated INTEGER DEFAULT 0{extra})")
    for ago, sid, provider, model, cx, i, o, cost, hit in rows:
        cols = "timestamp, task_type, model, provider, complexity, input_tokens, output_tokens, cost_usd, cache_hit"
        vals = [_ts(ago), "query", model, provider, cx, i, o, cost, hit]
        if session_id_column:
            cols += ", session_id"
            vals.append(sid)
        con.execute(f"INSERT INTO usage ({cols}) VALUES ({','.join('?' * len(vals))})", vals)
    con.commit()
    con.close()
    return db


def _decisions_db(home: Path, rows: list[dict]) -> Path:
    db = home / "usage.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE IF NOT EXISTS routing_decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "timestamp TEXT NOT NULL, recommended_model TEXT, final_model TEXT, final_provider TEXT, "
        "classifier_latency_ms REAL, latency_ms REAL, cost_usd REAL, complexity TEXT, "
        "was_downshifted INTEGER, reason_code TEXT, provenance TEXT, session_id TEXT)")
    for r in rows:
        d = {"recommended_model": "m1", "final_model": "m1", "final_provider": "ollama",
             "classifier_latency_ms": None, "latency_ms": None, "cost_usd": None,
             "complexity": "simple", "was_downshifted": 0, "reason_code": None,
             "provenance": "runtime", "session_id": "S", **r}
        ago = d.pop("ago", 1)
        keys = ["timestamp", *d]
        con.execute(f"INSERT INTO routing_decisions ({','.join(keys)}) VALUES ({','.join('?' * len(keys))})",
                    [_ts(ago), *d.values()])
    con.commit()
    con.close()
    return db


# ── F94 (a): the labelled "today ~$.. est (n=" fragment, asserted ─────────────────────────

def test_condensed_stop_prints_the_labelled_today_figure_when_the_ledger_has_usage(home):
    """test_first_forty_w2's disjunction passes when "today" is absent; this one cannot."""
    _usage_db(home, [(5, "S", "ollama", "llama-x", "simple", 400_000, 100_000, 0.0, 0),
                     (6, "S", "ollama", "llama-x", "simple", 400_000, 100_000, 0.0, 0)])
    mod = _load_hook("session_end_b4b_today")
    compact = mod._ledger_compact("today")
    assert compact and re.fullmatch(r"~\$[\d,.]+ est \(n=\d+, today\)", compact), compact
    line = mod._condense("  1153 decisions routed\n  5h 16%  weekly 18%\n")
    assert re.search(r"today ~\$[\d,.]+ est \(n=[\d,]+\)", line), line


def test_condensed_stop_leaves_today_out_when_there_is_no_usage_database(home):
    mod = _load_hook("session_end_b4b_nodb")
    assert mod._ledger_compact("today") is None
    assert "today" not in mod._condense("  1153 decisions routed\n  5h 16%  weekly 18%\n")


# ── F94 (b): _query_session_data's session_id branch ──────────────────────────────────────

def _two_sessions(home: Path) -> None:
    _usage_db(home, [
        (30, "A", "openai", "gpt-a1", "moderate", 10, 5, 0.01, 0),
        (20, "A", "ollama", "llama-a2", "simple", 10, 5, 0.0, 0),
        (10, "B", "openai", "gpt-b1", "moderate", 10, 5, 0.02, 0)])


def test_query_session_data_with_a_session_id_returns_only_its_own_rows(home):
    _two_sessions(home)
    mod = _load_hook("session_end_b4b_sid")
    mod._STOP_SESSION_ID = "A"
    paid, cc, free = mod._query_session_data(time.time() - 900)    # start is AFTER A's rows
    assert [r["model"] for r in paid] == ["gpt-a1"] and [r["model"] for r in free] == ["llama-a2"]
    assert cc == []
    mod._STOP_SESSION_ID = "B"
    paid, _cc, free = mod._query_session_data(time.time() - 7200)  # start BEFORE everything
    assert [r["model"] for r in paid] == ["gpt-b1"] and free == []


def test_query_session_data_without_an_id_falls_back_to_the_start_time(home):
    _two_sessions(home)
    mod = _load_hook("session_end_b4b_noid")
    mod._STOP_SESSION_ID = None
    paid, _cc, free = mod._query_session_data(time.time() - 900)   # only B's row is after the start
    assert [r["model"] for r in paid] == ["gpt-b1"] and free == []
    paid, _cc, free = mod._query_session_data(time.time() - 7200)  # all three rows
    assert sorted(r["model"] for r in paid) == ["gpt-a1", "gpt-b1"] and len(free) == 1


def test_query_session_data_with_an_id_but_no_session_column_is_empty_not_every_row(home):
    _usage_db(home, [(10, None, "openai", "gpt-a1", "moderate", 10, 5, 0.01, 0)], session_id_column=False)
    mod = _load_hook("session_end_b4b_nocol")
    mod._STOP_SESSION_ID = "A"
    assert mod._query_session_data(time.time() - 7200) == ([], [], [])
    assert lv.session_usage_rows(lv.session_scope(session_id="A")) is None


def test_complexity_breakdown_uses_the_same_session_rows(home):
    _two_sessions(home)
    mod = _load_hook("session_end_b4b_cx")
    mod._STOP_SESSION_ID = "A"
    by, filtered = mod._query_session_complexity_breakdown(time.time() - 900)
    assert filtered == 0
    assert {k: [(m, n) for m, n, _c, _p in v] for k, v in by.items()} == {
        "moderate": [("gpt-a1", 1)], "simple": [("llama-a2", 1)]}


# ── the migrated figures carry n and window; unknown is absent ────────────────────────────

def test_router_efficiency_counts_today_with_n_and_window(home):
    _decisions_db(home, [{}, {}, {"final_model": "m2"}, {"provenance": "synthetic"},
                         {"reason_code": "cache_hit"}, {"ago": 60 * 30}])      # 30 h ago: not today
    mod = _load_hook("session_end_b4b_eff")
    eff = mod._query_router_efficiency()
    assert (eff["total"], eff["on_target"], eff["window"]) == (3, 2, "today")


def test_router_efficiency_is_empty_when_nothing_was_routed_today(home):
    _decisions_db(home, [{"ago": 60 * 30}])
    assert _load_hook("session_end_b4b_eff0")._query_router_efficiency() == {}


def test_classifier_overhead_averages_only_rows_that_recorded_a_latency(home):
    _decisions_db(home, [{"classifier_latency_ms": 10.0}, {"classifier_latency_ms": 30.0}, {}])
    over = _load_hook("session_end_b4b_ovh")._query_classifier_overhead()
    assert (over["count"], over["avg_ms"], over["window"]) == (2, 20.0, "today")


def test_classifier_overhead_is_unknown_when_no_row_recorded_one(home):
    _decisions_db(home, [{}, {}])
    assert _load_hook("session_end_b4b_ovh0")._query_classifier_overhead() == {}


def test_session_metrics_use_discovered_seats_not_provider_names(home):
    """A Max-seat 'anthropic' call is subscription (cheap), not an escalation to a premium name."""
    (home / "seats.json").write_text(
        __import__("json").dumps(seats_mod.Seats(
            claude=seats_mod.Seat(kind="claude.ai", plan="x"), codex=seats_mod.Seat(),
            gemini=seats_mod.Seat(), api_keys={"OPENAI_API_KEY": True},
            detected_at=datetime.now(timezone.utc).isoformat()).to_dict()))
    _decisions_db(home, [
        {"final_provider": "anthropic", "complexity": "simple", "latency_ms": 2000.0, "cost_usd": 0.0},
        {"final_provider": "openai", "complexity": "simple", "latency_ms": 4000.0, "cost_usd": 0.5},
        {"final_provider": "ollama", "complexity": "complex", "was_downshifted": 1},
        {"final_provider": "ollama", "complexity": None, "latency_ms": 100.0},
        {"final_provider": "openai", "session_id": "OTHER", "cost_usd": 9.0}])
    mod = _load_hook("session_end_b4b_metrics")
    mod._STOP_SESSION_ID = "S"
    m = mod._query_session_metrics(time.time() - 3600)
    assert m["total_decisions"] == 4 and m["window"].startswith("session")
    assert m["escalation_count"] == 1                    # openai on simple work; anthropic is the seat
    assert m["fallback_count"] == 1
    assert m["routing_effectiveness_pct"] == 75.0         # anthropic(seat) + 2 ollama of 4
    assert m["session_cost_usd"] == 0.5 and m["timed_n"] == 2
    assert m["p95_latency"] == {"simple": 4.0}            # unknown complexity is not folded into a tier
    assert "session_cost_ratio" in m and m["session_cost_ratio"] is None   # no earlier days: unknown
    assert mod._session_basis(m) == f"n=4, {m['window']}"


def test_session_metrics_omit_cost_and_burn_when_no_row_recorded_a_cost(home):
    _decisions_db(home, [{}, {}])
    mod = _load_hook("session_end_b4b_nocost")
    mod._STOP_SESSION_ID = "S"
    m = mod._query_session_metrics(time.time() - 3600)
    assert m["total_decisions"] == 2
    assert "session_cost_usd" not in m and "burn_rate_per_hr" not in m
    assert m["p95_latency"] == {}


def test_session_metrics_compare_to_earlier_days(home):
    """30 h ago is never today: one earlier day with 2 calls costing 4.0 in all."""
    _decisions_db(home, [{"cost_usd": 1.0}, {"cost_usd": 1.0, "ago": 60 * 30, "session_id": "old"},
                         {"cost_usd": 3.0, "ago": 60 * 30, "session_id": "old"}])
    mod = _load_hook("session_end_b4b_ratio")
    mod._STOP_SESSION_ID = "S"
    m = mod._query_session_metrics(time.time() - 3600)
    assert m["typical_days"] == 1
    assert m["session_cost_ratio"] == pytest.approx(0.25) and m["session_calls_ratio"] == pytest.approx(0.5)
    assert mod._typical_basis(m) == "(vs 1 earlier day)"


def test_cache_trend_carries_its_rows_and_window(home):
    _usage_db(home, [(5, "S", "ollama", "llama-x", "simple", 1, 1, 0.0, 1),
                     (6, "S", "ollama", "llama-x", "simple", 1, 1, 0.0, 0),
                     (7, "S", "ollama", "llama-x", "simple", 1, 1, 0.0, None)])   # not recorded: out
    mod = _load_hook("session_end_b4b_cache")
    assert mod._query_daily_cache_trend() == [50.0]
    assert mod._CACHE_TREND_BASIS == "n=2 rows, 14d"


def test_model_mix_is_served_runtime_rows_in_the_window_with_staleness(home):
    _decisions_db(home, [{"final_model": "a"}, {"final_model": "a"}, {"final_model": "b"},
                         {"final_model": "c", "provenance": "synthetic"},
                         {"final_model": "old", "ago": 60 * 24 * 20}])
    mix = lv.model_mix(lv.resolve("14d"), limit=2)
    assert mix.rows == (("a", 2), ("b", 1)) and mix.n == 3 and mix.window == "14d"
    assert mix.newest is not None
    today = lv.model_mix(lv.resolve("today"), limit=2)
    assert today.n == 3


def test_new_reads_are_none_without_a_database(home):
    b = lv.resolve("today")
    assert lv.routing_quality(b) is None and lv.classifier_latency(b) is None
    assert lv.session_routing(b, seats=None) is None and lv.cache_trend(b) is None
    assert lv.model_mix(b) is None and lv.session_usage_rows(b) is None


# ── the hook pair ─────────────────────────────────────────────────────────────────────────

def test_hook_pair_is_byte_identical_at_version_42():
    a, b = (ROOT / "hooks" / "session-end.py").read_bytes(), HOOK.read_bytes()
    assert a == b
    assert b.splitlines()[1] == b"# llm_router-hook-version: 42"


def test_the_hook_has_no_private_copy_of_the_decision_predicate():
    text = HOOK.read_text(encoding="utf-8")
    assert "_REAL_DECISION" not in text and "_lifetime_saved" not in text
