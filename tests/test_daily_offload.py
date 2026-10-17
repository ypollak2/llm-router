"""P3.3: daily offload rows and the 14-day streak. Injected fixture ledgers, tmp home, fixed clock.
No prompt text anywhere: rows hold ids, models and counts."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone

import pytest

from llm_router import daily_offload as do
from llm_router import offload_share as osh
from llm_router.commands import kpi

from tests._o3_fixture import proxy_row

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc).timestamp()   # last closed UTC day: 2026-10-19
LAST = date(2026, 10, 19)


def _day(i: int) -> date:
    """day 1 = the oldest of 16 days, day 16 = LAST."""
    return LAST - timedelta(days=16 - i)


def _audit(ts):
    return {"ts": ts, "door": "proxy", "served_class": "haiku", "task_group": "qa", "kind": "audit", "n": 30,
            "acceptable": 28, "source": "fixture"}


QUALITY = [_audit(NOW - 25 * 86400.0)]


def _kind_of(sid, stamp):
    return {"org": "organic", "syn": "research", "res": "research", "har": "harness"}.get(str(sid)[:3])


_counter = [0]


def day_rows(day: date, *, prefix="org", n=36, sessions=12, tier="haiku", hour=1):
    """n organic-style proxy rows on ``day``: one turn_first row per session, the rest continuations."""
    base = do.day_bounds(day)[0] + hour * 3600
    rows = []
    for i in range(n):
        _counter[0] += 1
        j = i % sessions
        rows.append(proxy_row(_counter[0], sid=f"{prefix}-{day}-{j}", ts=base + i, tier=tier,
                              step="turn_first" if i < sessions else "continuation", msg_id=f"m{_counter[0]}",
                              **{"tier_task_type": "query"}))
    return rows


def inputs(rows, quality=QUALITY):
    return do.Inputs(rows, [], [], [], quality, _kind_of)


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(h / "state"))
    monkeypatch.delenv("LLM_ROUTER_DB_PATH", raising=False)
    monkeypatch.setattr(do, "_now", lambda: NOW)
    return h


def fill(days_rows, *, now=NOW, **kw):
    """compute and write one row per (day, rows) pair; returns the written paths."""
    out = {}
    for d, rows in days_rows:
        row = do.compute_day(d, inputs(rows), now=now, code_rev="t", **kw)
        out[d] = do.write_day(row)
    return out


def sixteen(skip=None, small=None):
    pairs = []
    for i in range(1, 17):
        if i == skip:
            continue
        pairs.append((_day(i), day_rows(_day(i), n=29 if i == small else 36)))
    return pairs


# ------------------------------------------------------------------ constants and seeds

def test_constants_pinned():
    assert (do.MIN_ACTIONS_DAY, do.MIN_TURNS_DAY, do.STREAK_REQUIRED, do.BOOT_B) == (30, 10, 14, 2000)
    assert do.TARGET == osh.TARGET


def test_seed_is_derived_from_date_not_clock(monkeypatch):
    s = do.seed_for("2026-10-01", "organic", "action")
    monkeypatch.setattr(time, "time", lambda: 1.0)
    monkeypatch.setattr(time, "time_ns", lambda: 1)
    assert do.seed_for("2026-10-01", "organic", "action") == s == 514129916
    assert do.seed_for("2026-10-02", "organic", "action") != s


# ------------------------------------------------------------------ streak

def test_sixteen_organic_days_streak_16(home):
    fill(sixteen())
    r = do.streak(now=NOW)
    assert (r["measured_streak"], r["written_streak"], r["pass"]) == (16, 16, True)
    assert r["first_break"]["reason"] == "missing"          # the walk ends at day 0
    assert do.run_streak(end=None, now=NOW) == 0


def test_missing_day_stops_streak_and_names_date(home):
    fill(sixteen(skip=5))
    r = do.streak(now=NOW)
    assert r["measured_streak"] == 11 and r["written_streak"] == 11
    assert r["first_break"] == {"date": _day(5).isoformat(), "reason": "missing"}
    assert r["pass"] is False and do.run_streak(end=None, now=NOW) == 1


def test_n29_is_unmeasured_breaks_measured_not_written(home):
    fill(sixteen(small=5))
    row = json.loads(do.day_file(_day(5).isoformat()).read_text())
    a = row["populations"]["organic"]["action"]
    assert a == {"measured": False, "reason": "unmeasured", "n": 29, "min_n": 30}   # no value, no CI keys
    r = do.streak(now=NOW)
    assert r["measured_streak"] == 11 and r["first_break"] == {"date": _day(5).isoformat(),
                                                               "reason": "unmeasured_action"}
    assert r["written_streak"] == 16


def test_turn_floor_is_ten(home):
    d = _day(16)
    # 36 actions but only 9 turns
    row = do.compute_day(d, inputs(day_rows(d, sessions=9)), now=NOW, code_rev="t")
    org = row["populations"]["organic"]
    assert org["action"]["measured"] and org["turn"] == {"measured": False, "reason": "unmeasured", "n": 9,
                                                         "min_n": 10}
    row10 = do.compute_day(d, inputs(day_rows(d, sessions=10)), now=NOW, code_rev="t")
    assert row10["populations"]["organic"]["turn"]["measured"] is True


def test_streak_ignores_partial_rows_and_files(home):
    fill(sixteen())
    p = do.day_file(LAST.isoformat())
    d = json.loads(p.read_text())
    d["partial"] = True
    p.write_text(json.dumps(d))
    r = do.streak(now=NOW)
    assert r["measured_streak"] == 0 and r["first_break"] == {"date": LAST.isoformat(), "reason": "partial"}
    # an honest partial snapshot lives apart and is never read
    today = date(2026, 10, 20)
    row = do.compute_day(today, inputs(day_rows(today)), now=NOW, code_rev="t")
    assert row["partial"] is True
    assert do.write_day(row, partial=True)["status"] == "partial"
    assert do.streak(end=today, now=NOW)["end"] == LAST.isoformat()


def test_streak_fourteen_boundary(home):
    pairs = [(_day(i), day_rows(_day(i))) for i in range(3, 17)]     # exactly 14 days
    fill(pairs)
    assert do.streak(now=NOW)["measured_streak"] == 14 and do.streak(now=NOW)["pass"] is True
    for d, _ in pairs[:1]:
        do.day_file(d.isoformat()).unlink()
    r = do.streak(now=NOW)
    assert r["measured_streak"] == 13 and r["pass"] is False


def test_streak_invalid_schema_breaks(home):
    fill(sixteen())
    do.day_file(_day(16).isoformat()).write_text("{not json")
    assert do.streak(now=NOW)["first_break"]["reason"] == "invalid_schema"


# ------------------------------------------------------------------ populations

def test_synthetic_only_day(home):
    d = _day(16)
    rows = day_rows(d, prefix="syn")
    row = do.compute_day(d, inputs(rows), now=NOW, manifest_ids={f"syn-{d}-{j}" for j in range(12)}, code_rev="t")
    p = row["populations"]
    assert p["organic"]["action"]["measured"] is False and p["organic"]["counts"]["action"]["n"] == 0
    assert p["synthetic"]["label"] == "synthetic (D-167)"
    assert p["synthetic"]["action"]["n"] == 36 and p["synthetic"]["action"]["measured"]
    assert p["other"]["action"]["n"] == 0
    do.write_day(row)
    assert do.streak(now=NOW)["measured_streak"] == 0       # never counts toward §7.1 c3


def test_synthetic_never_in_organic_counts(home):
    d = _day(16)
    rows = day_rows(d) + day_rows(d, prefix="syn")
    row = do.compute_day(d, inputs(rows), now=NOW, manifest_ids={f"syn-{d}-{j}" for j in range(12)}, code_rev="t")
    assert row["populations"]["organic"]["counts"]["action"] == {"k": 36, "n": 36}
    assert row["populations"]["synthetic"]["counts"]["action"]["n"] == 36


def test_unlisted_research_is_other_counts_only(home):
    d = _day(16)
    rows = day_rows(d, prefix="res") + day_rows(d, prefix="har")
    row = do.compute_day(d, inputs(rows), now=NOW, manifest_ids={"syn-listed"}, code_rev="t")
    p = row["populations"]
    assert p["synthetic"]["counts"]["action"]["n"] == 0
    assert p["organic"]["counts"]["action"]["n"] == 0
    o = p["other"]
    assert o["action"] == {"measured": False, "reason": "counts_only", "n": 72}
    assert o["breakdown"]["by_kind"] == {"harness": 36, "research": 36}
    assert "value" not in o["action"] and "cluster95" not in o["action"]


def test_no_manifest_means_research_is_other(home):
    d = _day(16)
    row = do.compute_day(d, inputs(day_rows(d, prefix="syn")), now=NOW, code_rev="t")
    assert row["populations"]["other"]["action"]["n"] == 36 and row["synthetic_manifest_ids"] == 0


def test_manifest_reader_takes_ids_only(tmp_path):
    p = tmp_path / "manifest.jsonl"
    p.write_text("\n".join([
        json.dumps({"schema": "synth-live-manifest/1", "session_id": "a", "session_id_stream": "zz",
                    "dry_run": False, "per_turn": [{"x": 1}]}),
        "not json",
        json.dumps({"session_id": "b", "dry_run": True}),
        json.dumps({"session_id": 5}),
        json.dumps({"session_id": "c"})]))
    assert do.read_manifest(p) == frozenset({"a", "c"})
    with pytest.raises(do.DailyError):
        do.read_manifest(tmp_path / "nope.jsonl")


# ------------------------------------------------------------------ files

def test_rerun_byte_identical_and_force_rev(home):
    d = _day(16)
    row = do.compute_day(d, inputs(day_rows(d)), now=NOW, code_rev="t")
    r1 = do.write_day(row)
    b1 = open(r1["path"], "rb").read()
    assert r1["status"] == "written"
    again = do.compute_day(d, inputs(day_rows(d)), now=NOW + 5, code_rev="t")
    r2 = do.write_day(again)
    assert r2["status"] == "exists" and open(r1["path"], "rb").read() == b1
    # force with no change: nothing new
    assert do.write_day(again, force=True)["status"] == "unchanged"
    # changed input: .rev1, original untouched; then .rev2
    changed = do.compute_day(d, inputs(day_rows(d) + day_rows(d, n=40, sessions=20, tier="sonnet")),
                             now=NOW, code_rev="t")
    r3 = do.write_day(changed, force=True)
    assert r3["status"] == "rev" and r3["path"].endswith(f"{d}.rev1.json")
    assert open(r1["path"], "rb").read() == b1
    assert do.day_file(d.isoformat()).name == f"{d}.rev1.json"       # the streak reads the newest rev
    changed2 = do.compute_day(d, inputs(day_rows(d, n=31)), now=NOW, code_rev="t")
    assert do.write_day(changed2, force=True)["path"].endswith(f"{d}.rev2.json")


def test_without_force_changed_input_never_overwrites(home):
    d = _day(16)
    p = do.write_day(do.compute_day(d, inputs(day_rows(d)), now=NOW, code_rev="t"))["path"]
    b = open(p, "rb").read()
    res = do.write_day(do.compute_day(d, inputs(day_rows(d, n=50)), now=NOW, code_rev="t"))
    assert res["status"] == "exists" and open(p, "rb").read() == b
    assert not [x for x in os.listdir(os.path.dirname(p)) if ".rev" in x or x.endswith(".tmp")]


def test_partial_today_refused_without_flag(home, capsys):
    today = date(2026, 10, 20)
    row = do.compute_day(today, inputs(day_rows(today)), now=NOW, code_rev="t")
    assert do.write_day(row)["status"] == "refused_open_day"
    assert not (do.daily_dir() / f"{today}.json").exists()
    rc = do.run_daily(date="2026-10-20", backfill=0, force=False, partial=False, manifest=None,
                      inputs=inputs([]), now=NOW)
    assert rc == 2 and "not a closed UTC day" in capsys.readouterr().err
    rc = do.run_daily(date="2026-10-20", backfill=0, force=False, partial=True, manifest=None,
                      inputs=inputs(day_rows(today)), now=NOW)
    assert rc == 0 and (do.daily_dir() / f"{today}.partial.json").is_file()
    assert not (do.daily_dir() / f"{today}.json").exists()


def test_utc_day_boundary_not_local(home):
    old = os.environ.get("TZ")
    os.environ["TZ"] = "Pacific/Auckland"          # UTC+13: local midnight is 11:00 UTC
    time.tzset()
    try:
        d = _day(16)
        start, end = do.day_bounds(d)
        rows = [proxy_row(1, sid="org-a", ts=start + 30, tier="haiku", msg_id="a"),       # 00:00:30 UTC
                proxy_row(2, sid="org-b", ts=end - 30, tier="haiku", msg_id="b"),         # 23:59:30 UTC
                proxy_row(3, sid="org-c", ts=end + 30, tier="haiku", msg_id="c"),         # next day
                proxy_row(4, sid="org-d", ts=start - 30, tier="haiku", msg_id="d")]       # day before
        row = do.compute_day(d, inputs(rows), now=NOW, code_rev="t")
        assert row["populations"]["organic"]["counts"]["action"]["n"] == 2
        assert do.utc_date(end - 1) == d and do.utc_date(end) == d + timedelta(days=1)
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


# ------------------------------------------------------------------ intervals and diagnostics

def test_cluster_ci_wider_than_wilson_and_deterministic(home):
    d = _day(16)
    base = do.day_bounds(d)[0] + 3600
    rows = []
    for i in range(90):                 # one session holds 90% of the rows; all offloaded
        rows.append(proxy_row(i, sid="org-big", ts=base + i, tier="haiku", msg_id=f"b{i}",
                              step="turn_first" if i < 10 else "continuation"))
    for j in range(10):                 # ten one-row sessions served by Sonnet
        rows.append(proxy_row(100 + j, sid=f"org-s{j}", ts=base + 100 + j, tier="sonnet", msg_id=f"s{j}"))
    row = do.compute_day(d, inputs(rows), now=NOW, code_rev="t")
    a = row["populations"]["organic"]["action"]
    assert a["n"] == 100 and a["k"] == 90 and a["largest_session_share"] == 0.9
    w, c = a["wilson95"], a["cluster95"]
    assert (c[1] - c[0]) > (w[1] - w[0]) + 0.2
    again = do.compute_day(d, inputs(rows), now=NOW + 99, code_rev="t")
    assert again["populations"]["organic"]["action"]["cluster95"] == c


def test_boot_rate_edges():
    assert do.boot_rate([]) is None
    assert do.boot_rate([("a", 1)] * 5, seed=1) == [1.0, 1.0]
    assert do.boot_rate([("a", 1), ("b", 0), ("c", 1)], seed=7) == do.boot_rate([("a", 1), ("b", 0), ("c", 1)], seed=7)


def test_no_lane_sampled_value_zero_k_if_all_positive(home):
    d = _day(16)
    row = do.compute_day(d, inputs(day_rows(d), quality=[]), now=NOW, code_rev="t")
    org = row["populations"]["organic"]
    assert org["action"]["value"] == 0.0 and org["action"]["k"] == 0
    assert org["diagnostics"]["k_if_all_lanes_sampled"] == 36 and org["diagnostics"]["sampled_lanes"] == []
    assert any("0 by rule" in c for c in org["caveats"])
    assert org["gap_to_0.60"]["action"] == -0.6


def test_breakdown_and_gaps(home):
    d = _day(16)
    rows = day_rows(d) + day_rows(d, prefix="org2", tier="sonnet", n=40, sessions=10)
    row = do.compute_day(d, inputs(rows), now=NOW, code_rev="t")
    org = row["populations"]["organic"]
    bd = org["breakdown"]
    assert set(bd) == {"by_task_group", "by_served_model", "by_lane"}
    assert bd["by_served_model"]["claude-haiku-4-5-20251001"]["k"] == 36
    assert bd["by_served_model"]["claude-sonnet-5-5"] == {"k": 0, "n": 40, "value": 0.0}
    assert org["action"]["value"] == round(36 / 76, 4)
    assert org["gap_to_0.60"]["action"] == round(36 / 76 - 0.6, 4)
    assert org["t_p2"] is None and org["gap_to_T_P2"]["action"] is None and "prereg" in org["gap_to_T_P2"]["note"]


def test_gap_to_t_p2_when_file_exists(home):
    from llm_router import paths
    (paths.state_path("prereg_t_p2.json")).parent.mkdir(parents=True, exist_ok=True)
    paths.state_path("prereg_t_p2.json").write_text(json.dumps({"T_P2": 0.25}))
    d = _day(16)
    org = do.compute_day(d, inputs(day_rows(d)), now=NOW, code_rev="t")["populations"]["organic"]
    assert org["t_p2"] == 0.25 and org["gap_to_T_P2"]["action"] == round(1.0 - 0.25, 4)
    assert org["gap_to_T_P2"]["note"] is None


def test_turn_line_counts_first_calls(home):
    d = _day(16)
    org = do.compute_day(d, inputs(day_rows(d)), now=NOW, code_rev="t")["populations"]["organic"]
    assert org["turn"]["n"] == 12 and org["turn"]["k"] == 12 and org["call"]["n"] == 36
    assert org["spread"]["k"] == 0


# ------------------------------------------------------------------ routing_report window

def test_day_local_by_cause_window_half_open(tmp_path):
    from llm_router.cost import CREATE_ROUTING_DECISIONS_TABLE
    from llm_router import ledger_view as lv

    p = tmp_path / "usage.db"
    con = sqlite3.connect(p)
    con.execute(CREATE_ROUTING_DECISIONS_TABLE)
    con.execute("ALTER TABLE routing_decisions ADD COLUMN provenance TEXT")
    for ts in ("2026-10-18 23:59:59", "2026-10-19 00:00:00", "2026-10-19 23:59:59", "2026-10-20 00:00:00"):
        con.execute("INSERT INTO routing_decisions (timestamp, final_provider, provenance) VALUES (?, 'ollama', "
                    "'runtime')", (ts,))
    con.commit()
    con.close()
    s, e = do.day_bounds(LAST)
    assert sum(lv.local_by_cause(lv.Bounds("day", "d", s, e - 1), db_path=p).values()) == 2


# ------------------------------------------------------------------ CLI

def test_cli_daily_and_streak_under_tmp_home(home, capsys):
    rows = []
    for i in range(1, 17):
        rows += day_rows(_day(i))
    inp = inputs(rows)
    assert do.run_daily(date=str(LAST), backfill=3, force=False, partial=False, manifest=None, inputs=inp,
                        now=NOW) == 0
    out = capsys.readouterr().out
    assert [ln.split()[1] for ln in out.splitlines()] == ["written"] * 3 and "organic action 36/36=1.0" in out
    assert do.run_daily(date=str(LAST), backfill=3, force=False, partial=False, manifest=None, inputs=inp,
                        now=NOW) == 0
    assert [ln.split()[1] for ln in capsys.readouterr().out.splitlines()] == ["exists"] * 3
    assert sorted(os.listdir(do.daily_dir())) == [f"{_day(i)}.json" for i in (14, 15, 16)]


def test_kpi_flags_route_to_daily(home, capsys, monkeypatch):
    assert kpi.cmd_kpi(["--daily-render-plist"]) == 0
    out = capsys.readouterr().out
    assert "PRINT ONLY" in out and "--daily" in out and not os.path.exists(do.daily_dir())
    assert kpi.cmd_kpi(["--daily-streak", "--end", "2026-10-19"]) == 1       # nothing written: fails, no crash
    assert "FAIL" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        kpi.cmd_kpi(["--date", "2026-10-19"])                                 # needs --daily
    assert kpi.cmd_kpi(["--daily", "--date", "2026-10-19", "--synthetic-manifest", "/nonexistent/m.jsonl"]) == 2
