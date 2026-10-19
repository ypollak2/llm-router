"""F99: the follow-ups of the P3.4-B4b review (rv-621), each pinned by a test that fails on v41.

Evidence (rv-621 non-blocking list): (1) ``provider_class`` took any present seat as a
subscription and ``load_seats`` has no age check, so a stale ``seats.json`` could class a lapsed
seat as cheap; (2) the Stop box's Escalated / Effective silently left unclassified rows out;
(3) the hook still held ``_FREE_PROVIDERS`` and friends, which drive the free / paid / cc split
(D-205: seats and providers are discovered, not listed); (4) ``_query_cache_hit_stats`` summed
``semantic_cache_lookups.saved_usd`` outside ``ledger_view``; (5) the hook pair moved to v42.
"""
from __future__ import annotations

import ast
import importlib.util
import io
import json
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from rich.console import Console

from llm_router import ledger_view as lv
from llm_router import seats as seats_mod
from llm_router.ui.session_summary import SessionSummaryDashboard

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "src" / "llm_router" / "hooks" / "session-end.py"
HOOK_TOP = ROOT / "hooks" / "session-end.py"
NOW = 1_790_000_000.0
DAY = 86400.0


def _load_hook(name: str):
    spec = importlib.util.spec_from_file_location(name, HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ts(minutes_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M:%S")


def _seats(*, claude=None, codex=None, gemini=None, keys=None, detected: float = NOW) -> seats_mod.Seats:
    def seat(kind):
        return seats_mod.Seat(kind=kind, plan="x") if kind else seats_mod.Seat()
    return seats_mod.Seats(claude=seat(claude), codex=seat(codex), gemini=seat(gemini),
                           api_keys=dict(keys or {}),
                           detected_at=datetime.fromtimestamp(detected, timezone.utc).isoformat())


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A scratch HOME-equivalent: the router state dir, the database and the seats file live here."""
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(tmp_path / "usage.db"))
    return tmp_path


def _write_seats(home: Path, seats: seats_mod.Seats) -> None:
    (home / "seats.json").write_text(json.dumps(seats.to_dict()))


def _real_now_seats(age_s: float, **kw) -> seats_mod.Seats:
    return _seats(detected=time.time() - age_s, **kw)


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
        keys = ["timestamp", *d]
        con.execute(f"INSERT INTO routing_decisions ({','.join(keys)}) VALUES ({','.join('?' * len(keys))})",
                    [_ts(1), *d.values()])
    con.commit()
    con.close()
    return db


def _usage_db(home: Path, providers: list[tuple[str, str]]) -> Path:
    db = home / "usage.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE usage (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, "
        "task_type TEXT, model TEXT, provider TEXT, complexity TEXT, input_tokens INTEGER, "
        "output_tokens INTEGER, cost_usd REAL, success INTEGER DEFAULT 1, cache_hit INTEGER, "
        "is_simulated INTEGER DEFAULT 0, session_id TEXT)")
    for provider, model in providers:
        con.execute(
            "INSERT INTO usage (timestamp, task_type, model, provider, complexity, input_tokens, "
            "output_tokens, cost_usd, session_id) VALUES (?, 'query', ?, ?, 'simple', 10, 5, 0.0, 'S')",
            (_ts(2), model, provider))
    con.commit()
    con.close()
    return db


# ── 1. seat staleness ─────────────────────────────────────────────────────────

def test_a_stale_seat_does_not_class_a_provider_as_subscription():
    fresh = _seats(claude="claude.ai", detected=NOW - 60)
    stale = _seats(claude="claude.ai", detected=NOW - seats_mod.STALE_AFTER_SECONDS - 60)
    assert lv.provider_class("anthropic", seats=fresh, now=NOW) == "subscription"
    assert lv.provider_class("anthropic", seats=stale, now=NOW) == "unclassified"


def test_the_age_rule_is_the_one_seats_and_sources_map_use():
    """No second threshold: the boundary is ``seats.STALE_AFTER_SECONDS`` as ``Seats.is_stale`` judges it."""
    edge = seats_mod.STALE_AFTER_SECONDS
    just_in = _seats(claude="claude.ai", detected=NOW - edge + 5)
    just_out = _seats(claude="claude.ai", detected=NOW - edge - 5)
    for s in (just_in, just_out):
        assert lv.seats_stale(s, NOW) == s.is_stale(NOW)
    assert lv.provider_class("claude", seats=just_in, now=NOW) == "subscription"
    assert lv.provider_class("claude", seats=just_out, now=NOW) == "unclassified"


def test_a_stale_seat_does_not_hide_row_evidence_local_or_a_stale_api_key_login():
    stale = _seats(claude="api-key", keys={"OPENAI_API_KEY": True}, detected=NOW - 3 * DAY)
    assert lv.provider_class("anthropic", seats=stale, now=NOW) == "unclassified"   # not paid either
    assert lv.provider_class("openai", seats=stale, now=NOW) == "unclassified"
    assert lv.provider_class("anthropic", auth="oauth", seats=stale, now=NOW) == "subscription"  # row evidence
    assert lv.provider_class("ollama", seats=stale, now=NOW) == "local"


def test_stale_and_fresh_seats_json_under_a_scratch_home_change_the_stop_metrics(home):
    """The seeded files differ only in age: stale = unclassified (not cheap), fresh = cheap."""
    _decisions_db(home, [{"final_provider": "anthropic", "complexity": "simple"}])
    mod = _load_hook("f99_seat_age")
    mod._STOP_SESSION_ID = "S"

    _write_seats(home, _real_now_seats(3 * DAY, claude="claude.ai"))
    stale = mod._query_session_metrics(time.time() - 3600)
    assert stale["unclassified_count"] == 1 and stale["routing_effectiveness_pct"] == 0.0

    _write_seats(home, _real_now_seats(60, claude="claude.ai"))
    fresh = mod._query_session_metrics(time.time() - 3600)
    assert fresh["unclassified_count"] == 0 and fresh["routing_effectiveness_pct"] == 100.0


# ── 2. the box says how many rows are unclassified ────────────────────────────

def _panel(**kw) -> str:
    console = Console(record=True, width=120, file=io.StringIO())
    console.print(SessionSummaryDashboard(console=console).render_main_panel(
        decisions=[{"method": "heuristic", "count": 5}], executed_routes=None, **kw))
    # The panel is narrow and wraps a long line: compare the words, not the box.
    return " ".join(re.sub(r"[│╭╮╰╯─━]", " ", console.export_text()).split())


def test_effective_and_escalated_show_the_unclassified_count():
    text = _panel(routing_effectiveness_pct=50.0, escalation_count=1, escalation_pct=25.0,
                  unclassified_count=2, decisions_n=4, session_basis="n=4, session abc")
    assert re.search(r"Effective: 50% \(2 unclassified\)", text), text
    assert re.search(r"Escalated 1 \(25%\) \(2 unclassified\)", text), text


def test_effective_and_escalated_are_na_when_every_row_is_unclassified():
    text = _panel(routing_effectiveness_pct=0.0, escalation_count=0, escalation_pct=0.0,
                  unclassified_count=3, decisions_n=3, session_basis="n=3, session abc")
    assert re.search(r"Effective: n/a \(3 unclassified\)", text), text
    assert re.search(r"Escalated n/a \(3 unclassified\)", text), text
    assert "Escalated 0" not in text and "Effective: 0" not in text


def test_a_fully_classified_session_prints_no_unclassified_note():
    text = _panel(routing_effectiveness_pct=50.0, escalation_count=1, escalation_pct=25.0,
                  decisions_n=4, session_basis="n=4, session abc")
    assert "unclassified" not in text and "Escalated 1 (25%)" in text


def test_the_hook_hands_the_unclassified_count_to_the_box(home):
    _decisions_db(home, [{"final_provider": "mystery"}, {"final_provider": "ollama"}])
    mod = _load_hook("f99_unclassified_count")
    mod._STOP_SESSION_ID = "S"
    m = mod._query_session_metrics(time.time() - 3600)
    assert m["unclassified_count"] == 1 and m["total_decisions"] == 2
    src = HOOK.read_text()
    assert 'unclassified_count=session_metrics.get("unclassified_count", 0)' in src
    assert 'decisions_n=session_metrics.get("total_decisions", 0)' in src


# ── 3. no provider literals drive the free / paid / cc split ──────────────────

def test_the_provider_lists_are_gone_from_the_hook():
    mod = _load_hook("f99_no_lists")
    assert not hasattr(mod, "_FREE_PROVIDERS")
    assert '{"ollama", "codex", "gemini_cli"}' not in HOOK.read_text()


def test_the_split_follows_provider_class_and_fresh_seats(home):
    rows = [("ollama", "llama-x"), ("lm_studio", "local-y"), ("gemini_cli", "g-1"),
            ("subscription", "cc-1"), ("codex", "c-1"), ("openai", "o-1"), ("mystery", "m-1")]
    _usage_db(home, rows)
    mod = _load_hook("f99_split")
    mod._STOP_SESSION_ID = "S"

    _write_seats(home, _real_now_seats(60, gemini="google"))
    paid, cc, free = mod._query_session_data(time.time() - 7200)
    assert sorted(r["provider"] for r in free) == ["gemini_cli", "lm_studio", "ollama"]  # local + seat
    assert [r["provider"] for r in cc] == ["subscription"]
    assert sorted(r["provider"] for r in paid) == ["mystery", "openai"]                 # unclassified stay
    assert all(r["provider"] != "codex" for r in paid + cc + free)                       # own panel (D2)

    _write_seats(home, _real_now_seats(3 * DAY, gemini="google"))                        # the seat lapsed
    paid, _cc, free = mod._query_session_data(time.time() - 7200)
    assert "gemini_cli" in {r["provider"] for r in paid} and "gemini_cli" not in {r["provider"] for r in free}


def test_the_dedicated_panel_exception_is_pinned_and_named():
    """Kept on purpose: 'has its own panel' is product design, not derivable from a seat. Codex is
    reported by ``_format_codex_section`` from ``codex_usage``; listing it again double-counts."""
    mod = _load_hook("f99_dedicated")
    assert mod._DEDICATED_PANEL_PROVIDERS == {"codex"}
    fn = next(n for n in ast.walk(ast.parse(HOOK.read_text()))
              if isinstance(n, ast.FunctionDef) and n.name == "_format_codex_section")
    assert "codex_usage" in ast.get_source_segment(HOOK.read_text(), fn)


def test_the_test_fixture_model_sentinels_are_pinned():
    """Kept on purpose: the router's own fixture names, not a provider or model the user runs."""
    mod = _load_hook("f99_sentinels")
    assert mod._TEST_MODEL_PATTERNS == {"mock-model", "test-model", "fake-model", "mock", "test"}
    assert mod._is_test_model("mock/x") and not mod._is_test_model("llama-x")


# ── 4. the cache saving is read through ledger_view ───────────────────────────

def _lookups_db(home: Path, rows: list[tuple[float, int, float]], *, saved_column: bool = True) -> Path:
    db = home / "usage.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE semantic_cache_lookups (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "ts REAL NOT NULL, task_type TEXT, hit INTEGER NOT NULL"
                + (", saved_usd REAL NOT NULL DEFAULT 0" if saved_column else "") + ")")
    for ts, hit, saved in rows:
        if saved_column:
            con.execute("INSERT INTO semantic_cache_lookups (ts, task_type, hit, saved_usd) VALUES (?, 'q', ?, ?)",
                        (ts, hit, saved))
        else:
            con.execute("INSERT INTO semantic_cache_lookups (ts, task_type, hit) VALUES (?, 'q', ?)", (ts, hit))
    con.commit()
    con.close()
    return db


def test_cache_hit_stats_carry_n_and_window_and_respect_the_bounds(home):
    db = _lookups_db(home, [(NOW - 60, 1, 0.25), (NOW - 120, 0, 0.0), (NOW - 180, 1, 0.5),
                            (NOW - 3 * DAY, 1, 9.0)])
    c = lv.cache_hit_stats(lv.resolve("24h", now=NOW), db_path=db)
    assert (c.lookups, c.hits, c.n, c.window) == (3, 2, 3, "24h")
    assert c.saved_usd == pytest.approx(0.75)


def test_cache_hit_stats_are_unknown_not_zero(home, tmp_path):
    bounds = lv.resolve("24h", now=NOW)
    assert lv.cache_hit_stats(bounds, db_path=tmp_path / "absent.db") is None          # no database
    db = _lookups_db(home, [(NOW - 3 * DAY, 1, 9.0)])
    assert lv.cache_hit_stats(bounds, db_path=db) is None                              # no lookup in the window


def test_cache_hit_saving_is_none_when_the_table_records_none(tmp_path):
    db = _lookups_db(tmp_path, [(NOW - 60, 1, 0.0)], saved_column=False)
    c = lv.cache_hit_stats(lv.resolve("24h", now=NOW), db_path=db)
    assert c.n == 1 and c.hits == 1 and c.saved_usd is None


def test_the_hook_reads_the_cache_through_ledger_view(home):
    _lookups_db(home, [(time.time(), 1, 0.004), (time.time(), 0, 0.0), (time.time(), 0, 0.0)])
    mod = _load_hook("f99_cache_hook")
    stats = mod._query_cache_hit_stats()
    assert (stats["hits"], stats["lookups"], stats["n"], stats["window"]) == (1, 3, 3, "today")
    assert stats["estimated_saved_usd"] == pytest.approx(0.004)
    assert "semantic_cache_lookups" not in HOOK.read_text()          # no private read left in the hook
    assert "cache_hit_stats(" in HOOK.read_text()


def test_the_cache_line_prints_its_window():
    assert "cache hit (n={cache_stats['n']}, {cache_stats['window']})" in HOOK.read_text()


# ── 5. hook pair, version, Stop path ──────────────────────────────────────────

def test_the_hook_pair_is_byte_identical_at_v43():
    a, b = HOOK_TOP.read_bytes(), HOOK.read_bytes()
    assert a == b and b.splitlines()[1] == b"# llm_router-hook-version: 46"


def test_the_stop_path_gains_no_import_or_call():
    """ledger_view / seats stay inside the full-mode and child functions; the cache-hit read stays in
    the cumulative section, the session rows in main (full mode only) and the child."""
    tree = ast.parse(HOOK.read_text())
    top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    names = {a.name for n in top for a in n.names} | {getattr(n, "module", "") or "" for n in top}
    assert not any("ledger_view" in x or x.endswith("seats") for x in names), names
    callers: dict[str, set[str]] = {"_query_cache_hit_stats": set(), "_query_session_data": set()}
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef):
            for n in ast.walk(fn):
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in callers:
                    callers[n.func.id].add(fn.name)
    assert callers["_query_cache_hit_stats"] == {"_format_cumulative_section"}
    assert callers["_query_session_data"] == {"_render_last_summary", "main"}
    assert "if _full:\n            paid_rows, cc_rows, free_rows = _query_session_data" in HOOK.read_text()
