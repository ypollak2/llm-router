"""W8 (owner decision D-364): the mix and the money segment stop mixing buckets and counting rows.

* ``⚖ L/S/P`` lumped the Claude seat (what the router wants to spend LESS of) with Codex (MORE of):
  S is now the Claude seat and C every other seat (``docs/bugs/MIXSEAT-1.md``).
* ``today n=`` summed rows over three overlapping tables, so one local call counted three times and the
  dollars with it: a call is counted once, by an id where both rows carry one, else by model + tokens +
  time (``docs/bugs/MONEY3X-1.md``).
* every n is printed with its unit and window.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from llm_router import ledger_view as lv
from llm_router import pricing, seats as seats_mod, statusline_segments as ss

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "src" / "llm_router" / "hooks" / "statusline-command.sh"
NOW = 1_791_234_000.0
LOCAL = "ollama/qwen3-coder:30b"


# ── the seat split ───────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("provider,auth,reason,seat", [
    ("anthropic", "oauth", None, "claude"),            # a proxy row forwarded on the Claude Code seat
    ("anthropic", None, None, "claude"),               # a router row for the Claude provider
    ("claude", None, None, "claude"),
    ("subscription", None, None, "claude"),            # the usage table's own word for the Claude Code seat
    ("openai", None, "claude_code_subscription", "claude"),
    ("codex", None, None, "other"),
    ("gemini_cli", None, None, "other"),
])
def test_subscription_seat_names_the_seat_a_subscription_call_ran_on(provider, auth, reason, seat):
    assert lv.subscription_seat(provider, auth=auth, reason=reason) == seat


def _seats(*, codex=False, claude=True):
    return seats_mod.Seats(claude=seats_mod.Seat(kind="claude.ai", plan="max") if claude else seats_mod.Seat(),
                           codex=seats_mod.Seat(kind="chatgpt", plan="plus") if codex else seats_mod.Seat(),
                           api_keys={}, detected_at=lv._iso(NOW - 3600))


ROWS = [("anthropic", "oauth", None)] * 24 + [("anthropic", "oauth", None)] + [("codex", None, None)] * 3 \
    + [("ollama", None, None)] * 4 + [("openai", None, None)]


def test_codex_and_claude_never_share_a_bucket():
    s = _seats(codex=True)
    split = lv.split_subscription(ROWS, s, now=NOW)
    assert split == {"claude": 25, "other": 3}
    mix = lv.classify_rows(ROWS, s, now=NOW)
    assert mix["subscription"] == split["claude"] + split["other"]      # the same rows, the same class
    assert (mix["local"], mix["paid"], mix["unclassified"]) == (4, 0, 1)


def test_a_codex_call_without_a_codex_seat_is_unclassified_not_a_claude_call():
    s = _seats(codex=False)
    assert lv.split_subscription(ROWS, s, now=NOW) == {"claude": 25, "other": 0}
    assert lv.classify_rows(ROWS, s, now=NOW)["unclassified"] == 4


def test_no_rows_is_no_data_not_zero():
    assert lv.split_subscription(None) is None and lv.classify_rows(None) is None


def _db(tmp_path):
    path = tmp_path / "usage.db"
    con = sqlite3.connect(path)
    con.executescript("""
    CREATE TABLE usage (id INTEGER PRIMARY KEY, timestamp TEXT, model TEXT, provider TEXT, input_tokens INTEGER,
        output_tokens INTEGER, cost_usd REAL, success INTEGER, is_simulated INTEGER, correlation_id TEXT);
    CREATE TABLE claude_usage (id INTEGER PRIMARY KEY, timestamp TEXT, model TEXT, tokens_used INTEGER,
        input_tokens INTEGER, output_tokens INTEGER, cost_saved_usd REAL, is_simulated INTEGER);
    CREATE TABLE savings_stats (id INTEGER PRIMARY KEY, timestamp TEXT, session_id TEXT, task_type TEXT,
        estimated_claude_cost_saved REAL, model_used TEXT, host TEXT, input_tokens INTEGER, output_tokens INTEGER,
        mode TEXT, is_simulated INTEGER);
    CREATE TABLE routing_decisions (id INTEGER PRIMARY KEY, timestamp TEXT, session_id TEXT, final_provider TEXT,
        reason_code TEXT, provenance TEXT, trace_id TEXT);
    """)
    con.commit()
    return path, con


def _stamp(sec):                          # the tables' own spellings
    t = time.gmtime(NOW - 3600 + sec)
    return time.strftime("%Y-%m-%d %H:%M:%S", t), time.strftime("%Y-%m-%dT%H:%M:%S.123456+00:00", t)


def test_the_session_mix_in_the_ledger_window_carries_the_split(tmp_path):
    path, con = _db(tmp_path)
    for i, (prov, auth) in enumerate([("anthropic", "oauth")] * 3 + [("codex", None)] * 2 + [("ollama", None)]):
        con.execute("INSERT INTO routing_decisions (timestamp, session_id, final_provider, reason_code, provenance) "
                    "VALUES (?,?,?,?,'runtime')", (_stamp(i)[0], "s1", prov, "router_unhinted"))
    con.commit()
    lw = lv.window_summary("session", now=NOW, session_id="s1", db_path=path, proxy_path=tmp_path / "none.jsonl",
                           seats=_seats(codex=True))
    assert (lw.local_n, lw.subscription_n, lw.subscription_claude_n, lw.subscription_other_n) == (1, 5, 3, 2)
    figs = lw.figures()
    assert lv.show(figs["mix_subscription_claude"], "{:.0f}") == "3" and lv.show(figs["mix_subscription_other"], "{:.0f}") == "2"
    back = lv.LedgerWindow.from_json(lw.to_json())                       # the cache round trip keeps the split
    assert (back.subscription_claude_n, back.subscription_other_n) == (3, 2)
    old = json.loads(lw.to_json())
    old.pop("subscription_claude_n"), old.pop("subscription_other_n")     # a cache written before W8
    assert lv.LedgerWindow.from_json(json.dumps(old)).figures()["mix_subscription_claude"].value is None


def test_the_segments_write_the_split_and_keep_mix_sub_as_every_seat(tmp_path, monkeypatch):
    path, con = _db(tmp_path)
    for i, prov in enumerate(["anthropic"] * 3 + ["codex"] * 2):
        con.execute("INSERT INTO routing_decisions (timestamp, session_id, final_provider, reason_code, provenance) "
                    "VALUES (?,?,?,?,'runtime')", (_stamp(i)[0], "s1", prov, "router_unhinted"))
    con.commit()
    con.close()
    monkeypatch.setattr(lv, "_load_seats", lambda: _seats(codex=True))
    mix = ss.mix_segment(str(tmp_path), NOW, "s1")
    assert (mix["mix_sub"], mix["mix_claude"], mix["mix_other"]) == ("5", "3", "2")     # an older script reads mix_sub
    assert int(mix["mix_claude"]) + int(mix["mix_other"]) == int(mix["mix_sub"])


# ── the money: one call, once ────────────────────────────────────────────────────────────────────

def _call(con, i, *, model=LOCAL, tin=1000, tout=50, usage=True, claude=True, savings=True, skew=0, cid=None, host="router"):
    """One call recorded in some of the three tables, the way the router records a local call."""
    u, s = _stamp(i * 100)
    _, s_skewed = _stamp(i * 100 + skew)
    cid = cid or f"c{i:07d}"
    saved = 0.001 * (i + 1)
    if usage:
        con.execute("INSERT INTO usage (timestamp, model, provider, input_tokens, output_tokens, cost_usd, success, "
                    "is_simulated, correlation_id) VALUES (?,?,?,?,?,0,1,0,?)", (u, model, model.split("/")[0], tin, tout, cid))
    if claude:
        con.execute("INSERT INTO claude_usage (timestamp, model, tokens_used, input_tokens, output_tokens, cost_saved_usd, "
                    "is_simulated) VALUES (?,?,?,?,?,?,0)", (u, model, tin + tout, tin, tout, saved))
    if savings:
        con.execute("INSERT INTO savings_stats (timestamp, session_id, task_type, estimated_claude_cost_saved, model_used, host, "
                    "input_tokens, output_tokens, mode, is_simulated) VALUES (?,?,?,?,?,?,?,?,NULL,0)",
                    (s_skewed, cid, "query", saved, model, host, tin, tout))
    return saved


def _window(path):
    return lv.money_window(lv.resolve("lifetime", now=NOW), db_path=path)


def _usage_saved(tin, tout):
    base = pricing.savings_baseline_model()
    return (tin * pricing.input_rate(base) + tout * pricing.output_rate(base)) / 1_000_000


def test_one_local_call_in_three_tables_is_one_call_and_its_dollars_count_once(tmp_path):
    path, con = _db(tmp_path)
    for i in range(5):
        _call(con, i, skew=1)                          # savings_stats stamps the call one second later
    con.commit()
    m = _window(path)
    assert m.unverified_calls == 5 and m.duplicate_calls == 10
    assert m.unverified_usd == pytest.approx(5 * _usage_saved(1000, 50))
    assert m.calls == 15 and m.by_source["claude_usage"]["calls"] == 5     # the raw rows are still shown per table


def test_the_call_id_wins_over_the_clock(tmp_path):
    """usage.correlation_id is written into savings_stats.session_id: ten minutes apart, still one call."""
    path, con = _db(tmp_path)
    _call(con, 0, claude=False, skew=600)
    con.commit()
    assert _window(path).unverified_calls == 1


def test_without_an_id_the_same_model_tokens_and_time_make_one_call(tmp_path):
    path, con = _db(tmp_path)
    _call(con, 0, cid="")                              # an empty id: no id on either row
    con.execute("UPDATE savings_stats SET session_id = NULL")
    con.execute("UPDATE usage SET correlation_id = NULL")
    con.commit()
    assert _window(path).unverified_calls == 1
    con.execute("UPDATE savings_stats SET input_tokens = 999")      # a different token count is another call
    con.commit()
    assert _window(path).unverified_calls == 2


def test_distinct_calls_with_the_same_model_and_tokens_a_minute_apart_stay_two(tmp_path):
    path, con = _db(tmp_path)
    _call(con, 0, claude=False, savings=False)
    u, _ = _stamp(60)
    con.execute("INSERT INTO claude_usage (timestamp, model, tokens_used, input_tokens, output_tokens, cost_saved_usd, "
                "is_simulated) VALUES (?,?,?,?,?,0.002,0)", (u, LOCAL, 1050, 1000, 50))
    con.commit()
    m = _window(path)
    assert m.unverified_calls == 2 and m.duplicate_calls == 0


def test_two_identical_rows_of_one_table_are_two_calls(tmp_path):
    """A retry with identical tokens is two calls: a call is never merged with its own table."""
    path, con = _db(tmp_path)
    _call(con, 0, claude=False, savings=False, cid="aaaa0001")
    _call(con, 0, claude=False, savings=False, cid="aaaa0002")
    con.commit()
    assert _window(path).unverified_calls == 2


def test_matching_is_one_to_one_so_one_extra_row_removes_one_not_three(tmp_path):
    path, con = _db(tmp_path)
    for n in range(3):
        _call(con, 0, claude=False, savings=False, cid=f"bbbb000{n}")
    _call(con, 0, usage=False, savings=False)           # one claude_usage row for those identical calls
    con.commit()
    m = _window(path)
    assert (m.unverified_calls, m.duplicate_calls) == (3, 1)


def test_a_call_only_in_savings_stats_and_claude_usage_counts_savings_stats_dollars_once(tmp_path):
    path, con = _db(tmp_path)
    saved = _call(con, 2, usage=False)
    con.commit()
    m = _window(path)
    assert m.unverified_calls == 1 and m.unverified_usd == pytest.approx(saved)


def test_a_verified_savings_row_is_neither_deduped_nor_deduping(tmp_path):
    from llm_router.savings import REALIZED_GATE_SINCE, REALIZED_MODE

    path, con = _db(tmp_path)
    _call(con, 0, claude=False, savings=False)
    con.execute("INSERT INTO savings_stats (timestamp, session_id, task_type, estimated_claude_cost_saved, model_used, host, "
                "input_tokens, output_tokens, mode, is_simulated) VALUES (?,?,?,?,?,?,?,?,?,0)",
                (REALIZED_GATE_SINCE[:10] + "T23:00:00+00:00", "s", "code", 0.5, LOCAL, "claude_code", 1000, 50, REALIZED_MODE))
    con.commit()
    big = lv.money_window(lv.resolve("lifetime", now=time.time()), db_path=path)
    assert big.verified_calls == 1 and big.unverified_calls == 1 and big.duplicate_calls == 0


def test_simulated_rows_are_still_left_out_and_a_table_without_a_model_column_does_not_break_the_sum(tmp_path):
    path, con = _db(tmp_path)
    _call(con, 0)
    con.execute("UPDATE claude_usage SET is_simulated = 1")                  # test traffic is not a call anyone made
    con.execute("CREATE TABLE codex_usage (id INTEGER PRIMARY KEY, timestamp TEXT, tokens_used INTEGER, "
                "cost_saved_usd REAL, is_simulated INTEGER)")               # an old platform table: no model, no tokens split
    con.execute("INSERT INTO codex_usage (timestamp, tokens_used, cost_saved_usd, is_simulated) VALUES (?,?,?,0)",
                (_stamp(500)[0], 100, 0.25))
    con.commit()
    m = _window(path)
    assert m.unverified_calls == 2 and m.duplicate_calls == 1       # the usage/savings pair is one; the codex row is another


def test_duplicate_calls_on_a_handmade_list():
    cr = lv.CallRow
    rows = [cr("usage", 100.0, "m", 1, 1, "id1", 0.0), cr("savings_stats", 400.0, "m", 1, 1, "id1", 0.5),
            cr("claude_usage", 101.5, "m", 1, 1, None, 0.5), cr("claude_usage", 101.9, "m", 1, 1, None, 0.5),
            cr("codex_usage", None, "m", 1, 1, None, 0.1)]                      # no stamp, no id: cannot be matched
    gone = lv.duplicate_calls(rows)
    assert [(g.table, g.usd) for g in gone] == [("savings_stats", 0.5), ("claude_usage", 0.5)]   # one claude row absorbed
    assert lv.CALL_MATCH_WINDOW_S == 2.0


def test_the_ledger_window_prints_calls_not_rows(tmp_path):
    path, con = _db(tmp_path)
    for i in range(4):
        _call(con, i)
    con.commit()
    lw = lv.window_summary("lifetime", now=NOW, db_path=path, proxy_path=tmp_path / "none.jsonl", seats=_seats())
    assert lw.unverified_n == 4 and lw.figures()["saved_estimated"].n == 4


# ── the script prints it, labelled ───────────────────────────────────────────────────────────────

def _render(tmp_path, kv: dict) -> list[str]:
    home = tmp_path / "home"
    state = home / ".llm-router"
    state.mkdir(parents=True)
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    shutil.copy2(SCRIPT, hooks / "statusline-command.sh")
    shutil.copy2(REPO / "src" / "llm_router" / "statusline_tick.py", hooks / "llm_router_statusline_tick.py")
    shutil.copy2(REPO / "src" / "llm_router" / "statusline_segments.py", hooks / "llm_router_statusline_segments.py")
    sid = "sess-w8"
    body = {"v": "1", "written": str(int(time.time())), "health": "ok", **kv}
    (state / f"statusline_seg_{sid}.kv").write_text("".join(f"{k}={v}\n" for k, v in body.items()))
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "TERM": "dumb", "NO_COLOR": "1",
           "LLM_ROUTER_ENFORCE": "smart"}
    out = subprocess.run(["bash", str(hooks / "statusline-command.sh")], input=json.dumps({"session_id": sid}), env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout.splitlines()


def _segments(line: str) -> dict:
    return {seg.split(" ", 1)[0]: seg for seg in line.split(" · ")}


MIX = {"mix_local": "4", "mix_sub": "27", "mix_paid": "0", "mix_n": "29", "mix_win": "session"}


def test_the_script_prints_the_claude_seat_apart_from_the_others_with_the_coverage(tmp_path):
    line = _render(tmp_path, {**MIX, "mix_claude": "25", "mix_other": "2", "mix_cover": "6871"})[0]
    assert _segments(line)["⚖"] == "⚖ 4L/25S/2C/0P·session ledger rows 29 of ~6,900 (0.4%)"


def test_an_older_cache_without_the_split_prints_the_old_line_labelled(tmp_path):
    line = _render(tmp_path, MIX)[0]
    assert _segments(line)["⚖"] == "⚖ 4L/27S/0P·session ledger rows 29"


@pytest.mark.parametrize("cover,tail", [
    ("999", " of ~999 (2.9%)"), ("1050", " of ~1,100 (2.8%)"), ("1234567", " of ~1,234,600 (<0.1%)"),
    ("100000", " of ~100,000 (<0.1%)"), ("29", " of ~29 (100.0%)"),
])
def test_the_coverage_is_rounded_above_a_thousand_and_a_tiny_share_is_not_zero(tmp_path, cover, tail):
    line = _render(tmp_path, {**MIX, "mix_claude": "25", "mix_other": "2", "mix_cover": cover})[0]
    assert _segments(line)["⚖"].endswith("ledger rows 29" + tail), line


def test_a_cover_smaller_than_the_rows_is_not_printed(tmp_path):
    """More ledger rows than calls is a window mismatch, not a coverage: say nothing rather than 400%."""
    line = _render(tmp_path, {**MIX, "mix_claude": "25", "mix_other": "2", "mix_cover": "10"})[0]
    assert _segments(line)["⚖"] == "⚖ 4L/25S/2C/0P·session ledger rows 29"


def test_a_cache_value_is_data_never_code(tmp_path):
    line = _render(tmp_path, {**MIX, "mix_claude": "a[$(touch PWNED)]", "mix_other": "2", "mix_cover": "$(touch PWNED)"})[0]
    assert not (tmp_path / "PWNED").exists() and not Path("PWNED").exists()
    assert _segments(line)["⚖"] == "⚖ 4L/27S/0P·session ledger rows 29"      # the bad pair hides the split


def test_money_says_calls_and_its_window(tmp_path):
    line = _render(tmp_path, {"money": "~$0.20 est. saved", "money_n": "36", "money_win": "today",
                              "money_m": "$0.50 verified saved", "money_m_n": "1"})[0]
    segs = line.split(" · ")
    assert "\U0001f4b0 ~$0.20 est. saved·today n=36 calls" in segs
    assert "\U0001f4b5 $0.50 verified saved·today n=1 calls" in segs


def test_the_last_route_segment_labels_its_n_as_ledger_rows(tmp_path):
    state_extra = {**MIX, "last": "analyze", "last_stale": "0"}
    line = _render(tmp_path, state_extra)[0]
    assert "(n=29 ledger rows, session)" in line


def test_the_coverage_helper_reads_only_a_fresh_cache_with_the_session(tmp_path):
    cache = tmp_path / "statusline_cache.json"
    cache.write_text(json.dumps({"written_at": NOW - 5, "call_mix": {"sessions": {"s1": 6871, "s2": 0, "s3": True, "s4": "9"}}}))
    assert ss._session_calls(str(tmp_path), "s1", NOW) == 6871
    assert [ss._session_calls(str(tmp_path), s, NOW) for s in ("s2", "s3", "s4", "nope", "", "default")] == [None] * 6
    assert ss._session_calls(str(tmp_path), "s1", NOW + 3600) is None            # a count from an hour ago is not a count
    cache.write_text("not json")
    assert ss._session_calls(str(tmp_path), "s1", NOW) is None
