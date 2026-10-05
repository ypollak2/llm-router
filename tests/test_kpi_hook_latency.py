"""KPI G1, hook half: every hook records how long it ran, and `kpi` reports it.

`llm-router kpi` used to print "hook latency is not instrumented". These tests
pin the replacement: the recorder (one append-only line per invocation, written
at process exit, no lock on the hot path, size-capped, fail-open), the one table
of budgets, and the p50 / p95 report -- including every empty-data case, which
must read "not measurable" and never a number.

The clock is a fake one throughout (`hook_latency._monotonic` / `_wall` and the
`now=` argument of the report), so no assertion depends on how fast this machine
happens to be.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from llm_router import capped_log, failopen, hook_latency as hl
from llm_router.commands import kpi

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / "src" / "llm_router" / "hooks"
NOW = 1_790_000_000.0  # fixed "now" for the report; every row below is placed relative to it
DAY = 86400.0


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    # compute_scorecard reads transcripts for other KPIs: never the operator's.
    (tmp_path / "claude-projects").mkdir()
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(tmp_path / "claude-projects"))
    monkeypatch.delenv("LLM_ROUTER_HOOK_LATENCY", raising=False)
    monkeypatch.delenv("LLM_ROUTER_HOOK_LATENCY_MAX_BYTES", raising=False)
    hl._pending = None
    failopen.reset_unpersisted()
    failopen.reset_cache()
    yield
    hl._pending = None
    failopen.reset_unpersisted()
    failopen.reset_cache()


def _rows(hook, elapsed_ms, *, n=1, ts=NOW - 60):
    for i in range(n):
        assert hl.record(hook, "E", elapsed_ms, now=ts - i)


def _lines():
    return [json.loads(x) for x in hl.store_path().read_text().splitlines()]


# ── the recorder ─────────────────────────────────────────────────────────────


def test_a_row_carries_hook_event_elapsed_timed_out_and_ts():
    assert hl.record("enforce-route", "PreToolUse", 212.449, now=NOW)
    assert _lines() == [{"hook": "enforce-route", "event": "PreToolUse", "elapsed_ms": 212.4,
                         "timed_out": False, "ts": NOW}]


def test_elapsed_runs_from_the_start_the_hook_gave_to_process_exit(monkeypatch):
    """begin() takes the hook's own t0 (taken before its first llm_router import);
    the exit handler measures to NOW on the same monotonic clock."""
    monkeypatch.setattr(hl, "_monotonic", lambda: 100.25)
    monkeypatch.setattr(hl, "_wall", lambda: NOW)
    hl.begin("auto-route", "UserPromptSubmit", t0=100.0)
    hl._finish()
    (row,) = _lines()
    assert row["elapsed_ms"] == 250.0 and row["ts"] == NOW and row["timed_out"] is False
    assert (row["hook"], row["event"]) == ("auto-route", "UserPromptSubmit")


def test_begin_twice_keeps_the_first_start(monkeypatch):
    monkeypatch.setattr(hl, "_monotonic", lambda: 10.0)
    hl.begin("auto-route", "UserPromptSubmit", t0=1.0)
    hl.begin("auto-route", "UserPromptSubmit", t0=9.0)
    hl._finish()
    assert _lines()[0]["elapsed_ms"] == 9000.0


def test_finish_without_begin_writes_nothing():
    hl._finish()
    assert not hl.store_path().exists()


def test_timed_out_means_the_whole_budget_was_used():
    budget = hl.HOOK_BUDGETS_MS["enforce-route"]
    hl.record("enforce-route", "PreToolUse", budget - 0.1, now=NOW)
    hl.record("enforce-route", "PreToolUse", budget, now=NOW)
    assert [r["timed_out"] for r in _lines()] == [False, True]


def test_the_off_switch_writes_nothing(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOOK_LATENCY", "off")
    assert hl.record("enforce-route", "PreToolUse", 5.0, now=NOW) is False
    hl.begin("enforce-route", "PreToolUse")
    assert hl._pending is None
    assert not hl.store_path().exists()


def test_a_write_failure_is_counted_not_raised(tmp_path, monkeypatch):
    """State dir under a regular file: mkdir fails. The hook must carry on, and
    the loss must be on the books (in memory -- the disk is what failed)."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    monkeypatch.setenv("LLM_ROUTER_HOME", str(blocker / "state"))
    assert hl.record("enforce-route", "PreToolUse", 5.0, now=NOW) is False
    assert failopen.snapshot().unpersisted_by_code == {"CHZ-FO-HOOK-LATENCY-WRITE": 1}


def test_negative_or_junk_elapsed_is_clamped_not_written_as_negative():
    hl.record("enforce-route", "PreToolUse", -5.0, now=NOW)
    assert _lines()[0]["elapsed_ms"] == 0.0
    assert hl.record("enforce-route", "PreToolUse", "nope", now=NOW) is False  # type: ignore[arg-type]


# ── the hot path costs four syscalls and no lock ────────────────────────────


def test_the_hot_path_is_one_open_write_fstat_close_and_takes_no_lock(monkeypatch):
    """The guardrail this module answers to: no measurable added latency. A lock,
    a read or a second path lookup on the warm path would be that latency."""
    hl.record("enforce-route", "PreToolUse", 1.0, now=NOW)  # warm: file + dir exist
    calls: list[str] = []
    for name in ("open", "write", "fstat", "close", "read", "stat", "lstat", "mkdir", "replace"):
        real = getattr(os, name)

        def spy(*a, _n=name, _r=real, **k):
            calls.append(_n)
            return _r(*a, **k)

        monkeypatch.setattr(capped_log.os, name, spy)
    from llm_router import file_lock

    def no_lock(*a, **k):
        raise AssertionError("the hot path must not take a lock")

    monkeypatch.setattr(file_lock, "exclusive_lock", no_lock)
    assert hl.record("enforce-route", "PreToolUse", 2.0, now=NOW)
    assert calls == ["open", "write", "fstat", "close"]


# ── size cap and rotation ────────────────────────────────────────────────────


def test_a_full_log_rotates_to_one_previous_generation(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOOK_LATENCY_MAX_BYTES", "3000")
    for i in range(200):
        hl.record("enforce-route", "PreToolUse", float(i), now=NOW + i)
    path = hl.store_path()
    prev = path.with_name(path.name + ".1")
    assert prev.exists(), "200 rows of ~110 bytes never crossed a 3000 byte cap"
    assert path.stat().st_size <= 3000 + 200
    assert prev.stat().st_size > 3000          # a FULL generation moved aside
    assert not path.with_name(path.name + ".2").exists()
    rows = hl.read_rows()                      # both generations, in order, none torn
    assert [r["elapsed_ms"] for r in rows] == sorted(r["elapsed_ms"] for r in rows)
    assert rows[-1]["elapsed_ms"] == 199.0


def test_rotation_is_skipped_not_waited_for_when_another_writer_holds_the_lock(monkeypatch):
    from llm_router.file_lock import exclusive_lock

    monkeypatch.setenv("LLM_ROUTER_HOOK_LATENCY_MAX_BYTES", "500")
    path = hl.store_path()
    hl.record("enforce-route", "PreToolUse", 1.0, now=NOW)
    lock = path.with_name(path.name + ".lock")
    with exclusive_lock(lock, timeout=1.0) as held:
        assert held
        started = time.monotonic()
        for i in range(10):
            assert hl.record("enforce-route", "PreToolUse", float(i), now=NOW + i)
        assert time.monotonic() - started < 1.0   # nobody waited for the lock
    assert path.stat().st_size > 500               # still over the cap: rotation was skipped
    assert not path.with_name(path.name + ".1").exists()
    assert len(_lines()) == 11                     # and no row was lost for it


def test_rotation_rechecks_the_size_so_a_small_file_never_replaces_a_full_generation():
    """Two writers both saw 'full'. The first rotated; the second must notice the
    file is now small and leave the full previous generation alone."""
    path = hl.store_path()
    prev = path.with_name(path.name + ".1")
    path.parent.mkdir(parents=True, exist_ok=True)
    prev.write_text("full generation\n" * 100)
    path.write_text('{"ts":1}\n')
    capped_log._rotate(path, max_bytes=500)
    assert prev.read_text() == "full generation\n" * 100
    assert path.read_text() == '{"ts":1}\n'


def test_max_bytes_env_falls_back_on_junk(monkeypatch):
    for junk in ("", "abc", "0", "-5"):
        monkeypatch.setenv("LLM_ROUTER_HOOK_LATENCY_MAX_BYTES", junk)
        assert hl.max_bytes() == hl._DEFAULT_MAX_BYTES
    monkeypatch.setenv("LLM_ROUTER_HOOK_LATENCY_MAX_BYTES", "1234")
    assert hl.max_bytes() == 1234


# ── several processes at once ────────────────────────────────────────────────

_WRITER = textwrap.dedent("""
    import os, sys, time
    from llm_router import hook_latency as hl
    name, n, go = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    while not os.path.exists(go):          # all writers released together
        time.sleep(0.001)
    for i in range(n):
        hl.record(name, "E", float(i), now=1790000000.0 + i)
""")


def _spawn_writers(tmp_path, env_extra, writers=6, per_writer=300):
    go = tmp_path / "go"
    env = {**os.environ, "LLM_ROUTER_HOME": os.environ["LLM_ROUTER_HOME"], **env_extra}
    procs = [subprocess.Popen([sys.executable, "-c", _WRITER, f"w{i}", str(per_writer), str(go)],
                              env=env, stderr=subprocess.PIPE) for i in range(writers)]
    go.write_text("go")
    for p in procs:
        _, err = p.communicate(timeout=120)
        assert p.returncode == 0, err.decode()


def test_concurrent_processes_lose_no_row_and_tear_no_line(tmp_path):
    _spawn_writers(tmp_path, {}, writers=6, per_writer=300)
    raw = hl.store_path().read_text().splitlines()
    assert len(raw) == 6 * 300
    rows = [json.loads(x) for x in raw]               # every line is whole JSON
    for w in range(6):
        mine = [r["elapsed_ms"] for r in rows if r["hook"] == f"w{w}"]
        assert sorted(mine) == [float(i) for i in range(300)], f"w{w} lost or duplicated rows"
    assert not hl.store_path().with_name(hl.STORE_FILENAME + ".1").exists()


def test_concurrent_processes_rotating_keep_every_surviving_line_whole(tmp_path):
    cap = 20_000
    _spawn_writers(tmp_path, {"LLM_ROUTER_HOOK_LATENCY_MAX_BYTES": str(cap)}, writers=6, per_writer=400)
    path = hl.store_path()
    prev = path.with_name(path.name + ".1")
    assert prev.exists(), "2400 rows (~250 KB) never crossed a 20 KB cap: the test proves nothing"
    # A full generation survived: two writers rotating the same file would have
    # replaced it with a nearly empty one.
    assert prev.stat().st_size > cap
    # Writers append after their fstat, so the live file may pass the cap by a few rows.
    assert path.stat().st_size < cap + 6 * 200
    for f in (path, prev):
        for line in f.read_text().splitlines():
            row = json.loads(line)                       # no torn or interleaved line
            assert set(row) == {"hook", "event", "elapsed_ms", "timed_out", "ts"}


# ── reading ──────────────────────────────────────────────────────────────────


def test_read_rows_skips_malformed_lines_and_never_reads_junk_as_zero():
    path = hl.store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join([
        '{"hook":"a","event":"E","elapsed_ms":5,"timed_out":false,"ts":10}',
        "{torn line",
        '{"hook":"a","event":"E","elapsed_ms":null,"timed_out":false,"ts":11}',
        '{"hook":"a","event":"E","elapsed_ms":true,"timed_out":false,"ts":12}',
        '{"hook":"a","event":"E","elapsed_ms":7,"timed_out":false,"ts":"13"}',
        '[1,2]',
        '{"hook":"a","event":"E","elapsed_ms":9,"timed_out":false,"ts":14}',
    ]) + "\n")
    assert [r["elapsed_ms"] for r in hl.read_rows()] == [5, 9]


def test_read_rows_window_is_inclusive_at_both_ends():
    for ts in (9.0, 10.0, 15.0, 20.0, 21.0):
        hl.record("a", "E", 1.0, now=ts)
    assert [r["ts"] for r in hl.read_rows(since=10.0, until=20.0)] == [10.0, 15.0, 20.0]


# ── the budget table ─────────────────────────────────────────────────────────


def test_agent_route_budget_is_the_registered_timeout():
    from llm_router.install_hooks import _AGENT_ROUTE_HOOK_TIMEOUT_SEC

    assert hl.HOOK_BUDGETS_MS["agent-route"] == _AGENT_ROUTE_HOOK_TIMEOUT_SEC * 1000 == 320_000


def test_an_unlisted_hook_is_held_to_the_default_not_to_no_budget():
    assert hl.budget_ms("some-new-hook") == hl.DEFAULT_BUDGET_MS > 0


# ── the report: every empty-data case reads "not measurable" or "too few" ───


def _g1(days=7, killed=None):
    return kpi._g1_hook(days, NOW, killed)


def test_no_rows_at_all_is_not_measurable():
    g1 = _g1()
    assert g1["value"].startswith("not measurable: no hook invocation recorded in window")
    assert g1["measurable"] is False and g1["n"] is None


def test_rows_only_outside_the_window_are_not_measurable():
    _rows("enforce-route", 100.0, n=80, ts=NOW - 8 * DAY)
    assert _g1(days=7)["value"].startswith("not measurable: ")
    assert _g1(days=9)["measurable"] is True


def test_just_under_the_minimum_n_says_too_few_and_at_it_measures():
    _rows("enforce-route", 100.0, n=kpi.MIN_N - 1)
    g1 = _g1()
    assert g1["value"] == (f"too few to tell (n={kpi.MIN_N - 1} rows over 1 hook(s); "
                           f"need >={kpi.MIN_N} per hook)")
    assert g1["measurable"] is False
    _rows("enforce-route", 100.0, n=1, ts=NOW - 7)
    assert _g1()["measurable"] is True


def test_p50_p95_per_hook_against_its_own_budget_exact():
    # enforce-route (budget 2000 ms): 100 x 100 ms and 10 x 5000 ms -> p95 is a slow one.
    _rows("enforce-route", 100.0, n=100, ts=NOW - 100)
    _rows("enforce-route", 5000.0, n=10, ts=NOW - 50)
    # auto-route (budget 60000 ms): all 4000 ms -> within ITS budget although over enforce-route's.
    _rows("auto-route", 4000.0, n=60, ts=NOW - 30)
    g1 = _g1()
    assert g1["hooks"]["enforce-route"] == {"n": 110, "budget_ms": 2000, "timed_out": 10,
                                            "p50_ms": 100.0, "p95_ms": 5000.0}
    assert g1["hooks"]["auto-route"] == {"n": 60, "budget_ms": 60_000, "timed_out": 0,
                                         "p50_ms": 4000.0, "p95_ms": 4000.0}
    assert g1["value"] == "OVER budget: enforce-route p95=5000ms>2000ms (n=170 invocations)"
    assert g1["lines"][0] == ("auto-route: p50=4000ms p95=4000ms vs 60000ms budget "
                              "(within budget); 0 of 60 hit the budget")
    assert g1["lines"][1] == ("enforce-route: p50=100ms p95=5000ms vs 2000ms budget "
                              "(OVER budget); 10 of 110 hit the budget")


def test_all_within_budget_names_the_worst_hook():
    _rows("enforce-route", 300.0, n=60)
    _rows("agent-route", 100.0, n=60)
    g1 = _g1()
    assert g1["value"] == "all 2 measurable hook(s) within budget (worst p95 enforce-route 300ms) (n=120 invocations)"


def test_a_thin_hook_is_named_not_averaged_into_the_rest():
    _rows("enforce-route", 300.0, n=60)
    _rows("session-start", 9000.0, n=3)
    g1 = _g1()
    assert "1 hook(s) too few to tell" in g1["value"]
    assert "session-start: too few to tell (n=3); budget 10000ms; 0 hit it" in g1["lines"]
    assert "p95" not in g1["hooks"]["session-start"]


def test_the_report_window_follows_the_fake_now():
    _rows("enforce-route", 100.0, n=60, ts=NOW - 2 * DAY)
    assert kpi._g1_hook(7, NOW, None)["measurable"] is True
    assert kpi._g1_hook(7, NOW + 6 * DAY, None)["measurable"] is False   # 2d-old rows are now 8d old


def test_hook_kills_are_reported_beside_the_log_because_they_leave_no_row():
    _rows("enforce-route", 100.0, n=60)
    assert "killed by the host (leaves no row; CHZ-HOOK-KILLED in the fail-open ledger): 2 in window" \
        in _g1(killed=2)["lines"]
    assert any(ln.startswith("killed by the host (leaves no row): not countable yet")
               for ln in _g1(killed=None)["lines"])


def test_killed_hooks_counts_only_timestamped_events_in_the_window(monkeypatch):
    monkeypatch.setattr(failopen, "_now", lambda: NOW - DAY)
    failopen.record("CHZ-HOOK-KILLED")
    failopen.record("CHZ-HOOK-KILLED")
    monkeypatch.setattr(failopen, "_now", lambda: NOW - 30 * DAY)
    failopen.record("CHZ-HOOK-KILLED")                      # outside the window
    assert kpi._killed_hooks(7, NOW) == 2
    # No timestamped event at all: unknown, not 0.
    failopen.clear()
    assert kpi._killed_hooks(7, NOW) is None


def test_a_timestamped_ledger_with_no_kill_is_a_real_zero(monkeypatch):
    monkeypatch.setattr(failopen, "_now", lambda: NOW - DAY)
    failopen.record("CHZ-FO-SOMETHING-ELSE")
    assert kpi._killed_hooks(7, NOW) == 0


def test_the_scorecard_prints_the_per_hook_lines(monkeypatch):
    _rows("enforce-route", 100.0, n=60, ts=time.time() - 60)
    text = kpi.render_scorecard(kpi.compute_scorecard(days=7))
    assert "G1  added latency (hook)" in text
    assert "enforce-route: p50=100ms p95=100ms vs 2000ms budget (within budget)" in text


# ── every instrumented hook, end to end and structurally ────────────────────

#: Each hook the installer registers for Claude Code, with the event it is
#: registered on. context-capture is the one registered hook NOT instrumented
#: (see the budget table's note), and a test below pins that it stays a
#: deliberate exception rather than a forgotten one.
_EVENTS = {
    "auto-route": "UserPromptSubmit", "status-bar": "UserPromptSubmit",
    "agent-route": "PreToolUse", "enforce-route": "PreToolUse",
    "session-start": "SessionStart", "subagent-start": "SubagentStart",
    "usage-refresh": "PostToolUse", "cc-usage-track": "PostToolUse",
    "agent-depth-release": "PostToolUse", "playwright-compress": "PostToolUse",
    "bash-compress": "PostToolUse", "session-end": "Stop",
}

#: A benign payload per hook for a REAL subprocess run. session-start is absent
#: on purpose: it spawns detached background processes and can reach the
#: network, so it is checked structurally only.
_PAYLOADS = {
    "enforce-route": {"session_id": "t-s1", "hook_event_name": "PreToolUse", "tool_name": "Read",
                      "tool_input": {"file_path": "/etc/hosts"}, "cwd": "/tmp"},
    "agent-route": {"session_id": "t-s1", "hook_event_name": "PreToolUse", "tool_name": "Agent",
                    "tool_input": {"subagent_type": "Explore", "description": "d",
                                   "prompt": "list files"}, "cwd": "/tmp"},
    "auto-route": {"session_id": "t-s1", "hook_event_name": "UserPromptSubmit", "prompt": "hi",
                   "cwd": "/tmp"},
    "status-bar": {"session_id": "t-s1", "hook_event_name": "UserPromptSubmit", "prompt": "hi",
                   "cwd": "/tmp"},
    "subagent-start": {"session_id": "t-s1", "hook_event_name": "SubagentStart",
                       "agent_type": "general-purpose", "agent_id": "a1", "cwd": "/tmp"},
    "usage-refresh": {"session_id": "t-s1", "hook_event_name": "PostToolUse",
                      "tool_name": "mcp__llm_router__llm", "tool_input": {},
                      "tool_response": {"content": [{"type": "text", "text": "ok"}]}, "cwd": "/tmp"},
    "cc-usage-track": {"session_id": "t-s1", "hook_event_name": "PostToolUse", "tool_name": "Agent",
                       "tool_input": {"subagent_type": "Explore"},
                       "tool_response": {"usage": {"input_tokens": 10, "output_tokens": 5}}, "cwd": "/tmp"},
    "agent-depth-release": {"session_id": "t-s1", "hook_event_name": "PostToolUse", "tool_name": "Agent",
                            "tool_input": {}, "tool_response": {}, "cwd": "/tmp"},
    "playwright-compress": {"session_id": "t-s1", "hook_event_name": "PostToolUse",
                            "tool_name": "mcp__playwright__browser_snapshot", "tool_input": {},
                            "tool_response": "short page", "cwd": "/tmp"},
    "bash-compress": {"session_id": "t-s1", "hook_event_name": "PostToolUse", "tool_name": "Bash",
                      "tool_input": {"command": "ls"}, "tool_response": {"stdout": "a\nb\n", "stderr": ""},
                      "cwd": "/tmp"},
    "session-end": {"session_id": "t-s1", "hook_event_name": "Stop",
                    "transcript_path": "/nonexistent", "cwd": "/tmp"},
}


def _run_hook(name, tmp_path):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "LLM_ROUTER_HOME": os.environ["LLM_ROUTER_HOME"],
           "LLM_ROUTER_ENFORCE": "smart", "LANG": "en_US.UTF-8"}
    return subprocess.run([sys.executable, str(HOOKS / f"{name}.py")],
                          input=json.dumps(_PAYLOADS[name]).encode(), env=env,
                          capture_output=True, timeout=120)


@pytest.mark.parametrize("name", sorted(_PAYLOADS))
def test_a_real_hook_process_leaves_exactly_one_row(name, tmp_path):
    before = time.time()
    proc = _run_hook(name, tmp_path)
    after = time.time()
    assert proc.returncode == 0, proc.stderr.decode()
    rows = [r for r in _lines() if r["hook"] == name]
    assert len(rows) == 1, _lines()
    row = rows[0]
    assert row["event"] == _EVENTS[name]
    assert before - 1 <= row["ts"] <= after + 1
    # Not an instant, and not longer than the whole subprocess took to run.
    assert 0 < row["elapsed_ms"] < (after - before) * 1000 + 50
    assert row["timed_out"] is False


def test_a_hook_that_exits_early_via_sys_exit_still_records(tmp_path):
    """enforce-route leaves main() through sys.exit(0) on this payload: the row
    comes from atexit, so no exit path of the hook can skip it."""
    assert _run_hook("enforce-route", tmp_path).returncode == 0
    assert len([r for r in _lines() if r["hook"] == "enforce-route"]) == 1


def test_importing_a_hook_as_a_module_arms_nothing(tmp_path):
    """A test (or any host) that imports the hook file must not get an exit-time
    write: the recorder is armed only when the file runs as a script."""
    code = textwrap.dedent(f"""
        import importlib.util
        spec = importlib.util.spec_from_file_location("imported_hook", {str(HOOKS / "enforce-route.py")!r})
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        from llm_router import hook_latency
        print(hook_latency._pending)
    """)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "LLM_ROUTER_HOME": os.environ["LLM_ROUTER_HOME"]}
    proc = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, timeout=120)
    assert proc.returncode == 0, proc.stderr.decode()
    assert proc.stdout.decode().strip().splitlines()[-1] == "None"
    assert not hl.store_path().exists()


def _module_level(body):
    """Statements that run at import: the module body and what sits inside its
    try / if blocks, but NOT the bodies of functions or classes."""
    for node in body:
        yield node
        if isinstance(node, ast.Try):
            for part in (node.body, node.orelse, node.finalbody, *[h.body for h in node.handlers]):
                yield from _module_level(part)
        elif isinstance(node, ast.If):
            yield from _module_level(node.body)
            yield from _module_level(node.orelse)


def _stanza(tree: ast.Module):
    """(the `_HOOK_T0` assignment, the `if __name__ == "__main__"` block that arms
    the recorder, the first import-time statement that imports llm_router)."""
    t0 = main_if = first_import = None
    for node in _module_level(tree.body):
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "_HOOK_T0" for t in node.targets):
            t0 = node
        elif isinstance(node, ast.If) and "__main__" in ast.unparse(node.test) \
                and "hook_latency" in ast.unparse(node):
            main_if = node
        elif first_import is None and isinstance(node, (ast.Import, ast.ImportFrom)) \
                and "llm_router" in ast.unparse(node) and "hook_latency" not in ast.unparse(node):
            first_import = node
    return t0, main_if, first_import


@pytest.mark.parametrize("name", sorted(_EVENTS))
def test_each_hook_starts_its_clock_before_its_first_llm_router_import(name):
    tree = ast.parse((HOOKS / f"{name}.py").read_text())
    t0, main_if, first_import = _stanza(tree)
    assert t0 is not None and main_if is not None, f"{name} lost its latency stanza"
    assert "time.monotonic()" in ast.unparse(t0)
    assert first_import is None or t0.lineno < first_import.lineno, (
        f"{name}: the clock starts after an llm_router import, so the import is not in the number")
    assert main_if.lineno > t0.lineno


@pytest.mark.parametrize("name", sorted(_EVENTS))
def test_each_hook_names_itself_and_its_event_as_the_installer_registers_it(name):
    from llm_router.install_hooks import _HOOK_DEFS

    registered = {src: event for src, _dst, event, _m in _HOOK_DEFS}
    assert registered[f"{name}.py"] == _EVENTS[name]
    _t0, main_if, _ = _stanza(ast.parse((HOOKS / f"{name}.py").read_text()))
    (call,) = [n for n in ast.walk(main_if) if isinstance(n, ast.Call)
               and ast.unparse(n.func) == "_hook_latency.begin"]
    assert [a.value for a in call.args[:2]] == [name, _EVENTS[name]]
    assert name in hl.HOOK_BUDGETS_MS


@pytest.mark.parametrize("name", sorted(_EVENTS))
def test_the_hooks_mirror_is_byte_identical(name):
    assert (REPO / "hooks" / f"{name}.py").read_bytes() == (HOOKS / f"{name}.py").read_bytes()


def test_every_registered_hook_is_instrumented_or_a_named_exception():
    """A hook added to the installer without the recorder would leave G1 silently
    partial. The only allowed gap is the one the budget table documents."""
    from llm_router.install_hooks import _HOOK_DEFS

    registered = {src.removesuffix(".py") for src, _dst, _event, _m in _HOOK_DEFS}
    assert registered - {"context-capture"} == set(_EVENTS), (
        "the installer registers a hook this test does not know (or dropped one): "
        f"{registered ^ (set(_EVENTS) | {'context-capture'})}")
    assert set(hl.HOOK_BUDGETS_MS) == set(_EVENTS), "the budget table and the instrumented hooks differ"
    assert "hook_latency" not in (HOOKS / "context-capture.py").read_text()


def test_the_stanza_is_the_same_in_every_hook_but_for_its_name_and_event():
    import re

    def block(text):
        start = text.index("# -- KPI G1: record how long this invocation ran")
        end = text.index("file=_hl_sys.stderr)\n", start) + len("file=_hl_sys.stderr)\n")
        return re.sub(r'begin\("[a-z-]+", "[A-Za-z]+",', 'begin("NAME", "EVENT",', text[start:end])

    blocks = {name: block((HOOKS / f"{name}.py").read_text()) for name in _EVENTS}
    assert len(set(blocks.values())) == 1, "the hooks' latency stanzas have drifted apart"


def test_the_stanza_catches_a_broken_recorder_without_touching_stdout(tmp_path):
    """A recorder that raises something other than ImportError must be reported on
    stderr and leave the hook running: the host parses stdout as JSON."""
    code = textwrap.dedent(f"""
        import sys, types
        broken = types.ModuleType("llm_router.hook_latency")
        def begin(*a, **k):
            raise RuntimeError("boom")
        broken.begin = begin
        import llm_router
        sys.modules["llm_router.hook_latency"] = broken
        llm_router.hook_latency = broken
        import runpy
        runpy.run_path({str(HOOKS / "enforce-route.py")!r}, run_name="__main__")
    """)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "LLM_ROUTER_HOME": os.environ["LLM_ROUTER_HOME"]}
    proc = subprocess.run([sys.executable, "-c", code], input=json.dumps(_PAYLOADS["enforce-route"]).encode(),
                          env=env, capture_output=True, timeout=120)
    assert "hook latency not recorded (RuntimeError)" in proc.stderr.decode()
    assert "hook latency" not in proc.stdout.decode()
    assert proc.returncode == 0


def test_the_two_env_vars_are_registered():
    from llm_router.env_registry import ENV_REGISTRY

    assert {"LLM_ROUTER_HOOK_LATENCY", "LLM_ROUTER_HOOK_LATENCY_MAX_BYTES"} <= set(ENV_REGISTRY)
