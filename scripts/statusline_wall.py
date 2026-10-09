#!/usr/bin/env python3
"""External wall-clock latency of the full status line (PLAN v16 P0.9-c), with the
machine load on every row.

The status line is a shell script Claude Code starts about once a second; the user
waits for the whole process. ``hook_latency``'s own ``statusline`` row starts at the
script's first statement and ends in an EXIT trap, so it leaves out bash start-up.
This harness times ``Popen`` to exit from outside, as ``scripts/hook_wall.py`` does
for the python hooks (P0.9-g): stdin is the session JSON, stdout is drained.

STATE. ``--home`` is a prepared ``$HOME`` (it holds ``.llm-router/`` with the
``usage.db`` / ``usage.json`` / ``last_route_*`` / ``proxy_default.json`` to measure
against, and optionally ``.claude/hooks/``). It is COPIED to a throwaway directory
once per batch, so a run never writes into it, and ``LLM_ROUTER_HOME`` /
``HOME`` of the child point at the copy. The session JSON's ``transcript_path`` is
whatever the ``--input`` file says: use a real transcript copy, since its size is
what the old script's context scan paid for.

MODES (one batch each, ``--runs`` timed runs per mode):

* ``cold``   -- each run follows an idle gap (``--gap``, default 0.5 s), as a redraw
  follows model think time. Verdict mode, as in ``hook_wall.py``.
* ``warm``   -- back to back.
* ``first``  -- the segment cache and the remembered interpreter are deleted before
  every run: the first render of a session, which pays the synchronous refresh.
  REPORTED, NOT THE VERDICT (it happens once per session, not once per redraw).

Each run is preceded by three discarded priming runs per batch (bytecode, page cache).

LOAD. ``os.getloadavg()[0]`` before and after each run, outside the timed span. A row
whose max is above ``--max-load`` (default 4.0, PLAN v16 L13's "clear machine" bar)
is excluded from the verdict and counted; its own p50/p95 are printed separately.

    python scripts/statusline_wall.py --home /path/to/bench_home \\
        --input input.json --runs 200 --out rows.jsonl
    python scripts/statusline_wall.py ... --script old_statusline.sh   # a baseline

Exit code 0 when the cold verdict is PASS (p95 <= --bar, n >= --min-n), 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BAR_MS = 100.0
MIN_N = 200
MAX_LOAD = 4.0
PRIMING_RUNS = 3
RUN_TIMEOUT_S = 120.0
CACHE_GLOBS = ("statusline_seg_*.kv", ".statusline_python", ".statusline_seg_spawn_*", "statusline_cache.json")

_clock = time.perf_counter


def percentile(values: list[float], q: float) -> float:
    """Nearest rank on the n-1 scale (the rule ``llm-router kpi`` and hook_wall use)."""
    v = sorted(values)
    return v[max(0, min(len(v) - 1, round(q * (len(v) - 1))))]


def time_one(cmd: list[str], payload: bytes, env: dict[str, str]) -> tuple[float, int | None]:
    t0 = _clock()
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env)
    try:
        proc.communicate(payload, timeout=RUN_TIMEOUT_S)
        rc: int | None = proc.returncode
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        rc = None
    return (_clock() - t0) * 1000.0, rc


def _drop_cache(state: Path) -> None:
    for pattern in CACHE_GLOBS:
        for p in state.glob(pattern):
            p.unlink(missing_ok=True)


def _in_process_ms(ledger: Path, offset: int) -> float | None:
    """The ``statusline`` row the script just appended (written by a detached child, so
    it may land a moment after the script exits: None when it is not there yet)."""
    try:
        with ledger.open("rb") as fh:
            fh.seek(offset)
            tail = fh.read().decode("utf-8", "replace")
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("hook") == "statusline":
            v = row.get("elapsed_ms")
            return float(v) if isinstance(v, (int, float)) else None
    return None


def _wait_for_load(max_load: float, wait_s: float) -> None:
    """Sleep (never spin) until load1 <= max_load, for at most wait_s. This harness
    generates no load of its own; it only declines to time into someone else's."""
    deadline = time.monotonic() + wait_s
    while os.getloadavg()[0] > max_load and time.monotonic() < deadline:
        time.sleep(2.0)


def measure(script: Path, home: Path, payload: bytes, *, runs: int, modes: list[str], gap_s: float,
            path_env: str, out: Path | None, max_load: float = MAX_LOAD, wait_s: float = 0.0) -> list[dict]:
    rows: list[dict] = []
    run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    for mode in modes:
        root = Path(tempfile.mkdtemp(prefix="statusline-wall-"))
        try:
            shutil.copytree(home, root / "home", symlinks=True)
            state = root / "home" / ".llm-router"
            env = {"HOME": str(root / "home"), "LLM_ROUTER_HOME": str(state), "PATH": path_env,
                   "LANG": "en_US.UTF-8", "LLM_ROUTER_STATUSLINE_TIMING": "all",
                   "LLM_ROUTER_SYNTHETIC": "1"}
            cmd = ["/bin/bash", str(script)]
            ledger = state / "hook_latency.jsonl"
            for _ in range(PRIMING_RUNS):
                time_one(cmd, payload, env)
            for seq in range(runs):
                if mode == "first":
                    _drop_cache(state)
                elif mode == "cold":
                    time.sleep(gap_s)
                if wait_s > 0:
                    _wait_for_load(max_load, wait_s)
                offset = ledger.stat().st_size if ledger.exists() else 0
                before = os.getloadavg()[0]
                wall, rc = time_one(cmd, payload, env)
                after = os.getloadavg()[0]
                if mode == "first":
                    time.sleep(0.05)  # let the detached ledger child land its row
                row = {"ts": round(time.time(), 3), "run_id": run_id, "mode": mode, "seq": seq,
                       "script": script.name, "wall_ms": round(wall, 2),
                       "in_script_ms": _in_process_ms(ledger, offset), "rc": rc,
                       "load1_before": round(before, 2), "load1_after": round(after, 2)}
                rows.append(row)
                if out:
                    with out.open("a") as fh:
                        fh.write(json.dumps(row, separators=(",", ":")) + "\n")
        finally:
            shutil.rmtree(root, ignore_errors=True)
    return rows


def summarise(rows: list[dict], mode: str, max_load: float) -> dict:
    rs = [r for r in rows if r["mode"] == mode]
    ok = [r for r in rs if max(r["load1_before"], r["load1_after"]) <= max_load]
    hi = [r for r in rs if max(r["load1_before"], r["load1_after"]) > max_load]
    out: dict = {"mode": mode, "n": len(ok), "excluded_load": len(hi), "n_all": len(rs)}
    for tag, group in (("", ok), ("above_load_", hi)):
        if not group:
            continue
        w = [r["wall_ms"] for r in group]
        loads = [max(r["load1_before"], r["load1_after"]) for r in group]
        out[tag + "p50_ms"] = round(percentile(w, 0.50), 1)
        out[tag + "p95_ms"] = round(percentile(w, 0.95), 1)
        out[tag + "max_ms"] = round(max(w), 1)
        out[tag + "load1_median"] = round(statistics.median(loads), 2)
        out[tag + "load1_max"] = round(max(loads), 2)
    allw = [r["wall_ms"] for r in rs]
    if allw:
        out["all_rows_p95_ms"] = round(percentile(allw, 0.95), 1)
    ins = [r["in_script_ms"] for r in ok if r["in_script_ms"] is not None]
    if ins:
        out["in_script_p95_ms"] = round(percentile(ins, 0.95), 1)
        out["in_script_n"] = len(ins)
    out["rc_nonzero"] = sum(1 for r in rs if r["rc"] != 0)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--script", type=Path, default=Path(__file__).resolve().parents[1]
                    / "src" / "llm_router" / "hooks" / "statusline-command.sh")
    ap.add_argument("--home", type=Path, required=True, help="prepared $HOME to copy per batch")
    ap.add_argument("--input", type=Path, required=True, help="session JSON piped to stdin")
    ap.add_argument("--runs", type=int, default=200)
    ap.add_argument("--modes", default="cold,warm,first")
    ap.add_argument("--gap", type=float, default=0.5)
    ap.add_argument("--path", default="/usr/bin:/bin", help="PATH of the child (put the venv bin first)")
    ap.add_argument("--max-load", type=float, default=MAX_LOAD)
    ap.add_argument("--bar", type=float, default=BAR_MS)
    ap.add_argument("--min-n", type=int, default=MIN_N)
    ap.add_argument("--wait-load", type=float, default=0.0, metavar="SECONDS",
                    help="before each run sleep until load1 <= --max-load, at most this long")
    ap.add_argument("--out", type=Path)
    ns = ap.parse_args(argv)
    modes = [m for m in ns.modes.split(",") if m]
    rows = measure(ns.script, ns.home, ns.input.read_bytes(), runs=ns.runs, modes=modes,
                   gap_s=ns.gap, path_env=ns.path, out=ns.out,
                   max_load=ns.max_load, wait_s=ns.wait_load)
    verdict = "INSUFFICIENT"
    for mode in modes:
        s = summarise(rows, mode, ns.max_load)
        print(json.dumps({"script": ns.script.name, **s}, sort_keys=True))
        if mode == "cold" and s["n"] >= ns.min_n:
            verdict = "PASS" if s["p95_ms"] <= ns.bar else "FAIL"
    print(f"verdict (cold, p95 <= {ns.bar:g} ms, n >= {ns.min_n}, load1 <= {ns.max_load:g}): {verdict}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
