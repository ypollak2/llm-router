"""D-611/D-613/D-614: ONE saving figure (usage.db + proxy local net + proxy tier), read without scanning the
proxy ledger on the render path.

Fixtures are synthetic and carry no live data. Every figure the tests compare against is computed by
``proxy.ledger`` / ``ledger_view.money_window`` themselves (``summarize`` of a full read), never typed in.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timedelta

import pytest

from llm_router import ledger_view as lv
from llm_router import savings_rollup as sr
from llm_router import statusline_segments as ss
from llm_router import statusline_snapshot as snap

NOW = datetime(2026, 11, 25, 12, 0, 0).timestamp()        # local noon: "today" is 2026-11-25 whatever TZ
LOCAL = "ollama/qwen3-coder:30b"


# ── fixtures ─────────────────────────────────────────────────────────────────────────────────────

def _day_ts(days_ago: int, hour: float) -> float:
    d = datetime.fromtimestamp(NOW).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days_ago)
    return d.timestamp() + hour * 3600


def _cycle(ts: float, session: str, tag: str, *, steps: int = 2, with_keys: bool = True) -> list[dict]:
    """prefix (forwarded) -> a served run of ``steps`` -> next call (forwarded, re-writes the cache) -> a tier row."""
    def row(dt, **kw):
        r = {"ts": ts + dt, "session_id": session, "requested_model": "claude-sonnet-5"}
        if with_keys:
            r["trace_id"] = f"{tag}-{dt}"
            r["counterfactual_cost_usd"] = 0.01
        r.update(kw)
        return r
    rows = [row(0, decision="forwarded", reason="not_eligible", usage={"cache_read_input_tokens": 40_000})]
    for i in range(steps):
        rows.append(row(10 + i, decision="served", step_class="continuation", backend_usage={"output_tokens": 200}))
    rows.append(row(30, decision="forwarded", reason="policy_kept",
                    usage={"cache_creation_input_tokens": 2_000,
                           "cache_creation": {"ephemeral_1h_input_tokens": 2_000, "ephemeral_5m_input_tokens": 0}}))
    rows.append(row(40, decision="forwarded", reason="tier", tier_reason="simple", requested_model="claude-opus-5",
                    served_model="claude-sonnet-5",
                    usage={"input_tokens": 100, "output_tokens": 300, "cache_read_input_tokens": 20_000}))
    return rows


def _ledger(path, days=(5, 4, 3, 2, 1, 0), per_day=3, **kw):
    rows = []
    for d in days:                                           # oldest first: the file is time-ordered
        for k in range(per_day):
            rows += _cycle(_day_ts(d, 9 + k), f"s{d}-{k}", f"d{d}k{k}", **kw)
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return rows


def _db(path, n_today=2, extra_trace: str | None = None):
    con = sqlite3.connect(path)
    con.executescript("""
    CREATE TABLE usage (id INTEGER PRIMARY KEY, timestamp TEXT, model TEXT, provider TEXT, input_tokens INTEGER,
        output_tokens INTEGER, cost_usd REAL, success INTEGER, is_simulated INTEGER, correlation_id TEXT, trace_id TEXT);
    CREATE TABLE routing_decisions (id INTEGER PRIMARY KEY, timestamp TEXT, session_id TEXT, final_provider TEXT,
        reason_code TEXT, provenance TEXT, trace_id TEXT);
    """)
    for i in range(n_today):
        t = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(NOW - 600 - i))
        con.execute("INSERT INTO usage (timestamp, model, provider, input_tokens, output_tokens, cost_usd, success, "
                    "is_simulated, correlation_id) VALUES (?,?,?,?,?,0,1,0,?)",
                    (t, LOCAL, "ollama", 20_000, 1_000, f"c{i}"))
    if extra_trace:
        t = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(NOW - 700))
        con.execute("INSERT INTO routing_decisions (timestamp, session_id, final_provider, reason_code, provenance, "
                    "trace_id) VALUES (?,?,?,?,?,?)", (t, "x", "ollama", "r", "runtime", extra_trace))
    con.commit()
    con.close()


@pytest.fixture
def env(tmp_path):
    p = {"ledger": tmp_path / "proxy_calls.jsonl", "rollup": tmp_path / "savings_rollup.json",
         "db": tmp_path / "usage.db", "home": tmp_path}
    return p


def _sums(roll):
    return {d: r["sum"] for d, r in roll["days"].items() if r["sum"] is not None}


def _build(env, budget=1000.0, now=NOW):
    return sr.refresh(ledger=env["ledger"], rollup=env["rollup"], db_path=env["db"], now=now, budget_s=budget)


def _full_scan(env, since, until):
    """The reference: every row of the file, filtered by ts, summarised once (no seek, no rollup)."""
    rows = []
    for line in open(env["ledger"], encoding="utf-8"):
        r = json.loads(line)
        if since <= r["ts"] <= until:
            rows.append(r)
    return sr.summarize(rows)


# ── the ledger is NOT time-ordered: late rows, shuffled order, resume ──────────────────────────────

def _day_of_rows(env, day):
    lo, hi = sr.day_start(day), sr.day_start(sr.next_day(day))
    return [r for r in map(json.loads, open(env["ledger"])) if lo <= r["ts"] < hi]


def test_a_row_written_13_hours_after_its_day_ended_is_in_that_days_rollup(env):
    _ledger(env["ledger"], days=(3, 2, 1, 0), per_day=2)
    _build(env)
    day = sr.day_of(_day_ts(2, 1))
    before = sr.load(env["rollup"])["days"][day]["sum"]
    late = {"ts": sr.day_start(sr.next_day(day)) - 600, "session_id": "late", "decision": "forwarded",
            "requested_model": "claude-sonnet-5", "reason": "tier", "tier_reason": "simple",
            "served_model": "claude-sonnet-5", "usage": {"input_tokens": 10, "output_tokens": 10}}
    late["requested_model"] = "claude-opus-5"
    with open(env["ledger"], "a", encoding="utf-8") as fh:        # written 13 h after the day ended, after later rows
        fh.write(json.dumps(late) + "\n")
    _build(env, now=sr.day_start(sr.next_day(day)) + 13 * 3600 + 86400)
    got = sr.load(env["rollup"])["days"][day]["sum"]
    assert got["rows"] == before["rows"] + 1 == len(_day_of_rows(env, day))
    assert got == sr.summarize(_day_of_rows(env, day))


def test_rollup_equals_full_scan_on_a_shuffled_ledger(env):
    import random

    rows = []
    for d in (6, 5, 4, 3, 2, 1, 0):
        for k in range(3):
            rows += _cycle(_day_ts(d, 9 + k), f"s{d}-{k}", f"d{d}k{k}")
    random.Random(7).shuffle(rows)
    with open(env["ledger"], "w", encoding="utf-8") as fh:
        fh.writelines(json.dumps(r) + "\n" for r in rows)
    _build(env)
    roll = sr.load(env["rollup"])
    closed = [d for d in roll["days"] if d < sr.day_of(NOW)]
    assert len(closed) == 6
    for d in closed:
        assert roll["days"][d]["sum"] == sr.summarize(_day_of_rows(env, d)), d


def test_resume_across_budgeted_refreshes_equals_a_single_pass(env, tmp_path):
    _ledger(env["ledger"], days=(4, 3, 2, 1, 0), per_day=3)
    one = dict(env, rollup=tmp_path / "one.json")
    sr.refresh(ledger=one["ledger"], rollup=one["rollup"], db_path=env["db"], now=NOW, budget_s=1000.0)
    steps = 0
    while True:
        out = sr.refresh(ledger=env["ledger"], rollup=env["rollup"], db_path=env["db"], now=NOW,
                         budget_s=1000.0, pass_lines=7)
        steps += 1
        if out["caught_up"] and out["missing"] == 0:
            break
        assert steps < 100
    assert steps > 3                                              # it really was spread over several refreshes
    a, b = sr.load(env["rollup"]), sr.load(one["rollup"])
    assert a["days"] == b["days"] and a["offset"] == b["offset"] == env["ledger"].stat().st_size


def test_a_window_is_na_until_the_pass_has_caught_up(env, monkeypatch):
    _ledger(env["ledger"])
    monkeypatch.setattr(sr, "TAIL_MAX_BYTES", 100)                    # a ledger this size counts as "far behind"
    sr.refresh(ledger=env["ledger"], rollup=env["rollup"], db_path=env["db"], now=NOW, pass_lines=5)
    p = sr.proxy_parts(lv.calendar_bounds(30, now=NOW), ledger=env["ledger"], rollup=env["rollup"], db_path=env["db"])
    assert p["available"] is False and "rollup building" in p["na_reason"]
    _build(env)
    p = sr.proxy_parts(lv.calendar_bounds(30, now=NOW), ledger=env["ledger"], rollup=env["rollup"], db_path=env["db"])
    assert p["available"] is True and p["b_runs"] == 18


def test_the_rollup_file_stays_bounded_as_closed_days_accumulate(env, tmp_path):
    sizes = {}
    for per_day in (3, 30):
        e = dict(env, ledger=tmp_path / f"l{per_day}.jsonl", rollup=tmp_path / f"r{per_day}.json")
        _ledger(e["ledger"], days=range(40, -1, -1), per_day=per_day)
        sr.refresh(ledger=e["ledger"], rollup=e["rollup"], db_path=env["db"], now=NOW, budget_s=1000.0)
        roll = sr.load(e["rollup"])
        kept = sum(len(r["offs"]) for r in roll["days"].values() if r["offs"] is not None)
        old = [d for d, r in roll["days"].items() if r["offs"] is None]
        assert len(old) >= 36 and all(roll["days"][d]["sum"] is not None for d in old)
        assert kept <= 5 * per_day * 5 + 5                              # only the youngest days keep offsets
        sizes[per_day] = e["rollup"].stat().st_size
    assert sizes[30] < sizes[3] * 2.5, sizes                            # 10x the rows, nowhere near 10x the file


def test_a_late_row_for_an_old_closed_day_still_updates_it(env):
    _ledger(env["ledger"], days=range(12, -1, -1), per_day=2)
    _build(env)
    day = sr.day_of(_day_ts(10, 1))
    assert sr.load(env["rollup"])["days"][day]["offs"] is None          # old: no offsets kept
    before = sr.load(env["rollup"])["days"][day]["sum"]
    late = {"ts": _day_ts(10, 23), "session_id": "late", "decision": "forwarded", "reason": "tier",
            "tier_reason": "simple", "requested_model": "claude-opus-5", "served_model": "claude-sonnet-5",
            "usage": {"input_tokens": 100, "output_tokens": 300, "cache_read_input_tokens": 20_000}}
    with open(env["ledger"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps(late) + "\n")
    _build(env)
    got = sr.load(env["rollup"])["days"][day]
    assert got["offs"] is None
    assert got["sum"]["rows"] == before["rows"] + 1 and got["sum"]["c_rewritten"] == before["c_rewritten"] + 1
    assert got["sum"] == sr.summarize(_day_of_rows(env, day))             # a tier row is additive: equal to a full scan
    # the window figure sees the same row before the refresher has indexed it (the unindexed tail)
    with open(env["ledger"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps({**late, "ts": late["ts"] + 1}) + "\n")
    p = sr.proxy_parts(lv.calendar_bounds(30, now=NOW), ledger=env["ledger"], rollup=env["rollup"], db_path=env["db"])
    assert p["available"] and p["c_rewritten"] == sr.summarize(
        [r for r in map(json.loads, open(env["ledger"])) if r["ts"] >= sr.day_start(sr.day_of(_day_ts(29, 1)))])["c_rewritten"]


def test_a_late_served_row_for_an_old_day_is_its_own_run(env):
    _ledger(env["ledger"], days=range(12, -1, -1), per_day=2)
    _build(env)
    day = sr.day_of(_day_ts(10, 1))
    roll = sr.load(env["rollup"])
    before, late0 = roll["days"][day]["sum"], roll.get("late_old_rows", 0)
    with open(env["ledger"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": _day_ts(10, 22), "session_id": "late-served", "decision": "served",
                             "step_class": "continuation", "requested_model": "claude-sonnet-5",
                             "backend_usage": {"output_tokens": 200}}) + "\n")
    _build(env)
    roll = sr.load(env["rollup"])
    got = roll["days"][day]["sum"]
    assert roll["days"][day]["offs"] is None
    assert got["b_runs"] == before["b_runs"] + 1 and got["b_steps"] == before["b_steps"] + 1
    assert roll["late_old_rows"] == late0 + 1 == 1
    p = sr.proxy_parts(lv.calendar_bounds(30, now=NOW), ledger=env["ledger"], rollup=env["rollup"], db_path=env["db"])
    assert p["late_old_rows"] == 1


def test_a_same_inode_rewrite_that_grows_past_the_old_size_rebuilds(env):
    _ledger(env["ledger"], days=(2, 1, 0), per_day=2)
    _build(env)
    old = sr.load(env["rollup"])
    lines = open(env["ledger"], encoding="utf-8").read().splitlines(True)
    ino = env["ledger"].stat().st_ino
    second = json.loads(lines[1])
    second["session_id"] = "rewritten-" + "x" * 50
    with open(env["ledger"], "w", encoding="utf-8") as fh:                # same inode, same first row, bigger file
        fh.write(lines[0] + json.dumps(second) + "\n" + "".join(lines[2:]) + "".join(lines))
    assert env["ledger"].stat().st_ino == ino and env["ledger"].stat().st_size > old["size"]
    assert json.loads(open(env["ledger"]).readline())["ts"] == old["first_ts"]
    assert not sr.usable(old, env["ledger"])
    assert _build(env)["reset"] is True


def test_a_lost_lock_reports_caught_up_from_the_file(env):
    from llm_router.file_lock import exclusive_lock

    _ledger(env["ledger"], days=(2, 1, 0), per_day=2)
    _build(env)
    with exclusive_lock(env["rollup"].with_name(env["rollup"].name + ".lock"), timeout=0.0):
        out = _build(env)
    assert out["caught_up"] is True and out["built"] == 0


# ── (d) rollup == full scan for closed days; incremental append ──────────────────────────────────

def test_rollup_equals_full_scan_for_every_closed_day(env):
    _ledger(env["ledger"])
    _db(env["db"])
    out = _build(env)
    roll = sr.load(env["rollup"])
    assert out["missing"] == 0 and out["caught_up"] and len(_sums(roll)) == 5     # days 5..1; today is open
    for d in range(5, 0, -1):
        day = sr.day_of(_day_ts(d, 1))
        lo, hi = sr.day_start(day), sr.day_start(sr.next_day(day))
        want = sr.summarize([r for r in map(json.loads, open(env["ledger"])) if lo <= r["ts"] < hi])
        assert roll["days"][day]["sum"] == want, day
        assert want["b_runs"] == 3 and want["c_rewritten"] == 3          # not an empty comparison


def test_a_new_closed_day_is_appended_and_old_days_are_not_rewritten(env):
    _ledger(env["ledger"], days=(3, 2, 1, 0))
    _build(env)
    before = sr.load(env["rollup"])
    later = NOW + 86400
    with open(env["ledger"], "a", encoding="utf-8") as fh:
        for r in _cycle(sr.midnight(later) + 3600, "new", "n"):
            fh.write(json.dumps(r) + "\n")
    sr.refresh(ledger=env["ledger"], rollup=env["rollup"], db_path=env["db"], now=later, budget_s=100.0)
    after = sr.load(env["rollup"])
    today_key = sr.day_of(NOW)
    assert before["days"][today_key]["sum"] is None and after["days"][today_key]["sum"] is not None  # NOW's day closed
    for k, v in before["days"].items():
        if k != today_key:
            assert after["days"][k]["sum"] == v["sum"]                  # nothing new for them: untouched


def test_history_is_na_until_the_rollup_is_built_then_whole_never_partial(env, monkeypatch):
    monkeypatch.setattr(sr, "TAIL_MAX_BYTES", 100)
    _ledger(env["ledger"])
    _db(env["db"])
    b = lv.calendar_bounds(30, now=NOW)
    fig, parts = lv.combined_saved(b, db_path=env["db"], proxy_path=env["ledger"], rollup_path=env["rollup"])
    assert fig.value is None and "rollup building" in fig.na_reason
    assert lv.render_saved(fig, parts, short=True).startswith("30d n/a (rollup building")
    sr.refresh(ledger=env["ledger"], rollup=env["rollup"], db_path=env["db"], now=NOW, budget_s=0.0, pass_lines=1000)
    assert len(_sums(sr.load(env["rollup"]))) == 1                      # one summary (newest first), then stop
    fig, _ = lv.combined_saved(b, db_path=env["db"], proxy_path=env["ledger"], rollup_path=env["rollup"])
    assert fig.value is None                                            # 1 of 5 days: still n/a, not a partial total
    _build(env)
    fig, _ = lv.combined_saved(b, db_path=env["db"], proxy_path=env["ledger"], rollup_path=env["rollup"])
    assert fig.value is not None


# ── (e') rotated / truncated ledger forces a rebuild ─────────────────────────────────────────────

def test_rotated_ledger_rebuilds(env):
    _ledger(env["ledger"], days=(3, 2, 1, 0))
    _build(env)
    old = sr.load(env["rollup"])
    new = env["ledger"].with_name("rotated.jsonl")
    _ledger(new, days=(2, 1, 0), per_day=1)                             # a different, smaller file
    os.replace(new, env["ledger"])                                      # new inode
    assert not sr.usable(old, env["ledger"])
    out = _build(env)
    assert out["reset"] is True
    roll = sr.load(env["rollup"])
    assert len(_sums(roll)) == 2 and all(v["b_runs"] == 1 for v in _sums(roll).values())


def test_truncated_ledger_rebuilds(env):
    _ledger(env["ledger"], days=(3, 2, 1, 0))
    _build(env)
    lines = open(env["ledger"], encoding="utf-8").read().splitlines(True)
    with open(env["ledger"], "w", encoding="utf-8") as fh:              # same inode, shorter
        fh.writelines(lines[len(lines) // 2:])
    assert not sr.usable(sr.load(env["rollup"]), env["ledger"])
    assert _build(env)["reset"] is True


# ── (f) a run spanning midnight is split across the two days ────────────────────────────────────

def test_a_run_spanning_midnight_is_split_by_day(env):
    mid = sr.midnight(_day_ts(1, 12))                                   # midnight between day 2 and day 1
    rows = [
        {"ts": mid - 30, "session_id": "m", "decision": "forwarded", "requested_model": "claude-sonnet-5",
         "usage": {"cache_read_input_tokens": 40_000}},
        {"ts": mid - 5, "session_id": "m", "decision": "served", "step_class": "continuation",
         "requested_model": "claude-sonnet-5", "backend_usage": {"output_tokens": 200}},
        {"ts": mid + 5, "session_id": "m", "decision": "served", "step_class": "continuation",
         "requested_model": "claude-sonnet-5", "backend_usage": {"output_tokens": 200}},
    ]
    with open(env["ledger"], "w", encoding="utf-8") as fh:
        fh.writelines(json.dumps(r) + "\n" for r in rows)
    whole = sr.summarize(rows)
    _build(env)
    roll = sr.load(env["rollup"])
    d_before, d_after = sr.day_of(mid - 5), sr.day_of(mid + 5)
    assert whole["b_runs"] == 1
    assert roll["days"][d_before]["sum"]["b_runs"] == 1 and roll["days"][d_after]["sum"]["b_runs"] == 1   # +1 run


# ── (a) combined = A + B + C, each with its n ────────────────────────────────────────────────────

def test_combined_is_a_plus_b_plus_c_with_each_n(env):
    _ledger(env["ledger"])
    _db(env["db"], n_today=2)
    _build(env)
    b = lv.resolve("today", now=NOW)
    fig, p = lv.combined_saved(b, db_path=env["db"], proxy_path=env["ledger"], rollup_path=env["rollup"])
    mw = lv.money_window(b, db_path=env["db"])
    ref = _full_scan(env, sr.midnight(NOW), NOW)
    assert mw.unverified_calls == 2 and mw.unverified_usd > 0
    assert ref["b_steps"] == 6 and ref["c_rewritten"] == 3
    assert p["A"]["n"] == 2 and p["B"]["n"] == 6 and p["B"]["runs"] == 3 and p["C"]["n"] == 3
    assert fig.n == 2 + 6 + 3 and fig.kind == "estimated" and fig.window == "today"
    assert fig.value == pytest.approx(mw.unverified_usd + ref["b_net"] + ref["c_usd"], abs=1e-5)
    assert p["A"]["usd"] == pytest.approx(mw.unverified_usd, abs=1e-5) and p["C"]["usd"] == pytest.approx(ref["c_usd"])


def test_a_30_day_window_is_rollup_days_plus_today(env):
    _ledger(env["ledger"])
    _db(env["db"])
    _build(env)
    fig, p = lv.combined_saved(lv.calendar_bounds(30, now=NOW), db_path=env["db"], proxy_path=env["ledger"],
                               rollup_path=env["rollup"])
    ref = _full_scan(env, 0, NOW)
    assert p["B"]["runs"] == ref["b_runs"] == 18 and p["B"]["usd"] == pytest.approx(ref["b_net"], abs=1e-5)
    assert p["C"]["n"] == ref["c_rewritten"] == 18


def test_unpriced_runs_and_tier_rows_are_counted_as_excluded_not_guessed(env):
    rows = _cycle(_day_ts(0, 9), "s", "t")
    rows += [{"ts": _day_ts(0, 10), "session_id": "u", "decision": "served", "step_class": "continuation",
              "requested_model": "no-such-model", "backend_usage": {"output_tokens": 5}}]
    with open(env["ledger"], "w", encoding="utf-8") as fh:
        fh.writelines(json.dumps(r) + "\n" for r in rows)
    got = sr.summarize(rows)
    assert got["b_unpriced"] == 1 and got["b_runs"] == 1


# ── (b) trace_id dedupe ──────────────────────────────────────────────────────────────────────────

def test_a_proxy_row_already_in_usage_db_is_counted_once(env):
    rows = _ledger(env["ledger"], days=(0,), per_day=1)
    served = [r for r in rows if r["decision"] == "served"][0]
    _db(env["db"], n_today=0, extra_trace=served["trace_id"])
    b = lv.resolve("today", now=NOW)
    _, with_dup = lv.combined_saved(b, db_path=env["db"], proxy_path=env["ledger"], rollup_path=env["rollup"])
    _db_path2 = env["home"] / "empty.db"
    _db(_db_path2, n_today=0)
    _, without = lv.combined_saved(b, db_path=_db_path2, proxy_path=env["ledger"], rollup_path=env["rollup"])
    assert with_dup["dedup_dropped"] == 1 and without["dedup_dropped"] == 0
    assert with_dup["B"]["n"] == without["B"]["n"] - 1


# ── (c) net <= 0 prints n/a with its parts, never ~$0 ────────────────────────────────────────────

def _fig(value, n=5, window="7d"):
    return lv.Figure(value, n, window, "t", "s", "estimated")


def _parts(a, b, c, window="7d"):
    return {"window": window, "A": {"usd": a, "n": 4, "runs": 0}, "B": {"usd": b, "n": 53, "runs": 38},
            "C": {"usd": c, "n": 0}}


def test_net_not_positive_prints_na_with_local_and_other_never_zero():
    text = lv.render_saved(_fig(-9.27), _parts(2.83, -12.41, 0.31))
    assert text == "saved n/a (net <= 0: local -$12.41, other +$3.14, 7d)"
    assert "~$0" not in text and "9.27" not in text
    zero = lv.render_saved(_fig(0.0), _parts(1.0, -1.0, 0.0))
    assert zero.startswith("saved n/a (net <= 0") and "~$0" not in zero
    short = lv.render_saved(_fig(-12.15, window="30d"), _parts(37.28, -68.34, 18.91, "30d"), short=True)
    assert short == "30d n/a (net <= 0: local -$68.34, other +$56.19)"


def test_positive_text_has_the_four_counts_and_the_window():
    parts = _parts(0.014, 1.08, 0.0, "today")
    assert lv.render_saved(_fig(1.094, 95, "today"), parts) == \
        "saved ~$1.09 est. (n=95: 4 hook/MCP · 53 local steps/38 runs · 0 tier, today)"
    assert lv.render_saved(_fig(18.0, 9, "30d"), _parts(1, 2, 3, "30d"), short=True) == "~$18.00 est. 30d"


# ── (g) one number: the router line and the money segment ────────────────────────────────────────

def _state(env):
    _ledger(env["ledger"])
    _db(env["db"])
    _build(env)
    return str(env["home"])


def test_render_line_and_money_segment_print_the_same_number(env, monkeypatch):
    state = _state(env)
    monkeypatch.setenv("LLM_ROUTER_HOME", state)
    seg = ss.money_segment(state, NOW)
    field = snap.money_field(NOW)
    assert seg["money_win"] == "today" and "money" in seg
    assert seg["money"].split(" ")[0] in field["text"]                  # the same "~$X.XX"
    assert f"n={int(seg['money_n']):,}" in field["text"]
    line = snap.render_line({"router": {"model": None, "reason": None, "ts": None},
                             "offload": {"value": None, "n": 0, "window": "today"},
                             "quota": {"claude": {"pct": None, "n": 0, "age_s": None, "window": "5h"},
                                       "codex": {"used": None, "budget": None, "window": "5h"}},
                             "quality": {"n": 0, "k": 0, "rate": None, "wilson": None, "window": "7d"},
                             "saved": None, "money": field})
    assert field["text"] in line


def test_work_hours_are_appended_only_when_measured(env):
    base = {"router": {"model": None, "reason": None, "ts": None},
            "offload": {"value": None, "n": 0, "window": "today"},
            "quota": {"claude": {"pct": None, "n": 0, "age_s": None, "window": "5h"},
                      "codex": {"used": None, "budget": None, "window": "5h"}},
            "quality": {"n": 0, "k": 0, "rate": None, "wilson": None, "window": "7d"},
            "money": {"text": "saved ~$1.09 est. (n=95: 4 hook/MCP · 53 local steps/38 runs · 0 tier, today)"}}
    zero = lv.Figure(None, 0, "today", "t", "s", "estimated", "n=0 turns off Claude, today")
    line = snap.render_line({**base, "saved": zero.to_dict()})
    assert "turns off Claude" not in line and line.endswith("0 tier, today)")
    measured = lv.Figure(0.4, 3, "today", "t", "s", "estimated", None, 12)
    assert "work-hours" in snap.render_line({**base, "saved": measured.to_dict()})


# ── (i) old rows without the estimator fields do not change B ────────────────────────────────────

def test_rows_without_counterfactual_or_trace_id_price_the_same(env):
    with_keys = _cycle(_day_ts(0, 9), "s", "t", with_keys=True)
    without = _cycle(_day_ts(0, 9), "s", "t", with_keys=False)
    assert all("trace_id" not in r and "counterfactual_cost_usd" not in r for r in without)
    a, b = sr.summarize(with_keys), sr.summarize(without)
    assert a["b_net"] == b["b_net"] and a["b_runs"] == b["b_runs"] == 1 and a["c_usd"] == b["c_usd"]


# ── (h) the render path reads a bounded amount ───────────────────────────────────────────────────

def test_warm_rollup_window_figure_reads_only_today_and_the_tail(env, monkeypatch):
    _ledger(env["ledger"], days=range(40, -1, -1), per_day=6)
    _db(env["db"])
    _build(env)
    total = sum(1 for _ in open(env["ledger"]))
    read: list[int] = []
    real = sr.rows_at

    def spy(path, offsets):
        read.append(len(offsets))
        return real(path, offsets)

    monkeypatch.setattr(sr, "rows_at", spy)
    fig, _ = lv.combined_saved(lv.calendar_bounds(30, now=NOW), db_path=env["db"], proxy_path=env["ledger"],
                               rollup_path=env["rollup"])
    assert fig.value is not None and read                               # something was read, and it was today only
    assert sum(read) <= total / 30, (read, total)


# ── the history command ──────────────────────────────────────────────────────────────────────────

def test_savings_history_lists_every_source_with_start_n_and_excluded(env, monkeypatch):
    _state(env)
    monkeypatch.setenv("LLM_ROUTER_HOME", str(env["home"]))
    from llm_router.commands import savings_report

    text = savings_report.render_history(NOW)
    for needle in ("== today ==", "== 7d ==", "== 30d ==", "== since earliest row ==", "A hook/MCP/delegation",
                   "B proxy local-served", "C proxy tier rewrite", "unpriced/excluded:", "first row:", "combined"):
        assert needle in text, needle
