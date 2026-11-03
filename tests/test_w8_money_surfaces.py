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
