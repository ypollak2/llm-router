"""F68: the statusline's timing row no longer starts a clock process.

The live statusline p95 was over its 100 ms bar, worst under machine load. The
render is pure bash; the sampled number included two `perl -MTime::HiRes` start-ups (one per clock read). Now start and end
are each a shell redirect and the row is written from the two file mtimes by
``hook_latency record-stamps``, with ``phases_ms = {cache_hit | cache_stale |
cache_miss | fast: elapsed}`` so the render path is measurable.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from llm_router import hook_latency as hl

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "src" / "llm_router" / "hooks" / "statusline-command.sh"
STDIN = '{"session_id":"f68","cwd":"/x/proj"}'


def _env(home: Path, bindir: Path, **extra) -> dict:
    # sys.executable's directory first: the timing child needs a python3 that imports llm_router.
    path = f"{bindir}:{Path(sys.executable).parent}:/usr/bin:/bin"
    return {"HOME": str(home), "PATH": path, "NO_COLOR": "1", "LANG": "en_US.UTF-8",
            "LLM_ROUTER_SYNTHETIC": "1", **extra}


def _run(home: Path, bindir: Path, **extra) -> str:
    r = subprocess.run(["/bin/bash", str(SCRIPT)], input=STDIN, env=_env(home, bindir, **extra),
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout


def _rows(home: Path, want: int, timeout: float = 20.0) -> list[dict]:
    store = home / ".llm-router" / hl.STORE_FILENAME
    end = time.monotonic() + timeout
    rows: list[dict] = []
    while time.monotonic() < end:
        if store.exists():
            rows = [json.loads(x) for x in store.read_text().splitlines()]
            if len(rows) >= want:
                break
        time.sleep(0.1)
    return rows


def _setup(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "home"
    (home / ".llm-router").mkdir(parents=True)
    (home / ".llm-router" / ".statusline_python").write_text(sys.executable + "\n")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    # A perl that records that it ran: the timed path must not start one.
    (bindir / "perl").write_text(f'#!/bin/sh\necho x >> "{tmp_path}/perl.log"\nexit 1\n')
    (bindir / "perl").chmod(0o755)
    return home, bindir


def _seg(home: Path, age_s: int) -> None:
    (home / ".llm-router" / "statusline_seg_f68.kv").write_text(
        f"v=1\nwritten={int(time.time()) - age_s}\nhealth=ok\nmix_local=3\nmix_paid=2\n")


def test_record_stamps_writes_phase_and_removes_the_stamps(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.delenv("LLM_ROUTER_HOOK_LATENCY", raising=False)  # in-process hook tests leak "off"
    monkeypatch.delenv("LLM_ROUTER_HOOK_LATENCY_MAX_BYTES", raising=False)
    s, e = tmp_path / "s", tmp_path / "e"
    s.write_text("")
    e.write_text("")
    os.utime(s, ns=(1_000_000_000, 1_000_000_000))
    os.utime(e, ns=(1_000_000_000 + 12_500_000, 1_000_000_000 + 12_500_000))
    assert hl._main(["record-stamps", "statusline", "Statusline", str(s), str(e), "sid1", "cache_hit"]) == 0
    row = json.loads((tmp_path / hl.STORE_FILENAME).read_text().splitlines()[-1])
    assert row["elapsed_ms"] == 12.5 and row["phases_ms"] == {"cache_hit": 12.5}
    assert row["session_id"] == "sid1" and row["hook"] == "statusline"
    assert not s.exists() and not e.exists()


def test_record_stamps_with_a_missing_stamp_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    s = tmp_path / "s"
    s.write_text("")
    assert hl._main(["record-stamps", "statusline", "Statusline", str(s), str(tmp_path / "nope")]) == 1
    assert not (tmp_path / hl.STORE_FILENAME).exists()


def test_record_stamps_usage_error_and_record_raw_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.delenv("LLM_ROUTER_HOOK_LATENCY", raising=False)
    monkeypatch.delenv("LLM_ROUTER_HOOK_LATENCY_MAX_BYTES", raising=False)
    assert hl._main(["record-stamps", "statusline"]) == 2
    assert hl._main(["record-raw", "statusline", "Statusline", "7.5"]) == 0
    row = json.loads((tmp_path / hl.STORE_FILENAME).read_text().splitlines()[-1])
    assert row["elapsed_ms"] == 7.5 and "phases_ms" not in row


def test_timed_render_names_its_path_starts_no_perl_and_leaks_no_stamp(tmp_path):
    home, bindir = _setup(tmp_path)
    # miss: no cache yet
    _run(home, bindir, LLM_ROUTER_STATUSLINE_TIMING="all")
    # hit: fresh cache; stale: older than the 30 s TTL
    _seg(home, 1)
    _run(home, bindir, LLM_ROUTER_STATUSLINE_TIMING="all")
    _seg(home, 60)
    _run(home, bindir, LLM_ROUTER_STATUSLINE_TIMING="all")
    rows = _rows(home, 3)
    phases = sorted(next(iter(r["phases_ms"])) for r in rows)
    assert phases == ["cache_hit", "cache_miss", "cache_stale"], rows
    assert all(r["hook"] == "statusline" and r["session_id"] == "f68" and r["elapsed_ms"] >= 0 for r in rows)
    assert not (tmp_path / "perl.log").exists(), "the timed path started perl"
    time.sleep(0.5)
    assert not list((home / ".llm-router").glob(".slt_*")), "stamp files leaked"


def test_timing_does_not_change_the_printed_line(tmp_path):
    home, bindir = _setup(tmp_path)
    _seg(home, 1)
    plain = _run(home, bindir)
    timed = _run(home, bindir, LLM_ROUTER_STATUSLINE_TIMING="all")
    assert plain == timed and plain.strip()


def test_unwritable_state_dir_renders_without_a_row(tmp_path):
    home, bindir = _setup(tmp_path)
    (home / ".llm-router").chmod(0o500)
    try:
        out = _run(home, bindir, LLM_ROUTER_STATUSLINE_TIMING="all")
    finally:
        (home / ".llm-router").chmod(0o700)
    assert out.strip()


def test_record_stamps_with_the_end_before_the_start_writes_nothing(tmp_path, monkeypatch):
    # A clock step back is an unknown duration, never a 0 ms render.
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.delenv("LLM_ROUTER_HOOK_LATENCY", raising=False)
    s, e = tmp_path / "s", tmp_path / "e"
    s.write_text("")
    e.write_text("")
    os.utime(s, ns=(2_000_000_000, 2_000_000_000))
    os.utime(e, ns=(1_000_000_000, 1_000_000_000))
    assert hl._main(["record-stamps", "statusline", "Statusline", str(s), str(e)]) == 1
    assert not (tmp_path / hl.STORE_FILENAME).exists()
