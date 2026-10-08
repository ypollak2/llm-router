"""PLAN v16 P0.9-g (AMEND R8 A.3): the five sync hooks with no latency MUST are
measured from outside (``hook_wall``), the machine load is on every row, and
``llm-router kpi`` judges p95 <= 300 ms per hook with its n.

R8 found two measurements of the same hooks 10x apart (enforce-route p95 469 ms
vs 47 ms) and could not say which was load-inflated, because no row recorded the
load. So: every wall row carries ``load1_before`` / ``load1_after``, every live
hook row carries ``load1``, rows above the bar are excluded and counted, and the
load read sits outside the timed span (the measurement must not grow the number
it measures).
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from llm_router import failopen, hook_latency as hl, hook_wall as hw
from llm_router.commands import kpi

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "tests" / "fixtures" / "hook_payloads"
HOOKS = REPO / "src" / "llm_router" / "hooks"
NOW = 1_790_000_000.0
DAY = 86400.0
FIVE = ("enforce-route", "bash-compress", "playwright-compress", "cc-usage-track", "subagent-start")


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    (tmp_path / "claude-projects").mkdir()
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(tmp_path / "claude-projects"))
    monkeypatch.delenv("LLM_ROUTER_HOOK_LATENCY", raising=False)
    monkeypatch.delenv("LLM_ROUTER_HOOK_LATENCY_MAX_BYTES", raising=False)

    def _reset():
        hl._pending = None
        hl._phases.clear()
        for name, value in (("_model_depth", 0), ("_nested_model_ms", 0.0), ("_session_id", None)):
            setattr(hl, name, value)

    _reset()
    failopen.reset_unpersisted()
    failopen.reset_cache()
    yield
    _reset()
    failopen.reset_unpersisted()
    failopen.reset_cache()


# ── R8 task 3: P0.9-f's table names all five ─────────────────────────────────


def test_the_budget_table_names_each_of_the_five_sync_hooks_at_300ms():
    assert hw.SYNC_HOOKS == FIVE
    assert {h: hl.HOOK_BUDGETS_MS.get(h) for h in FIVE} == {h: 300 for h in FIVE}


def test_min_n_is_r8s_200_and_30_for_the_two_low_volume_hooks():
    assert hw.WALL_MIN_N == 200
    assert hw.LIVE_MIN_N == {"enforce-route": 200, "bash-compress": 200, "playwright-compress": 200,
                             "cc-usage-track": 30, "subagent-start": 30}
    assert hw.NOT_INFORMATIVE_AFTER_DAYS == 14


# ── live rows carry the load, read after the clock stopped ───────────────────


def _lines():
    return [json.loads(x) for x in hl.store_path().read_text().splitlines()]


def test_a_hook_row_carries_load1_read_after_the_clock_stopped(monkeypatch):
    t = {"now": 1000.0}
    monkeypatch.setattr(hl, "_monotonic", lambda: t["now"])
    monkeypatch.setattr(hl, "_wall", lambda: NOW)

    def slow_loadavg():  # if the load read were inside the timed span, 5 s would show
        t["now"] += 5.0
        return (2.5, 2.0, 1.5)

    monkeypatch.setattr(hl, "_getloadavg", slow_loadavg)
    hl.begin("enforce-route", "PreToolUse", t0=1000.0)
    t["now"] += 0.1
    hl._finish()
    (row,) = _lines()
    assert row["elapsed_ms"] == 100.0
    assert row["load1"] == 2.5


@pytest.mark.parametrize("broken", [None, lambda: (_ for _ in ()).throw(OSError("no load")),
                                    lambda: (float("nan"), 0, 0)])
def test_no_load_from_the_os_still_writes_the_row_without_load1(monkeypatch, broken):
    monkeypatch.setattr(hl, "_getloadavg", broken)
    hl.begin("enforce-route", "PreToolUse")
    hl._finish()
    (row,) = _lines()
    assert "load1" not in row and row["hook"] == "enforce-route"


def test_a_real_hook_process_writes_load1(tmp_path):
    env = hw.child_env(tmp_path)
    proc = subprocess.run([sys.executable, str(HOOKS / "subagent-start.py")],
                          input=(FIXTURES / "subagent-start.json").read_bytes(), env=env,
                          capture_output=True, timeout=120)
    assert proc.returncode == 0, proc.stderr.decode()
    (row,) = [json.loads(x) for x in (Path(env["LLM_ROUTER_HOME"]) / "hook_latency.jsonl").read_text().splitlines()]
    assert isinstance(row["load1"], float) and row["load1"] >= 0


# ── the harness ──────────────────────────────────────────────────────────────

_FAKE_HOOK = r'''
import time as _t
_T0 = _t.monotonic()
import json, os, sys
from llm_router import hook_latency
hook_latency.begin(os.path.basename(__file__)[:-3], "E", _T0)
payload = json.loads(sys.stdin.read())
if payload.get("dump"):
    with open(payload["dump"], "a") as fh:
        fh.write(json.dumps({k: os.environ.get(k) for k in ("HOME", "LLM_ROUTER_HOME", "OLLAMA_HOST",
                                                           "LLM_ROUTER_SYNTHETIC", "PATH")}) + "\n")
_t.sleep(payload.get("sleep", 0))
'''


def _fake_hooks(tmp_path: Path, names=("enforce-route", "subagent-start"), **payload) -> tuple[Path, dict]:
    d = tmp_path / "hooks"
    d.mkdir()
    fixtures = {}
    for n in names:
        (d / f"{n}.py").write_text(_FAKE_HOOK)
        fx = tmp_path / f"{n}.json"
        fx.write_text(json.dumps(payload))
        fixtures[n] = fx
    return d, fixtures


def test_measure_writes_one_row_per_run_per_hook_per_mode_with_the_load(tmp_path):
    hooks_dir, fixtures = _fake_hooks(tmp_path, sleep=0.02)
    out = tmp_path / "wall.jsonl"
    rows = hw.measure(list(fixtures), fixtures, runs=3, modes=["cold", "warm"], gap_s=0.01,
                      hooks_dir=hooks_dir, out=out, seed=1)
    assert len(rows) == 3 * 2 * 2
    assert [json.loads(x) for x in out.read_text().splitlines()] == rows
    assert {(r["hook"], r["mode"]) for r in rows} == {(h, m) for h in fixtures for m in ("cold", "warm")}
    assert len({r["run_id"] for r in rows}) == 1
    for r in rows:
        assert r["rc"] == 0
        assert isinstance(r["load1_before"], float) and isinstance(r["load1_after"], float)
        # The hook's own clock ran (it slept 20 ms), and the outside clock saw at least that much more.
        assert 20 <= r["in_process_ms"] < r["wall_ms"]
        assert r["gap_s"] == (0.01 if r["mode"] == "cold" else 0.0)


def test_the_load_read_and_the_cold_gap_are_outside_the_timed_span(tmp_path, monkeypatch):
    """The measurement must not add to the number it measures: with a fake clock
    that only the load read and the idle gap advance, every run times ~0."""
    hooks_dir, fixtures = _fake_hooks(tmp_path)
    t = {"now": 0.0}
    monkeypatch.setattr(hw, "_clock", lambda: t["now"])

    def loadavg():
        t["now"] += 10.0
        return 1.0

    def sleep(s):
        t["now"] += 10.0

    monkeypatch.setattr(hw, "_loadavg", loadavg)
    monkeypatch.setattr(hw, "_sleep", sleep)
    rows = hw.measure(list(fixtures), fixtures, runs=2, modes=["cold", "warm"], hooks_dir=hooks_dir,
                      out=tmp_path / "w.jsonl")
    assert rows and {r["wall_ms"] for r in rows} == {0.0}


def test_children_run_with_a_throwaway_home_and_no_local_model(tmp_path):
    dump = tmp_path / "env.jsonl"
    hooks_dir, fixtures = _fake_hooks(tmp_path, names=("bash-compress",), dump=str(dump))
    hw.measure(["bash-compress"], fixtures, runs=1, modes=["warm"], hooks_dir=hooks_dir, out=tmp_path / "w.jsonl")
    envs = [json.loads(x) for x in dump.read_text().splitlines()]
    assert len(envs) == hw.PRIMING_RUNS + 1
    for e in envs:
        home = Path(e["HOME"])
        assert home.name == "home" and home.parent.name.startswith("hook-wall-")
        assert e["LLM_ROUTER_HOME"].startswith(e["HOME"])
        assert e["OLLAMA_HOST"] == "http://127.0.0.1:9" and e["LLM_ROUTER_SYNTHETIC"] == "1"
        assert e["PATH"] == "/usr/bin:/bin"
    assert not Path(envs[0]["HOME"]).exists(), "the throwaway home is removed after the run"


@pytest.mark.parametrize("hook", FIVE)
def test_each_fixture_drives_its_real_hook_down_its_working_path(hook, tmp_path):
    """A fixture that made the hook exit at its first check would time nothing."""
    env = hw.child_env(tmp_path)
    proc = subprocess.run([sys.executable, str(HOOKS / f"{hook}.py")], input=(FIXTURES / f"{hook}.json").read_bytes(),
                          env=env, capture_output=True, timeout=120)
    assert proc.returncode == 0, proc.stderr.decode()
    state = Path(env["LLM_ROUTER_HOME"])
    (row,) = [json.loads(x) for x in (state / "hook_latency.jsonl").read_text().splitlines()]
    assert row["hook"] == hook
    out = proc.stdout.decode()
    if hook == "bash-compress":      # compressed and logged, not skipped as too small
        with sqlite3.connect(state / "usage.db") as db:
            assert db.execute("select count(*) from compression_stats").fetchone()[0] == 1
    elif hook == "cc-usage-track":   # an Agent call: one usage row
        with sqlite3.connect(state / "usage.db") as db:
            assert db.execute("select count(*) from usage").fetchone()[0] == 1
    elif hook == "playwright-compress":  # >= 40 lines: the compression chain ran
        assert "DOM snapshot compressed" in out
    elif hook == "subagent-start":
        assert json.loads(out)["hookSpecificOutput"]["hookEventName"] == "SubagentStart"
    else:
        payload = json.loads((FIXTURES / f"{hook}.json").read_text())
        assert payload["tool_name"] == "Bash" and payload["hook_event_name"] == "PreToolUse"


# ── judging ──────────────────────────────────────────────────────────────────


def _wall_rows(hook, ms, *, n, mode="cold", load=1.0, run_id="r1", ts=NOW):
    return [{"ts": ts + i * 0.001, "run_id": run_id, "hook": hook, "mode": mode, "seq": i,
             "wall_ms": float(ms), "in_process_ms": float(ms) - 30, "rc": 0,
             "load1_before": load, "load1_after": load} for i in range(n)]


def test_wall_verdict_is_on_cold_with_rows_above_the_load_bar_excluded_and_counted():
    rows = (_wall_rows("enforce-route", 100, n=200) + _wall_rows("enforce-route", 900, n=40, load=9.0)
            + _wall_rows("enforce-route", 900, n=200, mode="warm"))
    w = hw.judge_wall(rows)["enforce-route"]
    assert w["verdict"] == hw.PASS
    assert (w["cold"]["n"], w["cold"]["excluded_load"], w["cold"]["p95_ms"]) == (200, 40, 100.0)
    assert w["warm"]["p95_ms"] == 900.0
    assert w["cold"]["startup_gap_median_ms"] == 30.0


def test_wall_rows_split_on_load_before_or_after_and_unrecorded_load_is_excluded():
    rows = _wall_rows("bash-compress", 100, n=200)
    rows[0]["load1_after"] = 4.01
    rows[1]["load1_before"] = 4.5
    del rows[2]["load1_before"], rows[2]["load1_after"]
    rows[3]["load1_before"] = 4.0  # at the bar is in
    c = hw.judge_wall(rows)["bash-compress"]["cold"]
    assert (c["n"], c["excluded_load"]) == (197, 3)


def test_wall_below_200_cold_runs_is_insufficient_and_over_300_fails():
    assert hw.judge_wall(_wall_rows("enforce-route", 100, n=199))["enforce-route"]["verdict"] == hw.INSUFFICIENT
    rows = _wall_rows("enforce-route", 100, n=180) + _wall_rows("enforce-route", 301, n=20)
    w = hw.judge_wall(rows)["enforce-route"]
    assert w["verdict"] == hw.FAIL and w["cold"]["p95_ms"] == 301.0


def test_wall_is_judged_on_each_hooks_latest_run_only():
    rows = _wall_rows("enforce-route", 900, n=200, run_id="old") + _wall_rows("enforce-route", 90, n=200,
                                                                              run_id="new", ts=NOW + 10)
    w = hw.judge_wall(rows)["enforce-route"]
    assert (w["run_id"], w["verdict"], w["cold"]["n"]) == ("new", hw.PASS, 200)


def _live(hook, ms, *, n, load=1.0):
    rows = []
    for i in range(n):
        r = {"hook": hook, "event": "E", "elapsed_ms": float(ms), "ts": NOW - 60 - i}
        if load is not None:
            r["load1"] = load
        rows.append(r)
    return rows


def test_live_excludes_rows_above_the_load_bar_and_counts_unrecorded_load():
    rows = _live("enforce-route", 50, n=190) + _live("enforce-route", 50, n=10, load=None) \
        + _live("enforce-route", 5000, n=30, load=12.0)
    lv = hw.judge_live(rows, 7)["enforce-route"]
    assert (lv["n"], lv["excluded_load"], lv["load_not_recorded"], lv["p95_ms"], lv["verdict"]) == (
        200, 30, 10, 50.0, hw.PASS)


def test_live_min_n_is_200_for_per_tool_hooks_and_30_for_the_low_volume_two():
    rows = _live("bash-compress", 50, n=199) + _live("cc-usage-track", 50, n=30) + _live("subagent-start", 400, n=30)
    lv = hw.judge_live(rows, 7)
    assert lv["bash-compress"]["verdict"] == hw.INSUFFICIENT
    assert lv["cc-usage-track"]["verdict"] == hw.PASS
    assert lv["subagent-start"]["verdict"] == hw.FAIL


def test_a_low_volume_hook_under_30_is_not_informative_only_after_14_days():
    rows = _live("cc-usage-track", 50, n=2) + _live("bash-compress", 50, n=2)
    assert hw.judge_live(rows, 13.9)["cc-usage-track"]["verdict"] == hw.INSUFFICIENT
    after = hw.judge_live(rows, 14)
    assert after["cc-usage-track"]["verdict"] == hw.NOT_INFORMATIVE
    assert after["bash-compress"]["verdict"] == hw.INSUFFICIENT  # a per-tool hook never is


def test_the_gate_needs_wall_and_live_and_lets_wall_decide_when_live_is_not_informative():
    wall = sum((_wall_rows(h, 100, n=200) for h in FIVE), [])
    live = sum((_live(h, 50, n=200) for h in FIVE[:3]), []) + _live("cc-usage-track", 50, n=30)
    g = hw.gate(wall, live, 14)
    assert g["hooks"]["subagent-start"]["live"]["verdict"] == hw.NOT_INFORMATIVE
    assert g["verdict"] == hw.PASS
    assert hw.gate(wall, live, 7)["verdict"] == hw.INSUFFICIENT
    live_over = live + _live("enforce-route", 900, n=40)
    assert hw.gate(wall, live_over, 14)["hooks"]["enforce-route"]["verdict"] == hw.FAIL
    assert hw.gate(wall, live_over, 14)["verdict"] == hw.FAIL


# ── the kpi line ─────────────────────────────────────────────────────────────


def test_kpi_prints_the_p09g_line_with_n_per_hook(monkeypatch):
    for r in sum((_wall_rows(h, 120, n=200) for h in FIVE), []) + _wall_rows("bash-compress", 250, n=12, load=6.0):
        hw.capped_log.append(hw.store_path(), (json.dumps(r) + "\n").encode(), 1 << 22)
    for i in range(200):
        hl.record("enforce-route", "PreToolUse", 40.0, now=NOW - 100 - i, load1=1.5)
    for i in range(5):
        hl.record("enforce-route", "PreToolUse", 2000.0, now=NOW - 50 - i, load1=7.0)
    card = kpi.compute_scorecard(days=7, now=NOW)
    g = card["p09g"]
    assert g["hooks"]["enforce-route"]["live"]["n"] == 200
    assert g["hooks"]["enforce-route"]["live"]["excluded_load"] == 5
    assert g["hooks"]["bash-compress"]["wall"]["cold"]["excluded_load"] == 12
    text = kpi.render_scorecard(card)
    assert "P0.9-g sync hooks p95 <= 300ms" in text and "INSUFFICIENT" in text
    line = next(ln for ln in text.splitlines() if ln.startswith("enforce-route: "))
    assert "wall cold p50=120ms p95=120ms max=120ms n=200/200" in line
    assert "live elapsed p95=40ms n=200/200 (5 above load excluded, 0 load not recorded) PASS" in line
    cc = next(ln for ln in text.splitlines() if ln.startswith("cc-usage-track: "))
    assert "n=0/30*" in cc
    assert "per tool call (report only): enforce-route + bash-compress wall cold p95 sum = 240ms" in text
    assert "G1_hook" in card["kpis"] and "p09g" not in card["kpis"]  # a gate line, not a KPI


def test_kpi_p09g_with_no_wall_rows_is_insufficient_not_a_number():
    card = kpi.compute_scorecard(days=7, now=NOW)
    assert card["p09g"]["verdict"] == hw.INSUFFICIENT
    line = next(ln for ln in kpi.render_scorecard(card).splitlines() if ln.startswith("enforce-route: "))
    assert "p95=- " in line and "n=0/200" in line


# ── command line ─────────────────────────────────────────────────────────────


def test_report_reads_a_store_and_prints_the_lines(tmp_path, capsys):
    store = tmp_path / "w.jsonl"
    store.write_text("".join(json.dumps(r) + "\n" for r in _wall_rows("enforce-route", 80, n=200)))
    assert hw.main(["report", "--store", str(store), "--days", "7"]) == 0
    out = capsys.readouterr().out
    assert "enforce-route: INSUFFICIENT | wall cold p50=80ms p95=80ms" in out and "n=200/200" in out


def test_run_rejects_a_fixture_without_exactly_one_hook(tmp_path):
    fx = tmp_path / "p.json"
    fx.write_text("{}")
    with pytest.raises(SystemExit):
        hw.main(["run", "--fixture", str(fx), "--runs", "1"])


def test_the_script_entry_point_runs_from_a_checkout(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run([sys.executable, str(REPO / "scripts" / "hook_wall.py"), "report", "--store",
                           str(tmp_path / "none.jsonl")], env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("P0.9-g sync hooks")
