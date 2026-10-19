"""F119 / D-247 guard: every user-visible savings surface, rendered from a fixture with KNOWN
verified, estimated and unknown values, must (a) never print the merged sum, (b) label what it
prints verified or est., (c) print unknown as n/a and never as 0.

Behavioural, not a source lint: each test renders the surface and reads the text.
Fixture: verified $2.00 (n=2) and est. $5.00 (n=4) -- their sum, $7.00, must appear nowhere.
"""
from __future__ import annotations

import importlib.util
import io
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / "src" / "llm_router" / "hooks"
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(s: str) -> str:
    return ANSI.sub("", s)


def _hook(name: str, alias: str):
    spec = importlib.util.spec_from_file_location(alias, HOOKS / name)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[alias] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def se():
    return _hook("session-end.py", "f119_session_end")


def _no_merge(text: str) -> None:
    assert "7.00" not in text, f"verified + est. merged into one figure:\n{text}"


# ── session-end Stop hook ──────────────────────────────────────────────────────

def test_split_text_two_segments_negative_and_unknown(se):
    text = se._split_text([2.0, 2, 5.0, 4])
    assert "$2.00 verified (n=2)" in text and "~$5.00 est. (n=4)" in text
    _no_merge(text)
    assert "-$1.00 est." in se._split_text([2.0, 2, -1.0, 3]) and "$-" not in se._split_text([2.0, 2, -1.0, 3])
    assert se._split_text(None) == "verified n/a · est. n/a"
    assert "est. n/a" in se._split_text([2.0, 2, None, None]) and "$2.00 verified" in se._split_text([2.0, 2, None, None])


def test_complexity_breakdown_unknown_cost_is_na_not_free(se, monkeypatch):
    rows = [
        {"complexity": "simple", "model": "m-unknown", "provider": "openai", "cost_usd": None},
        {"complexity": "simple", "model": "m-unknown", "provider": "openai", "cost_usd": None},
        {"complexity": "moderate", "model": "m-part", "provider": "openai", "cost_usd": 0.5},
        {"complexity": "moderate", "model": "m-part", "provider": "openai", "cost_usd": None},
        {"complexity": "complex", "model": "m-local", "provider": "ollama", "cost_usd": 0.0},
    ]
    from llm_router import ledger_view

    monkeypatch.setattr(ledger_view, "session_usage_rows", lambda *a, **k: rows)
    monkeypatch.setattr(se, "_session_bounds", lambda *_a, **_k: None, raising=False)
    monkeypatch.setattr(se, "_query_session_metrics", lambda *_a, **_k: {}, raising=False)
    text = _plain("\n".join(se._format_complexity_breakdown(0.0)))
    simple = next(ln for ln in text.splitlines() if ln.strip().startswith("simple"))
    moderate = next(ln for ln in text.splitlines() if ln.strip().startswith("moderate"))
    complex_ = next(ln for ln in text.splitlines() if ln.strip().startswith("complex"))
    assert "cost n/a" in simple and "$0" not in simple and "free" not in simple
    assert "$0.5000 (1 of 2)" in moderate
    assert "free" in complex_                                     # a recorded $0.00 is free
    assert "avg $0.2500/call" in text                             # over the 2 costed rows, not 5


def test_free_section_unknown_tokens_are_na_known_are_est(se):
    unknown = _plain("\n".join(se._format_free_section(
        [{"provider": "ollama", "input_tokens": 0, "output_tokens": 0}], [])))
    assert "n/a gross saved" in unknown and "$0.0000" not in unknown
    known = _plain("\n".join(se._format_free_section(
        [{"provider": "ollama", "input_tokens": 10_000, "output_tokens": 5_000}], [])))
    assert "est. gross saved" in known


def test_net_saved_line_is_labelled_est(se):
    free = [{"input_tokens": 2000, "output_tokens": 1000, "cost_usd": 0.0}]
    assert "Net saved      est. $" in _plain(se._net_session_line(free, []))


# ── Rich dashboard (ui/session_summary) ─────────────────────────────────────────

def _render(**kw) -> str:
    from rich.console import Console

    from llm_router.ui.session_summary import SessionSummaryDashboard

    buf = io.StringIO()
    console = Console(file=buf, width=200, force_terminal=False, color_system=None)
    dash = SessionSummaryDashboard(console=console)
    main = kw.pop("main", {})
    act = kw.pop("act", None)
    console.print(dash.render_main_panel(**main))
    if act is not None:
        console.print(dash.render_activity_panel(**act))
    return buf.getvalue()


def test_dashboard_models_panel_unknown_cost_is_na_partial_is_k_of_n():
    models = [
        {"model": "m-unknown", "calls": 3, "tokens": 900, "cost": 0.0, "cost_n": 0, "provider": "openai"},
        {"model": "m-part", "calls": 4, "tokens": 800, "cost": 1.5, "cost_n": 2, "provider": "openai"},
    ]
    text = _render(main=dict(session_models=models))
    unknown = next(ln for ln in text.splitlines() if "m-unknown" in ln)
    part = next(ln for ln in text.splitlines() if "m-part" in ln)
    total = next(ln for ln in text.splitlines() if ln.strip().startswith("│") and "total" in ln)
    assert "n/a" in unknown and "$0" not in unknown
    assert "$1.50 (2 of 4)" in part
    assert "(2 of 7)" in total and "saved n/a" in total          # no per-model saving is known


def test_dashboard_models_panel_all_unknown_total_is_na():
    models = [{"model": "m-unknown", "calls": 3, "tokens": 9, "cost": 0.0, "cost_n": 0}]
    text = _render(main=dict(session_models=models))
    total = next(ln for ln in text.splitlines() if "total" in ln)
    assert "n/a" in total and "$0.00" not in total


def test_dashboard_savings_split_missing_window_and_est_only_period():
    sav = {
        "lifetime_text": "$2.00 verified (n=2) · ~$5.00 est. (n=4)", "lifetime": 2.0,
        # today absent entirely: the ledger could not be read
        "14d_text": "$0.00 verified (n=0) · ~$3.00 est. (n=2)", "14d": 0.0,
    }
    text = _render(main=dict(savings=sav))
    _no_merge(text)
    assert "$2.00 verified" in text and "~$5.00 est." in text
    flat = re.sub(r"\s+", " ", text)
    assert "today verified n/a" in flat                                  # missing window: n/a, not $0.00
    assert "today $0.00" not in flat
    assert "14-day $0.00 verified" in flat and "~$3.00 est." in flat      # est.-only period is shown


def test_dashboard_empty_period_hidden_and_bare_numeric_is_labelled():
    sav = {"lifetime": 2.0, "today": 1.0, "week_text": "$0.00 verified (n=0) · ~$0.00 est. (n=0)", "week": 0.0}
    flat = re.sub(r"\s+", " ", _render(main=dict(savings=sav)))
    assert "lifetime $2.00 verified" in flat and "est. n/a" in flat       # bare numeric = verified only
    assert "week" not in flat.split("SAVINGS")[1]                         # a known-empty period is skipped


def test_dashboard_14_day_footer_is_labelled_verified_and_unknown_is_na():
    base = dict(daily_calls=[1, 2], daily_tokens=[10, 20], daily_costs=[0.5, 1.5])
    text = _render(main={}, act=dict(base, total_saved=2.0))
    assert "$2.00 verified saved over 2 days" in text and "verified saved/day" in text
    none = _render(main={}, act=dict(base, total_saved=None))
    assert "n/a verified saved over" in none and "$0.00" not in none


def test_dashboard_negative_usd_sign_before_dollar():
    from llm_router.ui.session_summary import _fmt_usd

    assert _fmt_usd(-1.0) == "-$1.00" and _fmt_usd(-0.001) == "-$0.0010"


# ── statusline / status bar ─────────────────────────────────────────────────────

def _window(**kw):
    from llm_router import ledger_view as lv

    base = dict(as_of="2026-10-19T00:00:00Z", window="today", source="usage.db", estimated=True)
    return lv.LedgerWindow(**{**base, **kw})


def test_status_bar_money_part_two_segments_never_merged():
    sb = _hook("status-bar.py", "f119_status_bar")
    from llm_router import ledger_view as lv

    both = _window(verified_usd=2.0, verified_n=2, unverified_usd=5.0, unverified_n=4,
                   saved_usd=7.0, saved_n=6)
    text = _plain(sb._money_part(lv, "D", both))
    _no_merge(text)
    assert "~$5.00 est n=4" in text and "$2.00 ver n=2" in text
    est_only = _plain(sb._money_part(lv, "D", _window(
        verified_usd=0.0, verified_n=0, unverified_usd=3.0, unverified_n=2, saved_usd=3.0, saved_n=2)))
    assert "~$3.00 est n=2" in est_only and "ver" not in est_only
    half = _plain(sb._money_part(lv, "D", _window(
        verified_usd=None, verified_n=None, unverified_usd=3.0, unverified_n=2)))
    assert "~$3.00 est n=2" in half and "ver n/a" in half
    assert _plain(sb._money_part(lv, "D", None)) == "D:n/a"
    assert _plain(sb._money_part(lv, "D", _window())) == "D:n/a"       # nothing known: n/a, not $0


# ── share card ──────────────────────────────────────────────────────────────────

def test_share_card_is_labelled_est_and_unknown_is_na():
    from llm_router.commands.share import SavingsStats, render_svg

    known = SavingsStats(total_calls=3, paid_calls=1, free_calls=2, total_saved=5.0, savings_pct=40, known=True)
    assert known.saved_text == "est. $5.00" and "est." in known.pct_text
    assert "est. $5.00" in render_svg(known)
    neg = SavingsStats(total_saved=-1.0, known=True)
    assert neg.saved_text == "est. -$1.00"
    unknown = SavingsStats()                                           # unreadable ledger
    svg = render_svg(unknown)
    assert "est. n/a" in svg and "$0.00" not in svg and "0% cheaper" not in svg


def test_share_gather_skips_paid_rows_without_cost(tmp_path):
    import sqlite3

    from llm_router.commands.share import _gather_stats

    db = tmp_path / "usage.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE usage (provider TEXT, model TEXT, input_tokens INT, output_tokens INT, "
                "cost_usd REAL, success INT, is_simulated INT)")
    con.executemany("INSERT INTO usage VALUES (?,?,?,?,?,1,0)", [
        ("openai", "m", 1_000_000, 0, None), ("openai", "m", 1_000_000, 0, 1.0)])
    con.commit()
    con.close()
    st = _gather_stats(str(db))
    assert st.known and st.paid_calls == 2
    from llm_router.commands.share import SONNET_IN

    # Only the costed row counts: its baseline (1M Sonnet input tokens) minus the $1.00 paid. The
    # unknown-cost row is not credited a full baseline as if it had cost nothing.
    assert st.total_saved == pytest.approx(SONNET_IN - 1.0, abs=0.01)


# ── retrospective / snapshot / replay / cyber-grid / clawcode / tui ─────────────

def test_retrospective_saved_is_est_or_na():
    from llm_router.retrospective import analyze_facts, format_saved

    assert format_saved(None) == "est. n/a" and format_saved(0.0033) == "est. $0.0033"
    assert format_saved(-1.0) == "est. -$1.0000"
    assert analyze_facts([], [])["total_saved"] is None


def test_snapshot_status_unknown_saved_is_na():
    from llm_router.commands.snapshot import format_snapshot_status

    text = _plain(format_snapshot_status({"facts": {"total_calls": 2, "total_cost": 0.5}}))
    assert "Saved: est. n/a" in text and "$0.00" not in text.split("Saved")[1]
    known = _plain(format_snapshot_status({"facts": {"total_calls": 2, "total_cost": 1.0, "total_saved": 1.0}}))
    assert "Saved: est. $1.0000 (50%)" in known


def test_replay_summary_unknown_is_na(capsys):
    from llm_router.commands import replay

    base = {"timestamp": "2026-10-19T10:00:00", "model": "m", "task_type": "code", "confidence": 0.9}
    replay.print_session_replay([dict(base), dict(base)])
    out = _plain(capsys.readouterr().out)
    assert "Cost: n/a" in out and "Saved: est. n/a" in out
    replay.print_session_replay([dict(base, cost_usd=0.5, saved_usd=1.5), dict(base)])
    out = _plain(capsys.readouterr().out)
    assert "Cost: $0.50 (1 of 2 recorded)" in out and "Saved: est. $1.50" in out


def test_cyber_grid_cumulative_is_labelled_verified_and_missing_is_na():
    from rich.console import Console

    from llm_router.hooks import cyber_grid

    def text_of(data):
        buf = io.StringIO()
        Console(file=buf, width=120, force_terminal=False, color_system=None).print(cyber_grid._build_financial(data))
        return buf.getvalue()

    cum = [("today", 1, 10, 0, 0.5), ("all time", 3, 30, 0, 2.0)]
    shown = text_of({"cumulative": cum})
    assert "lifetime verified" in shown and "$2.0000" in shown or "$2.00" in shown
    assert "verified saved" in shown
    missing = text_of({"cumulative": []})
    assert "n/a" in missing and "$0.0000" not in missing


def test_clawcode_free_section_no_invented_tokens():
    cc = _hook("session-end-clawcode.py", "f119_clawcode")
    unknown = _plain("\n".join(cc._format_free_section(
        [{"provider": "ollama", "input_tokens": 0, "output_tokens": 0}],
        [{"input_tokens": 4000, "output_tokens": 2000}])))
    assert "est. n/a saved" in unknown and "n/a saved" in unknown and "~est" not in unknown
    known = _plain("\n".join(cc._format_free_section(
        [{"provider": "ollama", "input_tokens": 10_000, "output_tokens": 5_000}], [])))
    assert "est. $" in known


def test_tui_sparkline_panel_labels_verified():
    pytest.importorskip("textual")
    from llm_router.tui.panels import SparklinePanel

    p = SparklinePanel()
    p.daily_costs, p.total_savings = [0.5, 1.5], 2.0
    body = str(p.render().renderable)
    assert "verified saved" in body and "$2.00 verified (week)" in body


# ── F119 repair 1: empty-known window, hook matrix ─────────────────────────────

def test_status_bar_known_empty_window_is_a_real_zero_not_a_crash():
    """Regression: est 0.0/n=0 and ver 0.0/n=0 (ledger_view's COALESCE on a day with no calls)
    left ``segs`` empty and raised IndexError, which froze the refresher on a stale line."""
    sb = _hook("status-bar.py", "f119_status_bar_empty")
    from llm_router import ledger_view as lv

    empty = _window(verified_usd=0.0, verified_n=0, unverified_usd=0.0, unverified_n=0,
                    saved_usd=0.0, saved_n=0)
    text = _plain(sb._money_part(lv, "D", empty))
    assert text == "D:~$0.00 est n=0 ·today"
    assert "n/a" not in text


def test_status_bar_known_unknown_matrix():
    sb = _hook("status-bar.py", "f119_status_bar_matrix")
    from llm_router import ledger_view as lv

    def part(**kw):
        return _plain(sb._money_part(lv, "D", _window(**kw)))

    both_unknown = part(verified_usd=None, verified_n=None, unverified_usd=None, unverified_n=None)
    assert both_unknown == "D:n/a"
    est_unknown = part(verified_usd=2.0, verified_n=2, unverified_usd=None, unverified_n=None)
    assert "$2.00 ver n=2" in est_unknown and "est n/a" in est_unknown
    ver_unknown = part(verified_usd=None, verified_n=None, unverified_usd=5.0, unverified_n=4)
    assert "~$5.00 est n=4" in ver_unknown and "ver n/a" in ver_unknown
    both_known = part(verified_usd=2.0, verified_n=2, unverified_usd=5.0, unverified_n=4)
    assert "~$5.00 est n=4" in both_known and "$2.00 ver n=2" in both_known
    _no_merge(both_known)
    both_empty = part(verified_usd=0.0, verified_n=0, unverified_usd=0.0, unverified_n=0)
    assert both_empty == "D:~$0.00 est n=0 ·today"


def test_status_bar_hook_pair_byte_identical():
    assert (REPO / "hooks" / "status-bar.py").read_bytes() == (HOOKS / "status-bar.py").read_bytes()


# ── F119 repair 1: remaining surfaces ──────────────────────────────────────────

def test_surface_status_labels_verified_and_unverified_apart(tmp_path, monkeypatch):
    from llm_router.observability import surface_status as ss

    monkeypatch.setenv("LLM_ROUTER_STATE_DIR", str(tmp_path))
    st = ss.SurfaceStatus(
        host="h", active=True, health=ss.HEALTH_OK, health_reason="", last_model="ollama/m",
        last_task="code/simple", last_age_s=1.0, last_tokens=100, routed_count_session=3,
        saved_session=2.0, saved_total=2.0, tokens_session=100, unverified_session=5.0)
    line = _plain(ss.compact_line(st, color=False))
    assert "$2.00 verified saved" in line and "+$5.00 unverified" in line
    _no_merge(line)
    assert "$2.00 verified" in ss.terminal_title(st)
    note = ss.notification(st, "route", now=1e9)
    assert "verified saved $2.00" in note["message"]
    none = _plain(ss.compact_line(ss.SurfaceStatus(
        host="h", active=False, health=ss.HEALTH_OK, health_reason="", last_model=None, last_task=None,
        last_age_s=None, last_tokens=None, routed_count_session=0, saved_session=0.0, saved_total=0.0,
        tokens_session=0), color=False))
    assert "saved" not in none          # nothing verified: no "$0.00 saved" claim


def test_statusline_hud_savings_are_est(monkeypatch):
    from llm_router import statusline_hud as hud

    st = hud.StatuslineState(decision_count=2, total_cost_session=0.5, total_saved_session=2.0)
    monkeypatch.setattr(hud, "_statusline_state", st)
    ctx = _plain(hud.format_statusline_context(max_width=500))
    assert "est. $2.00 saved" in ctx
    replay = _plain(hud.format_replay_summary())
    assert "Savings: est. $2.00" in replay
    _no_merge(ctx + replay)
    monkeypatch.setattr(hud, "_statusline_state", hud.StatuslineState(decision_count=2))
    assert "saved" not in _plain(hud.format_statusline_context(max_width=500))


def test_tools_dashboard_net_saved_is_verified_and_signed():
    from llm_router.tools.dashboard import _render_dashboard

    daily = [{"day": "2026-10-18", "tokens": 1000, "calls": 2, "saved": 2.0}]
    brk = [{"provider": "ollama", "calls": 2, "tokens": 1000, "saved": 2.0}]
    text = _plain(_render_dashboard(daily, brk, 0.0, "Last 7 days", "opus", False))
    assert "$2.00 verified net saved" in text
    _no_merge(text)
    neg = _plain(_render_dashboard(daily, brk, 3.0, "Last 7 days", "opus", False))
    assert "-$1.00 verified net saved" in neg and "$-" not in neg.split("verified net")[0]


def test_dashboard_server_unknown_saved_is_na_and_labelled():
    from llm_router.dashboard import server

    html = server._html("")
    assert "Lifetime Savings (est. vs Sonnet)" in html and "Lifetime Saved (est.)" in html
    assert re.search(r'id="g-saved"[^>]*>n/a<', html) and re.search(r'id="p-saved"[^>]*>n/a<', html)
    assert '>$0<' not in html.split('id="g-saved"')[1][:60]
    assert "savedKnown ? '$' + saved.toFixed(2) : 'n/a'" in html        # JS maps null to n/a
    import asyncio

    stats = asyncio.run(server._get_stats())
    assert stats["savings"]["total_saved_usd"] in (None,) or isinstance(stats["savings"]["total_saved_usd"], float)


def test_digest_saved_is_est(monkeypatch):
    import asyncio

    from llm_router import digest

    data = {"calls": 3, "cost": 1.0, "saved": 2.0, "total_in": 1, "total_out": 1,
            "paid_calls": 0, "paid_cost_n": 0,
            "by_provider": {"ollama": {"calls": 3, "cost": 1.0, "saved": 2.0, "cost_n": 0, "paid": False}}}

    async def fetch(_p):
        return data

    async def spike(*a, **k):
        return (False, 0.0, 0.0)

    async def sim(_p="week"):
        return (1.0, 3.0, 66.0)

    monkeypatch.setattr(digest, "_fetch_period_data", fetch)
    monkeypatch.setattr(digest, "detect_spend_spike", spike)
    monkeypatch.setattr(digest, "simulate_without_routing", sim)
    text = asyncio.run(digest.format_digest("week"))
    assert "est. $2.0000 vs Claude baseline" in text and "est. $2.0000 saved" in text
    assert "saved est. 66%" in text
    _no_merge(text)


def test_savings_report_per_model_saved_is_est(tmp_path, monkeypatch):
    from llm_router.commands import savings_report as sr

    db = tmp_path / "usage.db"
    db.write_bytes(b"")
    monkeypatch.setattr(sr, "_get_db_path", lambda: db)
    monkeypatch.setattr(sr, "_canonical_headline", lambda *_a, **_k: "headline")
    stats = {"calls": 3, "saved": 2.0, "cost": 0.5, "unverified": 0.0, "unverified_calls": 0,
             "by_model": {"ollama/m": {"calls": 3, "saved": 2.0, "cost": 0.0, "provider": "ollama"}}}
    monkeypatch.setattr(sr, "_query", lambda *_a, paid=False, **_k: stats if not paid else
                        {**stats, "calls": 0, "by_model": {}})
    text = sr.render_savings_report("all")
    assert "saved $2.0000 (est.) vs Claude" in text and "saved $2.0000 (est.)" in text.split("ollama/m")[1]
    _no_merge(text)


# ── F121: leftovers ────────────────────────────────────────────────────────────

def _usage_db(tmp_path, rows):
    import sqlite3

    db = tmp_path / "usage.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE usage (timestamp TEXT DEFAULT (datetime('now')), provider TEXT, model TEXT,"
                " input_tokens INT, output_tokens INT, cost_usd REAL, success INT DEFAULT 1,"
                " is_simulated INT DEFAULT 0, task_type TEXT)")
    con.executemany("INSERT INTO usage (provider, model, input_tokens, output_tokens, cost_usd) VALUES (?,?,?,?,?)", rows)
    con.commit()
    con.close()
    return str(db)


def test_share_free_row_counted_once_not_twice(tmp_path):
    """F121 bug: a free row was added at `total_saved += base` AND at loop level."""
    from llm_router.commands.share import SONNET_IN, _gather_stats

    stats = _gather_stats(_usage_db(tmp_path, [("ollama", "qwen", 1_000_000, 0, 0.0)]))
    assert stats.total_saved == pytest.approx(SONNET_IN)
    assert stats.free_calls == 1


def test_share_subscription_and_paid_rows_each_counted_once(tmp_path):
    from llm_router.commands.share import SONNET_IN, _gather_stats

    stats = _gather_stats(_usage_db(tmp_path, [("subscription", "m", 1_000_000, 0, 0.0),
                                               ("openai", "gpt", 1_000_000, 0, 1.0),
                                               ("openai", "gpt", 1_000_000, 0, None)]))
    assert stats.total_saved == pytest.approx(SONNET_IN + (SONNET_IN - 1.0))   # unknown-cost row: out


def test_metrics_omit_savings_when_ledger_unreadable(monkeypatch):
    import asyncio

    from llm_router import cost
    from llm_router.dashboard import server

    async def boom(**_k):
        raise RuntimeError("db locked")

    monkeypatch.setattr(cost, "get_savings_by_period", boom)
    text = asyncio.run(server._metrics_text())
    assert "# TYPE llm_router_savings_usd_total counter" in text
    assert not re.search(r"^llm_router_savings_usd_total ", text, re.M), text
    assert not re.search(r"^llm_router_external_usd_total ", text, re.M), text


def test_metrics_omit_savings_when_period_marked_unreadable(monkeypatch):
    import asyncio

    from llm_router import cost
    from llm_router.dashboard import server

    async def unreadable(**_k):
        return {"all_time": {"saved_usd": 0.0, "actual_usd": 0.0, "unreadable": True}}

    monkeypatch.setattr(cost, "get_savings_by_period", unreadable)
    text = asyncio.run(server._metrics_text())
    assert not re.search(r"^llm_router_savings_usd_total ", text, re.M)


def test_metrics_emit_real_zero_and_values(monkeypatch):
    import asyncio

    from llm_router import cost
    from llm_router.dashboard import server

    async def ok(**_k):
        return {"all_time": {"saved_usd": 0.0, "actual_usd": 1.5, "unreadable": False}}

    monkeypatch.setattr(cost, "get_savings_by_period", ok)
    text = asyncio.run(server._metrics_text())
    assert re.search(r"^llm_router_savings_usd_total 0\.0000$", text, re.M)       # a real zero stays
    assert re.search(r"^llm_router_external_usd_total 1\.5000$", text, re.M)


def test_savings_by_period_unknown_cost_paid_row_is_out_and_unreadable_flagged(tmp_path, monkeypatch):
    import asyncio

    from llm_router import cost, ledger_view
    from llm_router.config import get_config

    db = _usage_db(tmp_path, [("openai", "gpt", 1_000_000, 0, None), ("ollama", "q", 1_000_000, 0, 0.0)])
    monkeypatch.setattr(get_config(), "llm_router_db_path", db, raising=False)

    async def _noop():
        class C:
            async def close(self):
                return None
        return C()

    monkeypatch.setattr(cost, "_get_db", _noop)
    monkeypatch.setattr(ledger_view, "usage_period_rows",
                        lambda *_a, **_k: [("openai", 1_000_000, 0, None), ("ollama", 1_000_000, 0, 0.0)])
    out = asyncio.run(cost.get_savings_by_period())["all_time"]
    assert out["unpriced_calls"] == 1 and out["calls"] == 1 and out["unreadable"] is False
    monkeypatch.setattr(ledger_view, "usage_period_rows", lambda *_a, **_k: None)
    assert asyncio.run(cost.get_savings_by_period())["all_time"]["unreadable"] is True


def test_retrospective_cost_known_sum_k_of_n_and_na():
    from llm_router import retrospective as r

    def d(c):
        return {"timestamp": "2026-10-19T10:00:00", "cost_usd": c, "task_type": "t", "final_model": "m"}

    partial = r.analyze_facts([d(1.0), d(None), d(0.5)], [])
    assert partial["total_cost"] == pytest.approx(1.5) and partial["cost_n"] == 2
    assert r.format_cost(partial) == "$1.5000 recorded (2 of 3)"
    none = r.analyze_facts([d(None), d(None)], [])
    assert none["total_cost"] is None and r.format_cost(none) == "n/a"
    full = r.analyze_facts([d(0.25)], [])
    assert r.format_cost(full) == "$0.2500 recorded"
    assert r.format_cost(r.analyze_facts([], [])) == "n/a"


def test_digest_unknown_paid_cost_is_na_and_out_of_saving(tmp_path, monkeypatch):
    import asyncio

    from llm_router import digest

    db = _usage_db(tmp_path, [("openai", "gpt", 1_000_000, 0, None), ("openai", "gpt", 1_000_000, 0, 1.0)])
    import sqlite3

    class _Cur:
        def __init__(self, rows):
            self._r = rows

        async def fetchall(self):
            return self._r

        async def fetchone(self):
            return self._r[0] if self._r else None

    class _Db:
        def __init__(self):
            self._c = sqlite3.connect(db)

        async def execute(self, sql, *a):
            return _Cur(self._c.execute(sql, *a).fetchall())

        async def close(self):
            self._c.close()

    async def get():
        return _Db()

    monkeypatch.setattr(digest, "_get_db", get)
    data = asyncio.run(digest._fetch_period_data("all time"))
    assert data["paid_calls"] == 2 and data["paid_cost_n"] == 1 and data["cost"] == pytest.approx(1.0)
    assert digest._cost_text(data["cost"], 1, 2) == "$1.0000 (1 of 2 paid recorded)"
    assert digest._cost_text(0.0, 0, 2) == "n/a" and digest._cost_text(0.0, 0, 0) == "$0.0000"
    sold = digest._host_baseline(1_000_000, 0)
    assert data["saved"] == pytest.approx(sold - 1.0)        # the unknown row is not a full-baseline saving


def test_gain_cost_cells_k_of_n_and_na():
    from llm_router.commands import gain

    assert gain._fmt_cost({"count": 3, "cost": 1.0, "cost_n": 2}) == "$1.0000 (2 of 3)"
    assert gain._fmt_cost({"count": 3, "cost": 0.0, "cost_n": 0}) == "n/a"
    assert gain._fmt_cost({"count": 3, "cost": 2.0}) == "$2.0000"


def test_quota_savings_unreadable_ledger_is_none_not_zero(tmp_path):
    from datetime import datetime, timezone

    from llm_router import quota_savings as q

    assert q._sum_saved_usd_since(tmp_path / "nope.db", datetime.now(timezone.utc)) is None
    db = _usage_db(tmp_path, [])          # readable ledger, no usage.saved_usd column
    assert q._sum_saved_usd_since(__import__("pathlib").Path(db), datetime.now(timezone.utc)) is None


def test_team_label_is_est_and_telegram_paid_unknown_is_na():
    from llm_router import team

    assert team._savings_label({"provenance": "measured", "baseline_equivalent_avoided_usd": 2.0,
                                "real_dollars_avoided_usd": 0.0}).startswith("est. $2.0000")
    msg = team._telegram_message({"provenance": "unknown", "actual_usd": 0.0}, "1")["text"]
    assert "paid n/a" in msg and "paid \\$0.0000" not in msg


def test_agentic_savings_render_is_est():
    from llm_router.agentic.savings import Savings

    assert Savings(1.0, 3.0, 2.0, 3.0).render().startswith("est. saved +$2.0000")


def test_explain_dashboard_total_not_merged(tmp_path, monkeypatch):
    from llm_router.commands import explain_dashboard as ed

    db = _usage_db(tmp_path, [("openai", "gpt", 10, 10, None), ("openai", "gpt", 10, 10, 0.5)])
    monkeypatch.setattr(ed, "_db_path", lambda: __import__("pathlib").Path(db))
    text = _plain("\n".join(ed._savings_panel_block()))
    assert "1 of 2 recorded" in text and "est. saved (platform tables)" in text
    assert "verified saved (savings_stats)" in text
    assert "  saved:" not in text


def test_kpi_o1_gate_uses_larger_segment_n_not_a_sum(monkeypatch):
    from llm_router import dashboard_data as dd
    from llm_router.commands import kpi

    class S:
        unverified_n, realized_n = 30, 30      # a SUM (60) would clear MIN_N=50; neither segment does
        unverified_usd, realized_usd = 1.0, 1.0

        def display(self):
            return "est. saved $1.00 (n=30) · $1.00 verified saved (n=30)"

    monkeypatch.setattr(dd, "summary", lambda period: S())
    monkeypatch.setattr(dd, "newest_timestamp", lambda: None)
    out = kpi._o1_quota_avoided(7)
    assert out["measurable"] is False and out["n"] == 30


def test_merged_sums_are_gone():
    from llm_router import dashboard_data as dd
    from llm_router import ledger_view as lv

    assert not hasattr(dd.Summary, "estimated_usd") and not hasattr(dd.Summary, "estimated_n")
    assert not hasattr(lv, "saved_usd")
    assert "saved" not in lv.LedgerWindow(as_of="T", window="today", source="usage", estimated=True).figures()
    assert "saved_usd" in lv.LedgerWindow.__dataclass_fields__     # deprecated field kept: v2 JSON compat
