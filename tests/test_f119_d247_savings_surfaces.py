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
            "by_provider": {"ollama": {"calls": 3, "cost": 1.0, "saved": 2.0}}}

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
