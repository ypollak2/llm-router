"""W8 (coordinator S1): the surfaces that read the saved-money estimate agree on what a call is.

``ledger_view.money_window`` counts a call once (``docs/bugs/MONEY3X-1.md``). The daily series
(``dashboard_data.query_daily``: the Stop hook's 14-day chart, the TUI) summed raw rows per table, so over 7
days it showed 7.96 dollars where the window said 5.02 for the same ledger. It takes the repeats out of its
day's unverified dollars now; ``calls`` and ``tokens`` stay raw rows per table, as ``WindowTotals`` has them.
"""
from __future__ import annotations

import time

import pytest

from llm_router import dashboard_data as dd
from tests.test_w8_mix_money import LOCAL, _db


def _stamps(ago_s):
    t = time.gmtime(time.time() - ago_s)
    return time.strftime("%Y-%m-%d %H:%M:%S", t), time.strftime("%Y-%m-%dT%H:%M:%S.123456+00:00", t)


def _call_in_three_tables(con, i, ago_s, *, tin=1000, tout=50):
    """One local call as the router records it: ``usage``, ``claude_usage`` and ``savings_stats``."""
    u, s = _stamps(ago_s)
    cid = f"call{i:04d}"
    con.execute("INSERT INTO usage (timestamp, model, provider, input_tokens, output_tokens, cost_usd, success, "
                "is_simulated, correlation_id) VALUES (?,?,?,?,?,0,1,0,?)", (u, LOCAL, "ollama", tin, tout, cid))
    con.execute("INSERT INTO claude_usage (timestamp, model, tokens_used, input_tokens, output_tokens, cost_saved_usd, "
                "is_simulated) VALUES (?,?,?,?,?,?,0)", (u, LOCAL, tin + tout, tin, tout, 0.01 * (i + 1)))
    con.execute("INSERT INTO savings_stats (timestamp, session_id, task_type, estimated_claude_cost_saved, model_used, host, "
                "input_tokens, output_tokens, mode, is_simulated) VALUES (?,?,?,?,?,?,?,?,NULL,0)",
                (s, cid, "query", 0.01 * (i + 1), LOCAL, "router", tin, tout))


def test_the_daily_series_sums_to_the_window_because_a_call_counts_once(tmp_path):
    path, con = _db(tmp_path)
    for i in range(4):
        _call_in_three_tables(con, i, ago_s=600 + 60 * i)
    con.commit()
    window = dd.query_window("week", db_path=path)
    daily = dd.query_daily(7, db_path=path)
    assert window.unverified_calls == 4 and window.calls == 12                      # 12 rows are 4 calls
    assert sum(d.unverified_saved_usd for d in daily) == pytest.approx(window.unverified_saved_usd)
    assert window.unverified_saved_usd > 0
    assert sum(d.calls for d in daily) == window.calls == 12                        # calls stay the raw rows


def test_without_a_repeated_call_the_daily_series_is_what_it_was(tmp_path):
    path, con = _db(tmp_path)
    u, s = _stamps(600)
    con.execute("INSERT INTO usage (timestamp, model, provider, input_tokens, output_tokens, cost_usd, success, "
                "is_simulated, correlation_id) VALUES (?,?,?,?,?,0,1,0,'only')", (u, LOCAL, "ollama", 4000, 100))
    con.execute("INSERT INTO claude_usage (timestamp, model, tokens_used, input_tokens, output_tokens, cost_saved_usd, "
                "is_simulated) VALUES (?,?,?,?,?,0.02,0)", (u, "claude-sonnet-5-5", 900, 800, 100))      # another call
    con.commit()
    window = dd.query_window("week", db_path=path)
    daily = dd.query_daily(7, db_path=path)
    assert window.unverified_calls == 2
    assert sum(d.unverified_saved_usd for d in daily) == pytest.approx(window.unverified_saved_usd)


# ── the seat split and the unit on the other two status surfaces (coordinator S2) ─────────────────

def _window(**kw):
    from llm_router import ledger_view as lv

    base = dict(as_of="x", window="7d", source="s", estimated=True, n=100, executed=(("anthropic", 90), ("codex", 5), ("ollama", 5)),
                local_n=5, subscription_n=95, paid_n=0, unclassified_n=0, verified_usd=0.0, unverified_usd=4.92,
                verified_n=0, unverified_n=634, scope="session_id")
    base.update(kw)
    return lv.LedgerWindow(**base)


def test_the_kpi_ledger_line_splits_the_subscription_bucket_by_seat_and_labels_its_n():
    line = _window(subscription_claude_n=90, subscription_other_n=5).line()
    assert "subscription 95 (Claude seat 90 + other seats 5)" in line, line
    assert "est. saved $4.92 (n=634 calls)" in line, line


def test_a_ledger_window_without_the_split_keeps_the_one_bucket():
    """An older cache (or package) has no per-seat figure: the line says the bucket, never a made-up split."""
    line = _window().line()
    assert "subscription 95 /" in line and "Claude seat" not in line, line
    half = _window(subscription_claude_n=90).line()                  # one half unknown: no split either
    assert "subscription 95 /" in half and "Claude seat" not in half, half


def _status_bar():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "src" / "llm_router" / "hooks" / "status-bar.py"
    spec = importlib.util.spec_from_file_location("sb_w8_money", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_status_bar_mix_splits_the_seats_and_says_what_its_n_counts(monkeypatch):
    from llm_router import ledger_view as lv

    hook = _status_bar()
    monkeypatch.setattr(hook, "_ledger_window", lambda _lv, _w: _window(subscription_claude_n=90, subscription_other_n=5))
    assert hook._calls_str(lv) == "claude:90 other:5 free:5 paid:0 n=100 ledger rows, session"
    monkeypatch.setattr(hook, "_ledger_window", lambda _lv, _w: _window(subscription_claude_n=90, subscription_other_n=5, unclassified_n=3))
    assert hook._calls_str(lv).startswith("claude:90 other:5 free:5 paid:0 unc:3 n=100")


def test_the_status_bar_mix_falls_back_to_the_one_bucket_for_a_package_without_the_split(monkeypatch):
    """The installed hook can run against an older package: its figures() has no per-seat entry."""
    from llm_router import ledger_view as lv

    hook = _status_bar()
    old = _window()
    real = type(old).figures

    def old_figures(self):
        figs = real(self)
        figs.pop("mix_subscription_claude"), figs.pop("mix_subscription_other")
        return figs

    monkeypatch.setattr(type(old), "figures", old_figures)
    monkeypatch.setattr(hook, "_ledger_window", lambda _lv, _w: old)
    assert hook._calls_str(lv) == "sub:95 free:5 paid:0 n=100 ledger rows, session"


def test_the_status_bar_money_says_calls(monkeypatch):
    from llm_router import ledger_view as lv

    hook = _status_bar()
    assert hook._usd_seg(lv, lv.Figure(0.26, 44, "today", "x", "s", "estimated", None), "est") == "~$0.26 est n=44 calls"

