"""P35-5: weekly report from daily rows, ``kpi --maintenance``, the session-start stale trigger.

Evidence: ``v16/coord/P35-PLAN.md`` (Weekly (d)). Literal numbers below are hand-computed from the rows each
test writes (k/n sums, Wilson via ``daily_offload.wilson_ci``), not read back from the code under test.
"""
from __future__ import annotations

import ast
import calendar
import importlib.util
import json
import os
import time
from datetime import date, timedelta
from pathlib import Path

import pytest

from llm_router import daily_offload as do
from llm_router import weekly_report as wr

ROOT = Path(__file__).resolve().parents[1]
START = ROOT / "src" / "llm_router" / "hooks" / "session-start.py"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.delenv("LLM_ROUTER_NO_DETACHED_SPAWN", raising=False)
    for v in ("LLM_ROUTER_MAINTENANCE_AUTO", "LLM_ROUTER_MAINTENANCE_STALE_H"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(os, "nice", lambda inc: 0)          # never lower the priority of the test process
    return tmp_path


def _row(day: date, k: int, n: int, *, partial: bool = False, schema: str = do.SCHEMA) -> dict:
    return {"schema": schema, "date": day.isoformat(), "partial": partial, "populations": {"organic": {
        "action": {"measured": n >= 30, "k": k, "n": n, "value": round(k / n, 4) if n >= 30 else None},
        "counts": {"action": {"k": k, "n": n}, "turn": {"k": 4, "n": 8}},
        "breakdown": {"by_task_group": {"code": {"k": k, "n": n - 3}, "docs": {"k": 0, "n": 3}}}}}}


def _week(monday: date, k: int, n: int, skip: tuple[int, ...] = ()) -> None:
    for i in range(7):
        if i not in skip:
            do.write_day(_row(monday + timedelta(days=i), k, n))


MON41, MON40 = date(2026, 10, 5), date(2026, 9, 28)


def test_pools_counts_and_wilson_from_daily_rows(home):
    _week(MON41, 20, 40)                                   # 7 days x 20/40
    w = wr.build_week("2026-W41")
    o = w["offload"]
    assert (o["k"], o["n"], o["value"], o["informative"]) == (140, 280, 0.5, True)
    assert o["ci"] == do.wilson_ci(140, 280) and 0.44 < o["ci"][0] < 0.5 < o["ci"][1] < 0.56
    assert w["window"] == {"from": "2026-10-05", "to": "2026-10-11", "tz": "UTC"}
    assert [t["date"] for t in w["trend"]] == [(MON41 + timedelta(days=i)).isoformat() for i in range(7)]
    assert all(t["n"] == 40 and t["value"] == 0.5 for t in w["trend"])
    code = w["breakdowns"]["by_task_group"]["code"]
    assert (code["k"], code["n"], code["informative"]) == (140, 259, True)
    assert code["ci"] == do.wilson_ci(140, 259)


def test_group_below_floor_is_not_informative_and_prints_n(home):
    _week(MON41, 20, 40)
    docs = wr.build_week("2026-W41")["breakdowns"]["by_task_group"]["docs"]    # 7 days x 3 actions
    assert (docs["k"], docs["n"]) == (0, 21)
    assert docs["value"] is None and docs["ci"] is None and docs["informative"] is False
    assert docs["reason"] == "not informative (n=21 < 30)"
    text = wr.render(wr.build_week("2026-W41"))
    assert "- docs: n/a (not informative (n=21 < 30))" in text and "- docs: 0.0%" not in text


def test_too_few_days_makes_a_big_pool_not_informative(home):
    _week(MON41, 20, 40, skip=(0, 1, 2))                  # 4 days, n=160 >= 30 but < MIN_WEEK_DAYS rows
    o = wr.build_week("2026-W41")["offload"]
    assert o["n"] == 160 and o["value"] is None and o["ci"] is None and o["informative"] is False
    assert o["reason"] == "not informative (n=160, only 4 day(s) with a row, need 5)"


def test_missing_day_is_na_never_zero_and_is_listed(home):
    _week(MON41, 20, 40, skip=(2,))
    w = wr.build_week("2026-W41")
    assert w["days"] == 6 and w["days_skipped"] == {"2026-10-07": "no row"}
    gap = [t for t in w["trend"] if t["date"] == "2026-10-07"]
    assert gap == [{"date": "2026-10-07", "value": None, "n": None}] and len(w["trend"]) == 7
    assert w["offload"]["n"] == 240
    text = wr.render(w)
    assert "not used: 2026-10-07 (no row)" in text and "- 2026-10-07: n/a (no row)" in text


def test_unmeasured_day_value_is_none_but_n_is_kept(home):
    do.write_day(_row(MON41, 3, 12))
    w = wr.build_week("2026-W41")
    assert w["trend"][0] == {"date": "2026-10-05", "k": 3, "n": 12, "value": None}
    assert "n=12" in wr.render(w) and "n/a" in wr.render(w)


def test_empty_week_is_na_not_zero(home):
    w = wr.build_week("2026-W30")
    assert w["days"] == 0 and w["offload"] is None and w["breakdowns"] == {} and w["trend"] == []
    text = wr.render(w)
    assert "offload: n/a" in text and "0.0%" not in text
    assert wr.write_week(w) == {"status": "skipped", "path": None}
    assert not (home / "weekly").exists()


def test_delta_only_when_both_weeks_are_informative(home):
    _week(MON41, 24, 40)                                   # 0.6
    _week(MON40, 16, 40)                                   # 0.4
    d = wr.build_week("2026-W41")["delta_vs_previous_week"]
    assert d is not None and d["value"] == 0.2 and (d["n"], d["previous_n"]) == (280, 280)
    assert "no significance" in d["note"]
    _week(date(2026, 9, 21), 1, 40, skip=(3, 4, 5, 6))     # W39: 3 days
    w = wr.build_week("2026-W40")
    assert w["delta_vs_previous_week"] is None and "2026-W39" in w["delta_note"] and "n=120" in w["delta_note"]


def test_partial_old_schema_and_unreadable_rows_are_not_pooled(home):
    _week(MON41, 20, 40, skip=(0, 1, 2))
    do.write_day(_row(MON41, 20, 40, partial=True), partial=True)
    (do.daily_dir() / "2026-10-06.json").write_text(json.dumps(_row(MON41 + timedelta(days=1), 20, 40,
                                                                    schema=do.OLD_SCHEMA)))
    (do.daily_dir() / "2026-10-07.json").write_text("{not json")
    w = wr.build_week("2026-W41")
    assert w["days"] == 4 and w["offload"]["n"] == 160
    assert w["days_skipped"]["2026-10-05"] == "no row"     # the .partial.json never counts
    assert "daily-offload/1" in w["days_skipped"]["2026-10-06"]
    assert w["days_skipped"]["2026-10-07"].startswith("unreadable")


def test_write_is_atomic_idempotent_and_rewrites_on_change(home):
    _week(MON41, 20, 40)
    w = wr.build_week("2026-W41")
    r1 = wr.write_week(w)
    p = Path(r1["path"])
    assert r1["status"] == "written" and p == home / "weekly" / "2026-W41.json"
    assert json.loads(p.read_text())["schema"] == wr.SCHEMA == "weekly-offload/1"
    assert (home / "weekly" / "2026-W41.md").read_text().startswith("# Weekly offload 2026-W41")
    later = wr.build_week("2026-W41", now=time.time() + 3600)       # only generated_at differs
    assert wr.write_week(later)["status"] == "unchanged"
    do.write_day(_row(MON41, 24, 40), force=True)                   # a changed measurement
    assert wr.write_week(wr.build_week("2026-W41"))["status"] == "written"
    assert not [f for f in os.listdir(home / "weekly") if f.endswith(".tmp")]


@pytest.mark.parametrize("bad", ["2026-41", "W41", "2026-W54", "2026-W00", "", "2026-W4"])
def test_bad_week_ids_are_refused(bad):
    with pytest.raises(wr.WeeklyError):
        wr.week_start(bad)


def test_week_ids_across_year_boundaries():
    assert wr.week_start("2026-W53") == date(2026, 12, 28) and wr.iso_week_of(date(2027, 1, 3)) == "2026-W53"


def test_last_closed_week_is_the_week_before_the_current_one():
    assert wr.last_closed_week(calendar.timegm((2026, 10, 19, 12, 0, 0))) == "2026-W42"   # Monday
    assert wr.last_closed_week(calendar.timegm((2026, 10, 18, 23, 0, 0))) == "2026-W41"   # Sunday
    assert wr.last_closed_week(calendar.timegm((2027, 1, 4, 0, 0, 1))) == "2026-W53"


# ---------------------------------------------------------------- kpi --maintenance

NOW = calendar.timegm((2026, 10, 19, 12, 0, 0))            # Monday: last closed day 10-18, last closed week W42


def test_maintenance_builds_the_last_two_closed_weeks_and_is_idempotent(home, capsys):
    for i in range(14):                                     # 10-05 .. 10-18: the 7-day backfill finds them all
        do.write_day(_row(MON41 + timedelta(days=i), 20, 40))
    assert wr.run_maintenance(now=NOW) == 0
    out = capsys.readouterr().out
    assert "daily backfill" in out
    assert "weekly 2026-W41  written" in out and "weekly 2026-W42  written" in out
    assert (home / "weekly" / "2026-W41.json").is_file() and (home / "weekly" / "2026-W42.md").is_file()
    stamp = json.loads((home / "weekly" / ".last_run.json").read_text())
    assert stamp["exit"] == 0 and [x["week"] for x in stamp["weeks"]] == ["2026-W41", "2026-W42"]
    assert wr.run_maintenance(now=NOW + 60) == 0
    again = capsys.readouterr().out
    assert "weekly 2026-W41  unchanged" in again and "weekly 2026-W42  unchanged" in again


def test_maintenance_defers_while_the_benchmark_marker_exists(home, capsys):
    do.bench_marker().parent.mkdir(parents=True, exist_ok=True)
    do.bench_marker().write_text("1")
    assert wr.run_maintenance() == 0
    assert "deferred" in capsys.readouterr().out
    assert not (home / "weekly").exists() and not (home / "daily").exists()


def test_maintenance_second_run_while_locked_is_busy(home, capsys):
    from llm_router import file_lock

    with file_lock.exclusive_lock(wr.weekly_dir() / ".lock", timeout=0) as got:
        assert got
        # same process, a second flock on a new fd of the same file: not granted
        assert wr.run_maintenance() == 0
    assert "busy" in capsys.readouterr().out
    assert not (home / "weekly" / ".last_run.json").exists()


def test_maintenance_failure_exits_1_and_still_stamps(home, monkeypatch, capsys):
    monkeypatch.setattr(do, "run_daily", lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    assert wr.run_maintenance() == 1
    assert "failed (RuntimeError" in capsys.readouterr().err
    assert json.loads((home / "weekly" / ".last_run.json").read_text())["exit"] == 1


def test_maintenance_lowers_priority(home, monkeypatch):
    seen = []
    monkeypatch.setattr(os, "nice", lambda inc: seen.append(inc) or 0)
    monkeypatch.setattr(do, "run_daily", lambda **kw: 0)
    wr.run_maintenance(now=NOW)
    assert seen == [19]


# ---------------------------------------------------------------- session-start trigger

def _hook():
    spec = importlib.util.spec_from_file_location("_p355_start", START)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def spawns(home, monkeypatch):
    hook = _hook()
    calls: list[list[str]] = []
    monkeypatch.setattr(hook, "_spawn_detached", lambda argv: calls.append(argv) or True)
    return hook, calls, home


def test_stale_or_missing_stamp_spawns_exactly_one_kpi_maintenance(spawns):
    hook, calls, home = spawns
    assert hook._maybe_spawn_maintenance() is True            # never ran: stale
    assert len(calls) == 1 and calls[0][-2:] == ["kpi", "--maintenance"]
    assert hook._maybe_spawn_maintenance() is False           # the claim holds for an hour
    assert len(calls) == 1


def test_fresh_stamp_does_not_spawn(spawns):
    hook, calls, home = spawns
    (home / "weekly").mkdir()
    (home / "weekly" / ".last_run.json").write_text("{}")
    assert hook._maybe_spawn_maintenance() is False and calls == []
    old = time.time() - 25 * 3600
    os.utime(home / "weekly" / ".last_run.json", (old, old))
    assert hook._maybe_spawn_maintenance() is True and len(calls) == 1


def test_stale_hours_env_is_honoured(spawns, monkeypatch):
    hook, calls, home = spawns
    (home / "weekly").mkdir()
    stamp = home / "weekly" / ".last_run.json"
    stamp.write_text("{}")
    old = time.time() - 3 * 3600
    os.utime(stamp, (old, old))
    monkeypatch.setenv("LLM_ROUTER_MAINTENANCE_STALE_H", "6")
    assert hook._maybe_spawn_maintenance() is False
    monkeypatch.setenv("LLM_ROUTER_MAINTENANCE_STALE_H", "2")
    assert hook._maybe_spawn_maintenance() is True


def test_benchmark_marker_and_opt_out_block_the_spawn(spawns, monkeypatch):
    hook, calls, home = spawns
    (home / "bench.active").write_text("1")
    assert hook._maybe_spawn_maintenance() is False and calls == []
    (home / "bench.active").unlink()
    monkeypatch.setenv("LLM_ROUTER_MAINTENANCE_AUTO", "0")
    assert hook._maybe_spawn_maintenance() is False and calls == []
    assert not (home / "maintenance_spawn.claim").exists()


def test_failed_spawn_releases_the_claim(home, monkeypatch):
    hook = _hook()
    monkeypatch.setattr(hook, "_spawn_detached", lambda argv: False)
    assert hook._maybe_spawn_maintenance() is False
    assert not (home / "maintenance_spawn.claim").exists()


def test_missing_state_dir_does_not_create_it_or_spawn(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "absent"))
    hook = _hook()
    calls = []
    monkeypatch.setattr(hook, "_spawn_detached", lambda argv: calls.append(argv) or True)
    assert hook._maybe_spawn_maintenance() is False and calls == []
    assert not (tmp_path / "absent").exists()


def test_the_trigger_adds_no_llm_router_import():
    tree = ast.parse(START.read_text(encoding="utf-8"))
    fns = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert {"_maybe_spawn_maintenance", "_maintenance_argv"} <= set(fns)
    for name in ("_maybe_spawn_maintenance", "_maintenance_argv"):
        imports = [n for n in ast.walk(fns[name]) if isinstance(n, (ast.Import, ast.ImportFrom))]
        assert imports == [], f"{name} imports on the hot path: {ast.dump(imports[0])}"


def test_the_hook_pair_is_byte_identical_at_version_42():
    a, b = (ROOT / "hooks" / "session-start.py").read_bytes(), START.read_bytes()
    assert a == b and b.splitlines()[1] == b"# llm_router-hook-version: 42"
