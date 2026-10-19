"""P35-2 (D-220): the per-session statusline snapshot, its refreshers and its shell row.

The five-field oracle itself (``test_statusline_prints_the_five_fields`` and friends) lives in
``test_p35_fixtures.py`` next to the fixture home. This file holds the edge cases: unknown prints
``n/a`` with its n and window, the stale rule, the refresher/tick wiring, the shell's hot path
(no python on a cache hit) and the p95 timing test.
"""
from __future__ import annotations

import json
import os
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


LINE = ("router: m (r) | offload n/a (n=3, session, no quality-sampled lane) | Claude 5h 37% (n=3, 5h) / "
        "Codex 20% (n=3, 5h) | quality 80% (n=10, 7d) | saved ~2.7 work-hours (n=4, rate n=12, today)")


# -- the line: unknown is n/a, never 0, and every field keeps its n and window -------------------

UNKNOWN_FIELDS = {
    "router": {"model": None, "reason": None, "ts": None},
    "offload": {"k": 0, "n": 0, "window": "session", "value": None, "why": None},
    "quota": {"claude": {"pct": None, "n": 0, "window": "5h"}, "codex": {"pct": None, "n": None, "window": "5h"}},
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
    assert fields[4] == "saved n/a (n/a (3 of 10 active hours))", fields[4]


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
    assert snap.quota_field(NOW)["codex"]["pct"] is None             # never counted on this machine: n/a, not 0


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

    d = snap.refresh(host="claude_code", session_id=SID, now=NOW)    # no quota_samples.jsonl in the bare P35 home
    assert d["fields"]["saved"]["value"] is None and d["line"].endswith("saved n/a (n/a (quota saved unknown))")
    seed_snapshot_extras(p35.home)
    assert snap.refresh(host="claude_code", session_id=SID, now=NOW)["fields"]["saved"]["value"] == pytest.approx(8 / 3)


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
