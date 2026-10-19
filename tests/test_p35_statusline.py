"""P35-2 (D-220): the per-session statusline snapshot, its refreshers and its shell row.

The five-field oracle itself (``test_statusline_prints_the_five_fields`` and friends) lives in
``test_p35_fixtures.py`` next to the fixture home. This file holds the edge cases: unknown prints
``n/a`` with its n and window, the stale rule, the refresher/tick wiring, the shell's hot path
(no python on a cache hit) and the p95 timing test.
"""
from __future__ import annotations

import json
import os
import re
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.test_p09c_statusline_budget import HOT_PATH_ALLOWED, STATUSLINE, _run, _stdin
from tests.test_p09c_statusline_budget import SID as SHELL_SID
from tests.test_p34a_ledger_view_identity import home  # noqa: F401  (a fixture the P35 home builds on)
from tests.test_p34a_six_surfaces import six  # noqa: F401  (a fixture)
from tests.test_p35_fixtures import NOW, SID, p35, seed_snapshot_extras  # noqa: F401  (p35 is a fixture)

REPO = Path(__file__).resolve().parents[1]
PERCENTILE_SAMPLES = 60
BAR_MS = 100.0


@pytest.fixture
def shell_home(tmp_path):
    h = tmp_path / "home"
    (h / ".llm-router").mkdir(parents=True)
    (h / ".llm-router" / ".statusline_python").write_text(sys.executable + "\n")
    return h


def _kv(state_home: Path, **fields) -> None:
    kv = {"v": "1", "written": str(int(time.time())), "health": "ok", **fields}
    (state_home / ".llm-router" / f"statusline_seg_{SHELL_SID}.kv").write_text("".join(f"{k}={v}\n" for k, v in kv.items()))


LINE = ("router: m (r) | offload n/a (n=3, session, no quality-sampled lane) | Claude 5h 37% (n=1 poll, age 1m, 5h)"
        " / Codex 3/15 delegations (5h) | quality 80% (n=10, 7d) | saved ~1.3 work-hours (n=2, rate n=12, today)")


# -- the line: unknown is n/a, never 0, and every field keeps its n and window -------------------

UNKNOWN_FIELDS = {
    "router": {"model": None, "reason": None, "ts": None},
    "offload": {"k": 0, "n": 0, "window": "session", "value": None, "why": None},
    "quota": {"claude": {"pct": None, "n": 0, "age_s": None, "window": "5h"},
              "codex": {"used": None, "budget": None, "window": "5h"}},
    "quality": {"n": 0, "k": 0, "rate": None, "wilson": None, "window": "7d"},
    "saved": {"value": None, "n": None, "window": "today", "as_of": "", "source": "", "kind": "estimated",
              "na_reason": "n/a (3 of 10 active hours)", "n_basis": 3},
}


def test_every_unknown_field_prints_na_with_its_n_and_window():
    from llm_router import statusline_snapshot as snap

    line = snap.render_line(UNKNOWN_FIELDS)
    fields = line.split(" | ")
    assert len(fields) == 5, line                        # something to check: all five are present
    assert fields[0] == "router: n/a"
    assert all("n/a" in f for f in fields), line
    assert "0%" not in line and "~0" not in line, line   # unknown is never printed as a number
    for f in fields[1:4]:
        assert "(n=" in f or "(n/a" in f, f
    assert fields[3] == "quality n/a (n=0, 7d)" and "Codex n/a (n/a, 5h)" in fields[2], line
    assert fields[4] == "saved n/a (3 of 10 active hours)", fields[4]     # one n/a, not "n/a (n/a (...))"


def test_offload_is_unknown_when_no_lane_is_quality_sampled_and_a_rate_when_one_is():
    from llm_router import statusline_snapshot as snap

    def act(sampled, numerator, sid="s"):
        return {"session_id": sid, "sampled": sampled, "numerator": numerator}

    unsampled = snap.offload_field([act(False, False)] * 3, "s")
    assert (unsampled["value"], unsampled["n"], unsampled["why"]) == (None, 3, "no quality-sampled lane")
    sampled = snap.offload_field([act(True, True), act(True, False), act(True, True), act(True, False, "other")], "s")
    assert (sampled["k"], sampled["n"], sampled["value"]) == (2, 3, 2 / 3)       # another session is not counted
    empty = snap.offload_field([], "s")
    assert (empty["value"], empty["n"]) == (None, 0)


def test_quota_is_unknown_for_a_fallback_or_stale_snapshot(tmp_path, monkeypatch):
    from llm_router import statusline_snapshot as snap

    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    usage = tmp_path / "usage.json"
    usage.write_text(json.dumps({"session_pct": 50, "weekly_pct": 50, "is_fallback": True, "updated_at": NOW}))
    assert snap.quota_field(NOW)["claude"]["pct"] is None            # the invented 50s are not data
    usage.write_text(json.dumps({"session_pct": 37, "weekly_pct": 40, "updated_at": NOW - 3600}))
    assert snap.quota_field(NOW)["claude"]["pct"] is None            # older than 30 min
    usage.write_text(json.dumps({"session_pct": 37, "weekly_pct": 40, "updated_at": NOW - 60}))
    assert snap.quota_field(NOW)["claude"]["pct"] == 37.0
    codex = snap.quota_field(NOW)["codex"]
    assert (codex["used"], codex["budget"]) == (None, None)          # never counted on this machine: n/a, not 0


async def test_router_falls_back_to_the_newest_routing_decision_without_a_proxy_ledger(p35):  # noqa: F811 -- pytest fixture name
    from llm_router import statusline_snapshot as snap

    (p35.home / "proxy_calls.jsonl").unlink()
    d = snap.refresh(host="claude_code", session_id=SID, now=NOW)
    # R1 (ollama, -10 min) is the newest runtime decision of s-main; the fixture records no reason for it
    assert d["line"].startswith("router: ollama (n/a) | offload "), d["line"]


async def test_the_snapshot_file_is_private_atomic_and_pruned(p35):  # noqa: F811 -- pytest fixture name
    from llm_router import statusline_snapshot as snap

    for i in range(snap.KEEP + 3):
        snap.refresh(host="claude_code", session_id=f"sess-{i}", now=NOW)
    files = list((p35.home / snap.DIR_NAME).glob("*.json"))
    assert len(files) == snap.KEEP, len(files)                       # the newest KEEP survive
    assert not list((p35.home / snap.DIR_NAME).glob("*.tmp"))
    assert all(stat.S_IMODE(f.stat().st_mode) == 0o600 for f in files)
    assert json.loads(files[0].read_text())["v"] == snap.SCHEMA


async def test_saved_is_worked_out_from_the_measured_turn_burn(p35):  # noqa: F811 -- pytest fixture name
    """~work-hours needs BOTH rates: no measured quota samples -> n/a, never a guess."""
    from llm_router import statusline_snapshot as snap

    # no quota_samples.jsonl in the bare P35 home: a turn off Claude, but no measured burn to multiply it by
    fig = snap.saved_field([_action()], NOW)
    assert fig.value is None and fig.na_reason == "n/a (quota saved unknown)", fig
    assert snap.render_line({**UNKNOWN_FIELDS, "saved": fig.to_dict()}).endswith("saved n/a (quota saved unknown)")
    d = snap.refresh(host="claude_code", session_id=SID, now=NOW)    # and no turn off Claude today either: n=0 first
    assert d["fields"]["saved"]["value"] is None and d["line"].endswith("saved n/a (n=0 turns off Claude, today)")
    seed_snapshot_extras(p35.home)
    # 2 turns off Claude today x 2.0 pp per turn / 3.0 pp per active hour (hand oracle in test_p35_fixtures)
    assert snap.refresh(host="claude_code", session_id=SID, now=NOW)["fields"]["saved"]["value"] == pytest.approx(4 / 3)


def _action(**kw):
    a = {"first": True, "door": "proxy", "served_class": "local", "redone": False, "ts": NOW - 60}
    return {**a, **kw}


def test_saved_counts_turns_that_replace_a_claude_turn_and_nothing_else():
    """rv-632 must-fix 1: the burn is weekly pp per human Stop turn, so only units that replace a Claude
    turn are multiplied by it."""
    from llm_router import statusline_snapshot as snap

    since = NOW - 3600
    counted = [_action(), _action(door="hook"), _action(served_class="very_cheap"), _action(served_class="spread"),
               _action(door="subagent")]                   # a sub-agent-labelled row the transcript put on the main thread
    left_out = [_action(first=False),                      # a continuation call of a turn
                _action(door="mcp"), _action(door="direct"),  # local_assist: inside a Claude turn
                _action(door="techop", first=False),
                _action(served_class="haiku"), _action(served_class="paid"),   # Claude / paid served it
                _action(redone=True),                      # Claude answered it again
                _action(ts=since - 1)]                     # before the window
    assert snap.turns_off_claude(counted, since) == len(counted) == 5
    assert snap.turns_off_claude(left_out, since) == 0
    assert snap.turns_off_claude(counted + left_out, since) == 5


async def test_saved_with_no_turn_off_claude_is_na_never_zero(p35):  # noqa: F811 -- pytest fixture name
    from llm_router import statusline_snapshot as snap

    seed_snapshot_extras(p35.home)
    fig = snap.saved_field([_action(first=False)], NOW)                # burn and rate known, n=0 turns
    assert fig.value is None and fig.n == 0 and fig.na_reason == "n=0 turns off Claude, today", fig
    fields = {**UNKNOWN_FIELDS, "saved": fig.to_dict()}
    assert snap.render_line(fields).endswith("saved n/a (n=0 turns off Claude, today)")


def test_saved_with_no_turn_and_no_rate_names_the_n_0_turns(tmp_path, monkeypatch):
    """rv-632 r2: n=0 turns AND an unknown rate (no quota history): the reason is the n=0 turns, the
    count the figure would multiply, not the rate's own n/a."""
    from llm_router import statusline_snapshot as snap

    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    fig = snap.saved_field([], NOW)
    assert (fig.value, fig.n, fig.na_reason) == (None, 0, "n=0 turns off Claude, today"), fig
    assert snap.render_line({**UNKNOWN_FIELDS, "saved": fig.to_dict()}).endswith(
        "saved n/a (n=0 turns off Claude, today)")


def test_claude_5h_names_its_one_poll_and_is_never_a_value_with_n_0(tmp_path, monkeypatch):
    """rv-632 must-fix 3: the % is one usage.json poll, so its n is that poll (plus its age); with no
    poll it is n/a. History rows (only the fast tick wrote them) are not its n."""
    from llm_router import statusline_snapshot as snap

    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    (tmp_path / "quota_history.jsonl").write_text("".join(json.dumps(
        {"ts": NOW - i * 300, "source": "measured", "five_hour_pct": 1.0, "weekly_pct": 1.0}) + "\n" for i in range(5)))
    q = snap.quota_field(NOW)                                         # history rows, but no poll
    assert (q["claude"]["pct"], q["claude"]["n"]) == (None, 0)
    line = snap.render_line({**UNKNOWN_FIELDS, "quota": q})
    assert "Claude 5h n/a (n=0, 5h)" in line, line
    (tmp_path / "usage.json").write_text(json.dumps({"session_pct": 37, "weekly_pct": 40, "updated_at": NOW - 150}))
    q = snap.quota_field(NOW)
    assert (q["claude"]["pct"], q["claude"]["n"], q["claude"]["age_s"]) == (37.0, 1, 150.0)
    line = snap.render_line({**UNKNOWN_FIELDS, "quota": q})
    assert "Claude 5h 37% (n=1 poll, age 2m, 5h)" in line, line
    lines = [line]
    # D-237: the Codex half too. One delegation 6 h ago: the file exists, nothing in its 5 h window. That is
    # a measured count of 0 delegations against the budget, never "0% (n=0)".
    monkeypatch.delenv("LLM_ROUTER_CODEX_WINDOW_BUDGET", raising=False)   # the module default budget, 15
    (tmp_path / "codex_window.json").write_text(json.dumps({"delegations": [NOW - 6 * 3600]}))
    q = snap.quota_field(NOW)
    assert (q["codex"]["used"], q["codex"]["budget"]) == (0, 15), q["codex"]
    line = snap.render_line({**UNKNOWN_FIELDS, "quota": q})
    assert "Claude 5h 37% (n=1 poll, age 2m, 5h) / Codex 0/15 delegations (5h) |" in line, line
    lines.append(line)
    (tmp_path / "usage.json").unlink()                               # and with no Claude poll either
    lines.append(snap.render_line({**UNKNOWN_FIELDS, "quota": snap.quota_field(NOW)}))
    assert "Claude 5h n/a (n=0, 5h) / Codex 0/15 delegations (5h) |" in lines[-1], lines[-1]
    for ln in lines:
        for f in ln.split(" | "):
            assert not re.search(r"\d+% \(n=0\b", f), f"a value printed with n=0: {f}"
            # any "(n=0" on the row belongs to a field that prints n/a, never to a number
            assert "(n=0" not in f or " n/a (" in f, f"a value printed with n=0: {f}"


# -- the refreshers: one snapshot function, one CLI, the tick adds two stats ----------------------

def test_the_cli_writes_the_snapshot_and_skips_a_fresh_global_cache(tmp_path, monkeypatch):
    import signal

    from llm_router import statusline_refresh as sr
    from llm_router import statusline_snapshot as snap

    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.setattr(signal, "alarm", lambda s: None)          # the watchdog must not outlive the test
    calls = []
    monkeypatch.setattr(sr, "refresh", lambda now=None: calls.append("global") or True)
    (tmp_path / sr.CACHE_NAME).write_text(json.dumps({"written_at": time.time()}))
    assert sr.cmd_statusline(["--refresh", "--host", "codex", "--session", "S1"]) == 0
    assert (tmp_path / "snapshots" / "codex-S1.json").is_file()
    assert snap.read("codex", "S1")["session_id"] == "S1"
    assert calls == [], "a fresh global cache was recomputed by a ledger-triggered snapshot run"
    (tmp_path / sr.CACHE_NAME).write_text(json.dumps({"written_at": time.time() - 3600}))
    assert sr.cmd_statusline(["--refresh", "--session", "S2"]) == 0
    assert calls == ["global"] and (tmp_path / "snapshots" / "claude_code-S2.json").is_file()
    assert sr.cmd_statusline(["--refresh"]) == 0 and calls == ["global", "global"]      # no --session: as before
    with pytest.raises(SystemExit):
        sr.cmd_statusline(["--refresh", "--session", "../escape"])


def test_the_tick_spawns_when_a_ledger_changed_after_the_cache_but_not_twice_in_ten_seconds(tmp_path, monkeypatch):
    from llm_router import statusline_tick as tick

    monkeypatch.delenv("LLM_ROUTER_STATUSLINE_REFRESH_CMD", raising=False)
    monkeypatch.setattr(tick, "_refresh_argv", lambda: ["/bin/llm-router", "statusline", "--refresh"])
    spawned = []
    monkeypatch.setattr(tick, "_spawn_detached", lambda argv: spawned.append(argv))
    now = 1_000_000.0
    cache = {"written_at": now - 20}                                  # younger than the 60 s refresh age
    assert tick.maybe_refresh(str(tmp_path), cache, now, "s1") is False       # no ledger at all
    proxy = tmp_path / "proxy_calls.jsonl"
    proxy.write_text("{}\n")
    os.utime(proxy, (now - 60, now - 60))                             # older than the cache
    assert tick.maybe_refresh(str(tmp_path), cache, now, "s1") is False
    os.utime(proxy, (now - 5, now - 5))                               # written after the cache
    assert tick.maybe_refresh(str(tmp_path), cache, now, "s1") is True
    assert spawned == [["/bin/llm-router", "statusline", "--refresh", "--host", "claude_code", "--session", "s1"]]
    assert tick.maybe_refresh(str(tmp_path), cache, now + 5, "s1") is False   # < 10 s since the last spawn stamp
    assert tick.maybe_refresh(str(tmp_path), cache, now + 11, "s1") is True
    # the WAL is the live mtime of the database: a stale wal decides, the db file is not consulted
    (tmp_path / "usage.db-wal").write_text("")
    (tmp_path / "usage.db").write_text("")
    os.utime(tmp_path / "usage.db-wal", (now - 60, now - 60))
    os.utime(proxy, (now - 60, now - 60))
    assert tick.ledger_changed_since(str(tmp_path), now - 20) is False
    assert tick.session_of(b'{"session_id": "abc-1", "x": 1}') == "abc-1"
    assert tick.session_of(b'{"session_id": "a b"}') is None


# -- the segments refresher publishes the line; the shell prints it ---------------------------------

def test_the_refresher_publishes_nothing_for_a_foreign_state_dir(tmp_path, monkeypatch):
    from llm_router import statusline_segments as ss

    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "router"))
    assert ss.snap_segment(str(tmp_path / "other"), "s1", NOW) == {}          # never reads a home it was not given
    assert ss.snap_segment(str(tmp_path / "router"), "default", NOW) == {}    # no session id to scope it by
    (tmp_path / "router").mkdir()
    assert ss.snap_segment(str(tmp_path / "router"), "s1", NOW, refresh=False) == {}   # nothing on disk yet


def test_the_shell_prints_the_snapshot_row_and_na_past_15_minutes(shell_home, tmp_path):
    _kv(shell_home, snap_line=LINE, snap_at=str(int(time.time()) - 60))
    out, _ = _run(shell_home, tmp_path, _stdin(tmp_path))
    assert out.strip().splitlines()[-1] == LINE, out
    _kv(shell_home, snap_line=LINE, snap_at=str(int(time.time()) - 901))
    out, _ = _run(shell_home, tmp_path, _stdin(tmp_path))
    assert out.strip().splitlines()[-1] == "n/a", out
    _kv(shell_home, snap_line=LINE)                                         # no stamp: age unknown, so stale
    out, _ = _run(shell_home, tmp_path, _stdin(tmp_path))
    assert out.strip().splitlines()[-1] == "n/a", out
    _kv(shell_home)                                                         # no snapshot key: no row at all
    out, _ = _run(shell_home, tmp_path, _stdin(tmp_path))
    assert "router:" not in out and out.strip().splitlines()[-1] != "n/a", out


def test_a_control_character_in_the_snapshot_never_reaches_the_terminal(shell_home, tmp_path):
    _kv(shell_home, snap_line="router: x\x1b[31mred\x07 | quality 1% (n=1, 7d)", snap_at=str(int(time.time())))
    out, _ = _run(shell_home, tmp_path, _stdin(tmp_path))
    assert "\x07" not in out and "[31mred" in out.replace("\x1b", ""), out
    assert "\x1b[31m" not in out, "an escape from the cache file reached the terminal"


def test_a_cache_hit_with_a_snapshot_starts_no_process_but_the_clock(shell_home, tmp_path):
    _kv(shell_home, snap_line=LINE, snap_at=str(int(time.time())))
    out, spawned = _run(shell_home, tmp_path, _stdin(tmp_path))
    assert LINE in out
    assert not set(spawned) - HOT_PATH_ALLOWED, f"the cache-hit render started {sorted(set(spawned))}"


@pytest.mark.timing
def test_the_cache_hit_render_p95_is_inside_the_statusline_bar(shell_home, tmp_path):
    """60 wall-clock samples of the real script on a cache hit (Popen to exit, no shims); the live 200-sample
    figure is ``scripts/statusline_wall.py`` against a prepared home."""
    sys.path.insert(0, str(REPO / "scripts"))
    try:
        import statusline_wall as sw
    finally:
        sys.path.pop(0)
    _kv(shell_home, snap_line=LINE, snap_at=str(int(time.time())), money="~$1.00 est", money_n="3", money_win="today")
    env = {"HOME": str(shell_home), "PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8", "NO_COLOR": "1",
           "LLM_ROUTER_ENFORCE": "smart"}
    payload = _stdin(tmp_path).encode()
    samples = []
    for i in range(PERCENTILE_SAMPLES + 3):
        t0 = time.perf_counter()
        r = subprocess.run(["/bin/bash", str(STATUSLINE)], input=payload, env=env, capture_output=True, timeout=60)
        if i >= 3:                                                    # three discarded priming runs
            samples.append((time.perf_counter() - t0) * 1000.0)
        assert r.returncode == 0 and LINE.encode() in r.stdout
        _kv(shell_home, snap_line=LINE, snap_at=str(int(time.time())), money="~$1.00 est", money_n="3", money_win="today")
    assert len(samples) == PERCENTILE_SAMPLES
    p95 = sw.percentile(samples, 0.95)
    assert p95 <= BAR_MS, f"p95 {p95:.1f} ms over the {BAR_MS:.0f} ms statusline bar (n={len(samples)})"


# -- rv-632: the refresher's hard stop survives the snapshot budget --------------------------------

_DRIVER = r'''
import sys, time
from llm_router import statusline_segments as ss, statusline_snapshot as snap

state, hard, budget, mode = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
ss.RUN_TIMEOUT_S, ss.SNAP_BUDGET_S = hard, budget
ss.maybe_refresh_usage = lambda *a: None
ss.compute = lambda *a: {"health": "ok"}
ss.append_history = lambda *a: False


def hang(*a, **k):
    while True:
        time.sleep(0.05)


def slow(*a, **k):
    try:
        time.sleep(5)                 # the budget fires in here ...
    except Exception:                 # ... and a field's own catch-all must not end the stop
        pass
    return []


snap._actions = slow                  # the first field: slow
snap.quality_field = hang             # a later field: hangs (it never returns)
if mode == "after":                   # the snapshot is fine; what comes after it hangs
    ss.snap_segment = lambda *a, **k: {}
    ss.write_cache = lambda *a: hang()
if mode == "escape":                  # the budget fired mid-restore: _within raised with its own handler
    import signal                     # still installed and no timer armed; then the cache write hangs

    def _raise(*_):
        raise ss.SnapshotBudgetExceeded("late")

    def escaped(seconds, fn, started):
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, _raise)
        raise ss.SnapshotBudgetExceeded("budget fired after fn returned, before the restore")

    ss._within = escaped
    ss.write_cache = lambda *a: hang()
sys.exit(ss.main(["--state", state, "--session", "s1"]))
'''


#: Past this the refresher is still running: the hard stop did not end it (the bug these tests close).
_DRIVE_TIMEOUT_S = 15


def _drive(tmp_path, mode: str, hard: int = 3, budget: int = 1):
    """Integer seconds: the pre-rv-632 code armed ``signal.alarm(int)``, so on that code this driver runs
    and HANGS (killed at :data:`_DRIVE_TIMEOUT_S`) instead of failing early on a float."""
    state = tmp_path / "router"
    state.mkdir(exist_ok=True)
    env = {**os.environ, "HOME": str(tmp_path), "LLM_ROUTER_HOME": str(state)}
    t0 = time.monotonic()
    try:
        r = subprocess.run([sys.executable, "-c", _DRIVER, str(state), str(hard), str(budget), mode], env=env,
                           capture_output=True, text=True, timeout=_DRIVE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        pytest.fail(f"refresher still running after {_DRIVE_TIMEOUT_S} s (hard stop {hard} s): nothing ended it")
    return r, time.monotonic() - t0, state


@pytest.mark.skipif(not hasattr(signal, "SIGALRM"), reason="SIGALRM watchdog is POSIX only")
def test_a_slow_field_then_a_hanging_field_still_ends_the_refresher(tmp_path):
    """rv-632 must-fix 2. Before: the budget's TimeoutError was swallowed inside the slow field, the
    snapshot went on into the hanging field with no alarm armed, and the refresher held its lock for
    ever (this run hit the 30 s subprocess timeout). Now the budget unwinds the snapshot and the line is
    written without the row."""
    r, took, state = _drive(tmp_path, "fields")
    assert r.returncode == 0, (r.returncode, r.stderr[-2000:])
    assert took < 3 + 5.0, took                                     # well inside the injected hard stop
    kv = (state / "statusline_seg_s1.kv").read_text()
    assert "health=ok" in kv and "snap_line" not in kv, kv


@pytest.mark.skipif(not hasattr(signal, "SIGALRM"), reason="SIGALRM watchdog is POSIX only")
def test_the_hard_stop_is_rearmed_after_the_snapshot_block(tmp_path):
    """Whatever hangs after the snapshot is ended by the re-armed hard stop (os._exit(1))."""
    r, took, _ = _drive(tmp_path, "after", hard=2)
    assert r.returncode == 1, (r.returncode, r.stderr[-2000:])        # the hard stop's own exit code
    assert 1.5 <= took < 2 + 5.0, took


@pytest.mark.skipif(not hasattr(signal, "SIGALRM"), reason="SIGALRM watchdog is POSIX only")
def test_main_rearms_the_hard_stop_when_the_budget_escapes_mid_restore(tmp_path):
    """rv-632 r2: the one window _within cannot close (the budget firing after fn returned, before the
    restore) leaves its own handler and no timer; main's except puts the os._exit hard stop back."""
    r, took, _ = _drive(tmp_path, "escape", hard=2)
    assert r.returncode == 1, (r.returncode, r.stderr[-2000:])
    assert 1.5 <= took < 2 + 5.0, took


@pytest.mark.skipif(not hasattr(signal, "SIGALRM"), reason="SIGALRM watchdog is POSIX only")
def test_a_budget_signal_still_pending_after_fn_is_not_handed_to_the_hard_stop():
    """rv-632 r2: SIGALRM is blocked while _within swaps back, and the budget's own signal, if it is
    still pending once fn has returned, is dropped. Before, it reached the restored hard stop
    (os._exit) and ended a refresher that had finished in time."""
    from llm_router import statusline_segments as ss

    alrm = {signal.SIGALRM}
    fired = []
    old = signal.signal(signal.SIGALRM, lambda *_: fired.append(1))  # stands in for os._exit in-process

    def fn():
        signal.pthread_sigmask(signal.SIG_BLOCK, alrm)
        time.sleep(0.3)                     # the 0.05 s budget fires while blocked: pending on return
        assert signal.SIGALRM in signal.sigpending()
        return "done"

    try:
        assert ss._within(0.05, fn, started=time.time()) == "done"
        signal.pthread_sigmask(signal.SIG_UNBLOCK, alrm)
        time.sleep(0.01)
        assert not fired, "the budget's signal reached the hard stop"
        assert 0 < signal.getitimer(signal.ITIMER_REAL)[0] <= ss.RUN_TIMEOUT_S   # the hard stop is armed again
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.pthread_sigmask(signal.SIG_UNBLOCK, alrm)
        signal.signal(signal.SIGALRM, old)


@pytest.mark.skipif(not hasattr(signal, "SIGALRM"), reason="SIGALRM watchdog is POSIX only")
def test_the_snapshot_budget_is_what_is_left_of_the_hard_stop(monkeypatch):
    """min(SNAP_BUDGET_S, remaining of RUN_TIMEOUT_S), never a fresh 10 s; afterwards the remaining hard
    stop is armed again."""
    from llm_router import statusline_segments as ss

    fired = []
    old = signal.signal(signal.SIGALRM, lambda *_: fired.append(1))  # stands in for os._exit in-process
    try:
        started = time.time() - (ss.RUN_TIMEOUT_S - 3.0)               # 3 s of the hard stop left
        armed = ss._within(10, lambda: signal.getitimer(signal.ITIMER_REAL)[0], started=started)
        assert 0 < armed <= 3.0, armed
        rearmed = signal.getitimer(signal.ITIMER_REAL)[0]
        assert 0 < rearmed <= 3.0, rearmed
        assert signal.getsignal(signal.SIGALRM) is not None and not fired
        with pytest.raises(ss.SnapshotBudgetExceeded):
            ss._within(0.05, lambda: time.sleep(2), started=time.time())
        assert not isinstance(ss.SnapshotBudgetExceeded("x"), Exception)   # no `except Exception` catches it
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


# -- rv-632 non-blocking: tick stamp, history writer, C1 controls ----------------------------------

def test_a_snapshot_only_run_stops_the_ledger_respawn(tmp_path, monkeypatch):
    """The ledger change is judged against the session's snapshot, which a snapshot-only run moves (the
    global cache stamp it does not move made the tick respawn every 10 s for up to a minute)."""
    from llm_router import statusline_tick as tick

    monkeypatch.delenv("LLM_ROUTER_STATUSLINE_REFRESH_CMD", raising=False)
    monkeypatch.setattr(tick, "_refresh_argv", lambda: ["/bin/llm-router", "statusline", "--refresh"])
    spawned = []
    monkeypatch.setattr(tick, "_spawn_detached", lambda argv: spawned.append(argv))
    now = 1_000_000.0
    cache = {"written_at": now - 30}
    proxy = tmp_path / "proxy_calls.jsonl"
    proxy.write_text("{}\n")
    os.utime(proxy, (now - 20, now - 20))                             # after the cache
    snap_file = tmp_path / "snapshots" / "claude_code-s1.json"
    snap_file.parent.mkdir()
    snap_file.write_text("{}")
    os.utime(snap_file, (now - 15, now - 15))                         # ... but the snapshot has it
    assert tick.maybe_refresh(str(tmp_path), cache, now, "s1") is False and spawned == []
    os.utime(proxy, (now - 5, now - 5))                               # a new write after the snapshot
    assert tick.maybe_refresh(str(tmp_path), cache, now, "s1") is True and len(spawned) == 1
    snap_file.unlink()                                                # no snapshot: the cache stamp decides
    assert tick.maybe_refresh(str(tmp_path), cache, now + 11, "s1") is True


def test_the_segments_refresher_writes_the_quota_history_too(tmp_path, monkeypatch):
    """rv-632 (a): the default layout had no history writer, so saved was always n/a there."""
    from llm_router import statusline_segments as ss

    rhome = tmp_path / "router"
    rhome.mkdir()
    monkeypatch.setenv("LLM_ROUTER_HOME", str(rhome))
    now = time.time()
    (rhome / "usage.json").write_text(json.dumps({"session_pct": 37, "weekly_pct": 40, "updated_at": now - 60}))
    assert ss.append_history(str(tmp_path / "other"), now) is False   # never a rhome it was not given
    assert not (tmp_path / "other" / "quota_history.jsonl").exists()
    assert ss.append_history(str(rhome), now) is True
    assert ss.append_history(str(rhome), now + 10) is False            # one row per 5-minute slot (tick's own lock)
    rows = [json.loads(ln) for ln in (rhome / "quota_history.jsonl").read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["source"] == "measured" and rows[0]["weekly_pct"] == 40, rows
    assert stat.S_IMODE((rhome / "quota_history.jsonl").stat().st_mode) == 0o600


def test_c1_controls_are_stripped_in_python_and_in_the_shell(shell_home, tmp_path):
    from llm_router import statusline_segments as ss
    from llm_router import statusline_snapshot as snap

    assert snap._clean("m\x9b31mX\x85y") == "m 31mX y"
    assert ss._clean("a\x9bb\x1bc") == "a b c"
    _kv(shell_home, snap_line="router: x\u009b31mred\u0085 café | quality 1% (n=1, 7d)",
        snap_at=str(int(time.time())))
    out, _ = _run(shell_home, tmp_path, _stdin(tmp_path))
    assert "\u009b" not in out and "\u0085" not in out, repr(out)
    assert "router: x31mred café | quality 1% (n=1, 7d)" in out, repr(out)   # multibyte text is kept
