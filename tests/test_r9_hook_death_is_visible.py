"""R9 — a killed hook looked exactly like a hook that chose not to route.

Both end in a turn Claude answered directly. Measured during the audit: a 60s
timeout, a maximum observed hook duration of 55.3s, and 5.5% of real
invocations reaching no terminal outcome. Nothing distinguished "killed at 60s"
from "declined in 40ms", and the difference is the whole question of whether
routing is working.

The detection cannot live inside the hook, because the condition being detected
is "this process stopped executing". The evidence has to be on disk BEFORE the
kill, which is why it is a marker that outlives its writer rather than a
message the writer sends.

The two halves that matter, and both are asserted here:

  * a process that exits under its own control clears its marker, so a normal
    invocation is never counted as a kill;
  * a process that cannot clear it leaves one, and the NEXT invocation counts
    it once and removes it — a counter that re-counts the same event forever
    tells an operator the problem is growing when it is not.
"""

from __future__ import annotations

import ast
import json
import os
import pathlib
import subprocess
import sys
import time

import pytest

from llm_router import failopen, hook_liveness

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    failopen.clear()
    yield


def _write_marker(tmp_path, pid: int, age_seconds: float) -> pathlib.Path:
    d = tmp_path / hook_liveness.MARKER_DIR
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{pid}.json"
    p.write_text(json.dumps({"pid": pid, "started_at": time.time() - age_seconds}))
    return p


def _dead_pid() -> int:
    """A pid that is definitely not running: spawn and reap a trivial child."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_a_normal_exit_leaves_no_marker(tmp_path):
    hook_liveness.mark_started(1.0)
    assert hook_liveness.marker_path().exists()
    hook_liveness.clear_marker()
    assert not hook_liveness.marker_path().exists()
    assert hook_liveness.orphan_count() == 0


def test_a_marker_left_by_a_dead_process_is_counted_as_a_kill(tmp_path):
    pid = _dead_pid()
    marker = _write_marker(tmp_path, pid, age_seconds=300)

    assert hook_liveness.orphan_count() == 1, "the kill was not detected"
    assert hook_liveness.reap_orphans() == 1
    assert not marker.exists(), "the marker was counted but not removed"

    failopen.reset_cache()
    assert failopen.snapshot().by_code.get("CHZ-HOOK-KILLED") == 1, (
        "the kill was detected but recorded nowhere, so no surface can report it"
    )


def test_the_same_kill_is_not_counted_twice(tmp_path):
    _write_marker(tmp_path, _dead_pid(), age_seconds=300)
    assert hook_liveness.reap_orphans() == 1
    assert hook_liveness.reap_orphans() == 0, (
        "the same kill was counted again. A counter that re-counts one event "
        "forever reports a problem that is growing when it is not."
    )


def test_a_young_marker_is_not_a_kill(tmp_path):
    """A slow-but-alive hook must never be reported as killed."""
    _write_marker(tmp_path, _dead_pid(), age_seconds=1)
    assert hook_liveness.orphan_count() == 0
    assert hook_liveness.reap_orphans() == 0


def test_a_live_process_marker_is_left_alone(tmp_path):
    """Our own pid is alive, so an old marker for it is not a kill."""
    _write_marker(tmp_path, os.getpid(), age_seconds=999)
    assert hook_liveness.orphan_count() == 0
    assert hook_liveness.reap_orphans() == 0


def test_an_unknown_liveness_answer_undercounts_rather_than_inventing(monkeypatch, tmp_path):
    """Direction matters for a number an operator will act on."""
    _write_marker(tmp_path, _dead_pid(), age_seconds=300)

    def _explode(_pid):
        raise OSError("cannot determine")

    monkeypatch.setattr(os, "kill", _explode)
    assert hook_liveness.orphan_count() == 0, (
        "an undeterminable pid was reported as a kill; it must undercount"
    )


def test_the_counter_registry_reports_it(tmp_path):
    from llm_router import counter_registry

    _write_marker(tmp_path, _dead_pid(), age_seconds=300)
    r = counter_registry.read_one("hook_kills")
    assert r.value == 1.0, r
    assert r.alarming is True
    assert any(
        line.startswith("hook_kills: 1") for line in counter_registry.render_lines()
    ), counter_registry.render_lines()


def test_reading_the_count_does_not_change_it(tmp_path):
    """`orphan_count` must not reap.

    "Run doctor twice and the number changes" is its own kind of broken
    instrumentation — a reader with a side effect makes every report depend on
    how many times it was read.
    """
    _write_marker(tmp_path, _dead_pid(), age_seconds=300)
    assert hook_liveness.orphan_count() == 1
    assert hook_liveness.orphan_count() == 1
    assert hook_liveness.orphan_count() == 1


def test_the_hook_marks_before_the_work_and_registers_a_cleanup():
    """AST on the hook, not a substring.

    Two things must be true and neither is visible from behaviour in a unit
    test: the marker is written BEFORE the expensive work (a marker written
    after would only exist for invocations that completed), and the cleanup is
    registered with atexit so every exit this process controls clears it —
    including `sys.exit`, which the hook uses on at least five paths.
    """
    hook = SRC / "llm_router" / "hooks" / "auto-route.py"
    tree = ast.parse(hook.read_text(encoding="utf-8"))
    main = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "main"
    )
    calls = [
        (ast.unparse(c.func), c.lineno)
        for c in ast.walk(main) if isinstance(c, ast.Call)
    ]
    names = {n for n, _ in calls}
    for needed in ("hook_liveness.mark_started", "hook_liveness.reap_orphans",
                   "atexit.register"):
        assert needed in names, f"main() does not call {needed}"

    mark_line = min(
        lineno for name, lineno in calls
        if name == "hook_liveness.mark_started"
    )
    # `_route`/`classify` are the expensive part; the marker must precede the
    # bulk of main() rather than sit near its end.
    assert mark_line < main.lineno + 60, (
        f"mark_started is at line {mark_line}, {mark_line - main.lineno} lines "
        "into main(). It must run BEFORE the expensive work, or it only ever "
        "exists for invocations that finished."
    )
