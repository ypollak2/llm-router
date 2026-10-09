"""PLAN v16 P0.9 tasks 1-2: hook budgets are the PRD bars, G1 judges router-added
time, and the statusline records a sampled timing row.

Before this change ``HOOK_BUDGETS_MS`` held the host timeouts (auto-route 60 s,
agent-route 320 s) and declared 2-10 s budgets, so ``llm-router kpi`` called a
16 s auto-route p95 and a 4.5 s status-bar p95 "within budget" [HL7: status-bar
p95 4,488 ms, n = 344]. The PRD bars are 300 ms per sync hook, 2 s for
session-start and 100 ms for the statusline (NFR-LAT, S6). A local draft's model
time is the answer, not overhead, so G1 judges ``router_added_ms`` = elapsed minus
the model phases (``draft_chain``, ``zce_model``, ``cold_wait``).
"""

from __future__ import annotations

import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

import pytest

from llm_router import failopen, hook_latency as hl
from llm_router.commands import kpi

REPO = Path(__file__).resolve().parent.parent
STATUSLINE = REPO / "src" / "llm_router" / "hooks" / "statusline-command.sh"
NOW = 1_790_000_000.0


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
            if hasattr(hl, name):
                setattr(hl, name, value)

    _reset()
    failopen.reset_unpersisted()
    failopen.reset_cache()
    yield
    _reset()
    failopen.reset_unpersisted()
    failopen.reset_cache()


def _lines():
    return [json.loads(x) for x in hl.store_path().read_text().splitlines()]


# ── task 1: the table is the PRD's bars ──────────────────────────────────────


def test_budgets_are_the_prd_bars():
    sync_hooks = set(hl.HOOK_BUDGETS_MS) - {"session-start", "statusline"}
    assert sync_hooks, "the table lost its sync hooks"
    assert {h: hl.HOOK_BUDGETS_MS[h] for h in sync_hooks} == {h: 300 for h in sync_hooks}
    assert hl.HOOK_BUDGETS_MS["session-start"] == 2_000
    assert hl.HOOK_BUDGETS_MS["status-bar"] == 300
    assert hl.HOOK_BUDGETS_MS["statusline"] == 100
    assert hl.HOOK_BUDGETS_MS["auto-route"] == 300
    assert hl.DEFAULT_BUDGET_MS == 300


def test_timed_out_still_means_the_host_timeout_not_the_bar():
    # A 400 ms enforce-route call is over its 300 ms bar but nowhere near the
    # host's 60 s timeout: timed_out must stay False.
    assert hl.record("enforce-route", "PreToolUse", 400.0, now=NOW)
    assert hl.record("enforce-route", "PreToolUse", 60_000.0, now=NOW)
    assert [r["timed_out"] for r in _lines()] == [False, True]


def test_kpi_fails_status_bar_at_the_measured_live_p95():
    # 344 rows like [HL7]: most fast, the top 5% at 4,488 ms.
    for i in range(326):
        hl.record("status-bar", "UserPromptSubmit", 44.0, now=NOW - 100 - i)
    for i in range(18):
        hl.record("status-bar", "UserPromptSubmit", 4488.0, now=NOW - 10 - i)
    g1 = kpi._g1_hook(7, NOW, 0)
    sb = g1["hooks"]["status-bar"]
    assert sb["budget_ms"] == 300 and sb["p95_ms"] == 4488.0
    assert g1["value"].startswith("OVER budget: status-bar p95=4488ms>300ms")


def test_kpi_judges_router_added_and_reports_it_for_a_10s_draft():
    # 60 auto-route rows that spent 10 s in a local draft and 120 ms elsewhere.
    for i in range(60):
        hl.record("auto-route", "UserPromptSubmit", 10_120.0, now=NOW - 60 - i,
                  phases_ms={"import": 100.0, "draft_chain": 10_000.0})
    g1 = kpi._g1_hook(7, NOW, 0)
    ar = g1["hooks"]["auto-route"]
    assert ar["p95_ms"] == pytest.approx(120.0)
    assert ar["p95_elapsed_ms"] == 10_120.0
    assert ar["model_time_rows"] == 60
    assert g1["value"].startswith("all 1 measurable hook(s) within budget")
    assert "router-added p50=120ms p95=120ms vs 300ms budget" in g1["lines"][0]


def test_kpi_says_the_agent_route_bar_is_deferred():
    """agent-route's routed-model phases are not MODEL_PHASES, so it stays OVER
    the 300 ms bar; PLAN v16 S6 defers that bar to 16.1 and the output says so."""
    for i in range(60):
        hl.record("agent-route", "PreToolUse", 63_720.0, now=NOW - 60 - i,
                  phases_ms={"codex_delegation": 63_000.0})
    g1 = kpi._g1_hook(7, NOW, 0)
    ar = g1["hooks"]["agent-route"]
    assert ar["p95_ms"] == 63_720.0 and "16.1" in ar["deferred"]
    assert "codex_delegation" in g1["lines"][0] and "deferred to 16.1" in g1["lines"][0]


# ── router_added_ms on the row ───────────────────────────────────────────────


def _fake_clock(monkeypatch, start=1000.0):
    t = {"now": start}
    monkeypatch.setattr(hl, "_monotonic", lambda: t["now"])
    monkeypatch.setattr(hl, "_wall", lambda: NOW)
    return t


def test_the_row_carries_router_added_with_a_nested_cold_wait_subtracted_once(monkeypatch):
    t = _fake_clock(monkeypatch)
    hl.begin("auto-route", "UserPromptSubmit", t0=1000.0)
    t["now"] += 0.1  # 100 ms of import and routing
    with hl.phase("draft_chain"):
        hl.add_phase("cold_wait", 3000.0)  # Ollama's load time, inside the draft
        t["now"] += 9.0
    t["now"] += 0.05
    hl._finish()
    (row,) = _lines()
    assert row["phases_ms"]["draft_chain"] == 9000.0 and row["phases_ms"]["cold_wait"] == 3000.0
    assert row["router_added_ms"] == pytest.approx(150.0)
    assert hl.router_added_ms(row) == pytest.approx(150.0)


def test_a_cold_wait_outside_any_model_phase_is_subtracted(monkeypatch):
    t = _fake_clock(monkeypatch)
    hl.begin("auto-route", "UserPromptSubmit", t0=1000.0)
    hl.add_phase("cold_wait", 2000.0)
    t["now"] += 2.2
    hl._finish()
    (row,) = _lines()
    assert row["router_added_ms"] == pytest.approx(200.0)


def test_zce_model_counts_as_model_time(monkeypatch):
    t = _fake_clock(monkeypatch)
    hl.begin("auto-route", "UserPromptSubmit", t0=1000.0)
    with hl.phase("zce"):
        with hl.phase("zce_model"):
            t["now"] += 4.0
        t["now"] += 0.01
    hl._finish()
    (row,) = _lines()
    assert row["router_added_ms"] == pytest.approx(10.0)


def test_a_row_without_model_phases_is_unchanged_and_judged_on_elapsed(monkeypatch):
    t = _fake_clock(monkeypatch)
    hl.begin("status-bar", "UserPromptSubmit", t0=1000.0)
    with hl.phase("read_cache"):
        t["now"] += 0.02
    hl._finish()
    (row,) = _lines()
    assert "router_added_ms" not in row
    assert hl.router_added_ms(row) == row["elapsed_ms"] == 20.0


def test_router_added_for_rows_written_before_the_field_existed():
    # Old rows cannot say where cold_wait sat: subtract it only when no
    # enclosing model phase is present, so the reading never comes out low.
    assert hl.router_added_ms({"elapsed_ms": 10_500, "phases_ms": {"draft_chain": 10_000,
                                                                  "cold_wait": 4000}}) == 500
    assert hl.router_added_ms({"elapsed_ms": 4100, "phases_ms": {"cold_wait": 4000}}) == 100
    assert hl.router_added_ms({"elapsed_ms": 250}) == 250
    assert hl.router_added_ms({"elapsed_ms": None}) is None


def test_set_session_puts_the_session_id_on_the_row(monkeypatch):
    t = _fake_clock(monkeypatch)
    hl.begin("session-start", "SessionStart", t0=1000.0)
    hl.set_session("abc-123")
    hl.set_session(None)  # ignored
    t["now"] += 0.5
    hl._finish()
    (row,) = _lines()
    assert row["session_id"] == "abc-123"


def test_zero_claude_edit_times_its_model_call_as_zce_model():
    src = (REPO / "src" / "llm_router" / "zero_claude_edit.py").read_text()
    body = src[src.index("def generate_edits"):]
    i_phase = body.index('_hl_phase("zce_model")')
    i_call = body.index("call_ollama(", i_phase)
    assert 0 < i_call - i_phase < 200, "the zce model call is not inside the zce_model phase"


# ── task 2: record-raw and the statusline ────────────────────────────────────


def _env(home: Path, **extra) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("LLM_ROUTER_")}
    env.update(HOME=str(home), LLM_ROUTER_HOME=str(home / ".llm-router"),
               PYTHONPATH=str(REPO / "src"), **extra)
    return env


def test_record_raw_writes_one_row(tmp_path):
    home = tmp_path / "h"
    (home / ".llm-router").mkdir(parents=True)
    r = subprocess.run([sys.executable, "-m", "llm_router.hook_latency", "record-raw",
                        "statusline", "Statusline", "87"], env=_env(home),
                       capture_output=True, text=True, timeout=20)
    assert r.returncode == 0 and r.stdout == ""
    (row,) = [json.loads(x) for x in (home / ".llm-router" / "hook_latency.jsonl").read_text().splitlines()]
    assert (row["hook"], row["event"], row["elapsed_ms"], row["timed_out"]) == (
        "statusline", "Statusline", 87.0, False)


@pytest.mark.parametrize("args", [[], ["record-raw", "x", "E"], ["record-raw", "x", "E", "abc"],
                                  ["record-raw", "x", "E", "-4"], ["nope", "x", "E", "1"]])
def test_record_raw_rejects_bad_usage_without_writing(args):
    assert hl._main(args) == 2
    assert not hl.store_path().exists()


def _shim_python(tmp_path: Path) -> Path:
    """A `python3` on PATH that can import this checkout's llm_router."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    shim = bindir / "python3"
    shim.write_text(f'#!/bin/sh\nPYTHONPATH="{REPO / "src"}" exec "{sys.executable}" "$@"\n')
    shim.chmod(0o755)
    return bindir


def _run_statusline(home: Path, bindir: Path, **extra) -> float:
    env = _env(home, PATH=f"{bindir}:{os.environ.get('PATH', '')}", **extra)
    t = time.perf_counter()
    r = subprocess.run(["bash", str(STATUSLINE)], input='{"cwd": "/tmp"}', env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return (time.perf_counter() - t) * 1000.0


@pytest.mark.skipif(shutil.which("perl") is None, reason="needs perl (the macOS clock)")
def test_statusline_timing_all_writes_a_statusline_row(tmp_path):
    home = tmp_path / "h"
    (home / ".llm-router").mkdir(parents=True)
    _run_statusline(home, _shim_python(tmp_path), LLM_ROUTER_STATUSLINE_TIMING="all")
    row = _wait_row(home / ".llm-router" / "hook_latency.jsonl")
    assert row["hook"] == "statusline" and row["elapsed_ms"] > 0


def test_statusline_writes_nothing_when_timing_is_off(tmp_path):
    home = tmp_path / "h"
    (home / ".llm-router").mkdir(parents=True)
    _run_statusline(home, _shim_python(tmp_path))
    time.sleep(0.3)
    assert not (home / ".llm-router" / "hook_latency.jsonl").exists()


# ── P0.9 repair 1: the statusline row carries the session id ────────────────
# Without it a reader cannot count sessions or drop research / executor ones
# (PLAN v16 §1.4 rules 4 and 8), so P0.9-c could never be judged.

SID = "0b9e7c1a-5d2f-4e3b-9a61-7f0c2d4e8b15"


def test_record_raw_puts_the_session_id_on_the_row(tmp_path):
    home = tmp_path / "h"
    (home / ".llm-router").mkdir(parents=True)
    r = subprocess.run([sys.executable, "-m", "llm_router.hook_latency", "record-raw",
                        "statusline", "Statusline", "42", SID], env=_env(home),
                       capture_output=True, text=True, timeout=20)
    assert r.returncode == 0 and r.stdout == ""
    (row,) = [json.loads(x) for x in (home / ".llm-router" / "hook_latency.jsonl").read_text().splitlines()]
    assert (row["hook"], row["elapsed_ms"], row["session_id"]) == ("statusline", 42.0, SID)


def test_record_raw_leaves_an_empty_session_id_off_the_row():
    assert hl._main(["record-raw", "statusline", "Statusline", "42", "  "]) == 0
    (row,) = _lines()
    assert "session_id" not in row


def test_record_raw_rejects_six_args():
    assert hl._main(["record-raw", "x", "E", "1", SID, "extra"]) == 2
    assert not hl.store_path().exists()


def _wait_row(store: Path) -> dict:
    """The single row the backgrounded `record-raw` appends.

    Wait for a complete line, not for the file: capped_log.append does
    os.open(O_CREAT) then os.write, so the file exists, empty, for the gap
    between the two (CI py3.11 run 37901401651 read it there: "expected 1, got
    0"). The deadline is 15 s against a write that takes well under 1 s idle
    (one python start-up); it is only reached when the writer is truly lost."""
    deadline = time.monotonic() + 15
    rows: list[dict] = []
    while time.monotonic() < deadline:
        text = store.read_text() if store.exists() else ""
        if text.endswith("\n"):
            rows = [json.loads(x) for x in text.splitlines()]
            break
        time.sleep(0.05)
    (row,) = rows
    return row


def _run_statusline_with(home: Path, bindir: Path, stdin: str, **extra) -> None:
    env = _env(home, PATH=f"{bindir}:{os.environ.get('PATH', '')}", **extra)
    r = subprocess.run(["bash", str(STATUSLINE)], input=stdin, env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr


@pytest.mark.skipif(shutil.which("perl") is None, reason="needs perl (the macOS clock)")
@pytest.mark.parametrize("mode", ["full", "fast"])
def test_statusline_row_carries_the_session_id_from_stdin(tmp_path, mode):
    home = tmp_path / "h"
    (home / ".llm-router").mkdir(parents=True)
    _run_statusline_with(home, _shim_python(tmp_path),
                         json.dumps({"cwd": "/tmp", "session_id": SID}),
                         LLM_ROUTER_STATUSLINE_TIMING="all", LLM_ROUTER_STATUSLINE=mode)
    row = _wait_row(home / ".llm-router" / "hook_latency.jsonl")
    assert row["hook"] == "statusline" and row.get("session_id") == SID


@pytest.mark.skipif(shutil.which("perl") is None, reason="needs perl (the macOS clock)")
def test_statusline_row_is_written_when_bare_python3_cannot_import_llm_router(tmp_path):
    """The fast line, and a full line without usage.db, never resolve $_chz_py.
    The row must still be written through an interpreter that imports
    llm_router (here: the one behind the `llm-router` CLI), not bare python3."""
    home = tmp_path / "h"
    (home / ".llm-router").mkdir(parents=True)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    py3 = bindir / "python3"
    py3.write_text("#!/bin/sh\nexit 1\n")  # cannot import anything
    py3.chmod(0o755)
    cli = bindir / "llm-router"
    cli.write_text(f"#!{sys.executable}\nraise SystemExit(0)\n")
    cli.chmod(0o755)
    _run_statusline_with(home, bindir, json.dumps({"cwd": "/tmp", "session_id": SID}),
                         LLM_ROUTER_STATUSLINE_TIMING="all")
    row = _wait_row(home / ".llm-router" / "hook_latency.jsonl")
    assert row["hook"] == "statusline" and row.get("session_id") == SID


def _wrapper_only_script(tmp_path: Path) -> Path:
    """The timing block exactly as shipped, around an empty body."""
    text = STATUSLINE.read_text()
    start = text.index("# >>> statusline timing")
    end = text.index("# <<< statusline timing")
    script = tmp_path / "wrapper.sh"
    script.write_text("#!/bin/bash\n" + text[start:end] + "\ntrue\n")
    return script


def _counting_perl(tmp_path: Path, log: Path) -> Path:
    """A `perl` on PATH that logs each start, then runs the real perl."""
    bindir = tmp_path / "countbin"
    bindir.mkdir()
    shim = bindir / "perl"
    shim.write_text(f'#!/bin/sh\necho x >> "{log}"\nexec "{shutil.which("perl")}" "$@"\n')
    shim.chmod(0o755)
    return bindir


@pytest.mark.skipif(shutil.which("perl") is None, reason="needs perl (the macOS clock)")
def test_the_unsampled_path_starts_no_process_and_a_sampled_call_two_clock_reads(tmp_path):
    """The structural half of "the wrapper adds < 5 ms", independent of load:
    an unsampled call starts no perl, a sampled call starts exactly two (t0, t1)."""
    home = tmp_path / "h"
    (home / ".llm-router").mkdir(parents=True)
    script = _wrapper_only_script(tmp_path)
    log = tmp_path / "perl_starts.log"
    path = f"{_counting_perl(tmp_path, log)}:{_shim_python(tmp_path)}:{os.environ.get('PATH', '')}"

    def starts(timing: str | None) -> int:
        log.unlink(missing_ok=True)
        extra = {"LLM_ROUTER_STATUSLINE_TIMING": timing} if timing else {}
        subprocess.run(["bash", str(script)], env=_env(home, PATH=path, **extra),
                       capture_output=True, timeout=20)
        return len(log.read_text().splitlines()) if log.exists() else 0

    assert [starts(None) for _ in range(5)] == [0] * 5
    assert [starts("0") for _ in range(5)] == [0] * 5
    assert [starts("all") for _ in range(5)] == [2] * 5
    one_in_20 = [starts("1") for _ in range(40)]
    assert set(one_in_20) <= {0, 2}, one_in_20
    assert one_in_20.count(2) < 12, one_in_20  # P(>= 12 of 40 at p = 1/20) < 1e-7


@pytest.mark.skipif(shutil.which("perl") is None, reason="needs perl (the macOS clock)")
def test_the_timing_wrapper_adds_under_5ms_per_call(tmp_path):
    """At 1-in-20 sampling the wrapper's average cost per call is < 5 ms: the
    unsampled path starts no process, and a sampled call adds two perl clock
    reads and one backgrounded fork.

    Load-robust (review of #312: medians of separate blocks failed 6/6 at load
    ~65 and on CI): the arms run interleaved, so a load change hits both, and
    each arm is judged on its minimum, the best estimate of the intrinsic cost
    when the noise is one-sided (a busy machine only ever adds time)."""
    home = tmp_path / "h"
    (home / ".llm-router").mkdir(parents=True)
    script = _wrapper_only_script(tmp_path)
    bindir = _shim_python(tmp_path)

    def once(timing: str | None) -> float:
        extra = {"LLM_ROUTER_STATUSLINE_TIMING": timing} if timing else {}
        env = _env(home, PATH=f"{bindir}:{os.environ.get('PATH', '')}", **extra)
        t = time.perf_counter()
        subprocess.run(["bash", str(script)], env=env, capture_output=True, timeout=20)
        return (time.perf_counter() - t) * 1000.0

    off: list[float] = []
    sampled: list[float] = []
    every: list[float] = []
    for i in range(40):
        off.append(once(None))
        sampled.append(once("1"))
        if i % 3 == 0:
            every.append(once("all"))
    added_unsampled = min(sampled) - min(off)
    added_sampled = min(every) - min(off)
    # Mean added per call at 1-in-20: 19 unsampled + 1 sampled.
    per_call = (19 * max(added_unsampled, 0.0) + max(added_sampled, 0.0)) / 20
    assert per_call < 5.0, (round(added_unsampled, 2), round(added_sampled, 2),
                            round(statistics.median(off), 2))
