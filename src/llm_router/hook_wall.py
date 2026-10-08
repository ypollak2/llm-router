"""External wall-clock latency of the five sync hooks with no latency MUST (PLAN
v16 P0.9-g, AMEND R8 A.3), with the machine load on every row.

``hook_latency.elapsed_ms`` starts at the hook's first statement, so it leaves out
interpreter start-up; the host waits for the whole process. This module runs each
hook the way the host does -- a fresh ``python <hook>.py`` with the payload on
stdin -- and times ``Popen`` to exit from outside.

THE FIVE HOOKS (hooks.json): ``enforce-route`` (PreToolUse), ``bash-compress`` and
``playwright-compress`` (PostToolUse), ``cc-usage-track`` (PostToolUse, matcher
``Agent``: rare) and ``subagent-start`` (SubagentStart). Bar: ``HOOK_BUDGETS_MS``,
300 ms p95 for each.

ONE ROW PER RUN, appended to ``<state>/hook_wall.jsonl``::

    {"ts":..., "run_id":"...", "hook":"enforce-route", "mode":"cold", "seq":17,
     "wall_ms":88.4, "in_process_ms":61.2, "rc":0,
     "load1_before":1.92, "load1_after":1.95, "fixture":"enforce-route.json", ...}

``load1_*`` is ``os.getloadavg()[0]`` read just before and just after the run,
outside the timed span. ``in_process_ms`` is the ``elapsed_ms`` row the hook wrote
into the run's own throwaway ``LLM_ROUTER_HOME``; ``wall_ms - in_process_ms`` is
interpreter start-up and teardown.

MODES. ``cold``: each run follows an idle gap (default 0.5 s), as a hook fires
after model think time. ``warm``: runs back to back. Both start from installed
bytecode (three discarded priming runs per hook): a run without ``.pyc`` files
happens once per upgrade, not per tool call. Hooks are interleaved in a shuffled
order each round, so a change in machine load hits all five alike. The verdict is
on cold.

LOAD. A row whose ``max(load1_before, load1_after)`` is above ``MAX_LOAD`` is
excluded from the verdict and counted. ``MAX_LOAD`` = 4.0 is PLAN v16 L13's
"clear machine" bar (``load < 4``); §1.4 rule 5, which AMEND R8 cites, names no
load number.

CHILD ENVIRONMENT. A throwaway ``HOME`` and ``LLM_ROUTER_HOME`` (nothing under
``~/.claude`` or ``~/.llm-router`` is read or written by a hook), ``PATH`` =
``/usr/bin:/bin``, ``LLM_ROUTER_SYNTHETIC=1``, and ``OLLAMA_HOST`` pointed at a
closed port: playwright-compress would otherwise make a local-model call, which is
model time and is forbidden outside the 00:00-07:00 bench window (PLAN v16 L13).

THE LIVE CLAUSE reads ``hook_latency.jsonl`` (``elapsed_ms``) over a window.
``hook_latency`` writes ``load1`` on each hook row; a row above ``MAX_LOAD`` is
excluded and counted, a row without ``load1`` (written before that field) is kept
and counted. Minimum n: 200 for the three per-tool hooks, 30 for the two
low-volume ones; for those two, n < 30 over a window of >= 14 days makes the live
clause "not informative" and the wall clause decides alone (R8).

    python scripts/hook_wall.py run --runs 200            # all five, cold + warm
    python scripts/hook_wall.py run --hook enforce-route --fixture f.json --runs 200 --cold
    python scripts/hook_wall.py report --since 2026-10-09 --until 2026-10-23
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from llm_router import capped_log
from llm_router.paths import state_path

SYNC_HOOKS: tuple[str, ...] = (
    "enforce-route", "bash-compress", "playwright-compress", "cc-usage-track", "subagent-start",
)
#: Agent-only PostToolUse and SubagentStart: a few calls a day.
LOW_VOLUME = frozenset({"cc-usage-track", "subagent-start"})
WALL_MIN_N = 200
LIVE_MIN_N: dict[str, int] = {h: (30 if h in LOW_VOLUME else 200) for h in SYNC_HOOKS}
NOT_INFORMATIVE_AFTER_DAYS = 14
MAX_LOAD = 4.0
STORE_FILENAME = "hook_wall.jsonl"
DEFAULT_GAP_S = 0.5
PRIMING_RUNS = 3
_MAX_BYTES = 4 * 1024 * 1024
_CLOSED_PORT_URL = "http://127.0.0.1:9"
_RUN_TIMEOUT_S = 120.0

PASS, FAIL, INSUFFICIENT, NOT_INFORMATIVE = "PASS", "FAIL", "INSUFFICIENT", "NOT_INFORMATIVE"

# Indirections so tests drive a fake clock and load without touching the process's.
_clock = time.perf_counter
_sleep = time.sleep


def _loadavg() -> float:
    return os.getloadavg()[0]


def store_path() -> Path:
    return state_path(STORE_FILENAME)


def budget_ms(hook: str) -> int:
    from llm_router import hook_latency

    return hook_latency.budget_ms(hook)


def default_hooks_dir() -> Path:
    return Path(__file__).resolve().parent / "hooks"


def default_fixture_dir() -> Path:
    """``tests/fixtures/hook_payloads`` of the checkout this module runs from."""
    return Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "hook_payloads"


def _percentile(sorted_values: list[float], q: float) -> float:
    """Nearest rank on the n-1 scale (the rule ``llm-router kpi`` uses)."""
    k = max(0, min(len(sorted_values) - 1, round(q * (len(sorted_values) - 1))))
    return sorted_values[k]


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
        return None
    return float(v)


# ── running ──────────────────────────────────────────────────────────────────


def child_env(root: Path) -> dict[str, str]:
    """The environment every timed hook gets: nothing of the operator's."""
    import llm_router

    home = root / "home"
    (home / "state").mkdir(parents=True, exist_ok=True)
    return {
        "HOME": str(home), "LLM_ROUTER_HOME": str(home / "state"), "PATH": "/usr/bin:/bin",
        "LANG": "en_US.UTF-8", "LLM_ROUTER_SYNTHETIC": "1", "OLLAMA_HOST": _CLOSED_PORT_URL,
        # The children import the same llm_router as this process.
        "PYTHONPATH": str(Path(llm_router.__file__).resolve().parent.parent),
    }


def time_one(cmd: list[str], payload: bytes, env: dict[str, str]) -> tuple[float, int | None]:
    """Wall milliseconds from ``Popen`` to exit, stdin written and output drained as
    the host does; and the exit code (None when killed at the timeout)."""
    t0 = _clock()
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env)
    try:
        proc.communicate(payload, timeout=_RUN_TIMEOUT_S)
        rc: int | None = proc.returncode
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        rc = None
    return (_clock() - t0) * 1000.0, rc


def _new_elapsed(log: Path, offset: int, hook: str) -> float | None:
    """The ``elapsed_ms`` the hook just wrote to its own ledger, if it wrote one."""
    try:
        with log.open("rb") as fh:
            fh.seek(offset)
            tail = fh.read().decode("utf-8", "replace")
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("hook") == hook:
            return _num(row.get("elapsed_ms"))
    return None


def measure(hooks: list[str], fixtures: dict[str, Path], *, runs: int, modes: list[str],
            gap_s: float = DEFAULT_GAP_S, hooks_dir: Path | None = None,
            python: str = sys.executable, out: Path | None = None,
            seed: int | None = None) -> list[dict]:
    """Run ``runs`` timed invocations of each hook per mode; append and return the rows."""
    if not hasattr(os, "getloadavg"):
        raise RuntimeError("hook_wall needs os.getloadavg (macOS / Linux): every row records the load")
    hooks_dir = hooks_dir or default_hooks_dir()
    out = out or store_path()
    rng = random.Random(seed)
    run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:6]
    payloads = {h: fixtures[h].read_bytes() for h in hooks}
    cmds = {h: [python, str(hooks_dir / f"{h}.py")] for h in hooks}
    root = Path(tempfile.mkdtemp(prefix="hook-wall-"))
    rows: list[dict] = []
    try:
        env = child_env(root)
        ledger = Path(env["LLM_ROUTER_HOME"]) / "hook_latency.jsonl"
        for h in hooks:  # bytecode and page caches as installed; discarded
            for _ in range(PRIMING_RUNS):
                time_one(cmds[h], payloads[h], env)
        for mode in modes:
            for seq in range(runs):
                order = list(hooks)
                rng.shuffle(order)
                for h in order:
                    if mode == "cold":
                        _sleep(gap_s)
                    offset = ledger.stat().st_size if ledger.exists() else 0
                    before = _loadavg()
                    wall, rc = time_one(cmds[h], payloads[h], env)
                    after = _loadavg()
                    row = {
                        "ts": round(time.time(), 3), "run_id": run_id, "hook": h, "mode": mode,
                        "seq": seq, "wall_ms": round(wall, 2),
                        "in_process_ms": _new_elapsed(ledger, offset, h), "rc": rc,
                        "load1_before": round(before, 2), "load1_after": round(after, 2),
                        "fixture": fixtures[h].name, "gap_s": gap_s if mode == "cold" else 0.0,
                        "python": platform.python_version(), "cpus": os.cpu_count(),
                    }
                    capped_log.append(out, (json.dumps(row, separators=(",", ":")) + "\n").encode(),
                                      _MAX_BYTES)
                    rows.append(row)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return rows


# ── judging ──────────────────────────────────────────────────────────────────


def read_rows(path: Path | None = None) -> list[dict]:
    """Wall rows with a numeric ``wall_ms``, oldest first. Never raises."""
    try:
        rows = [r for r in capped_log.read_dicts(path or store_path())
                if _num(r.get("wall_ms")) is not None and _num(r.get("ts")) is not None]
        rows.sort(key=lambda r: r["ts"])
        return rows
    except Exception:  # noqa: BLE001 -- a reader never raises into the scorecard
        return []


def _row_load(row: dict) -> float | None:
    vals = [v for v in (_num(row.get("load1_before")), _num(row.get("load1_after"))) if v is not None]
    return max(vals) if vals else None


def _contended(values: list[float], loads: list[float]) -> dict[str, Any]:
    """The rows above the load bar, reported separately and never scored (R8)."""
    v = sorted(values)
    return {"n": len(v), "p50_ms": round(_percentile(v, 0.50), 1), "p95_ms": round(_percentile(v, 0.95), 1),
            "load1_median": round(statistics.median(loads), 2)}


def wall_summary(rows: list[dict], hook: str, mode: str, max_load: float = MAX_LOAD) -> dict:
    """p50 / p95 / max wall time of one hook in one mode, rows above ``max_load``
    (or with no load recorded) excluded and counted; the ones above the bar are
    summarised separately under ``above_load``."""
    rs = [r for r in rows if r.get("hook") == hook and r.get("mode") == mode]
    ok = [r for r in rs if (_row_load(r) is not None and _row_load(r) <= max_load)]
    out: dict[str, Any] = {"n": len(ok), "excluded_load": len(rs) - len(ok), "max_load": max_load}
    hi = [r for r in rs if (_row_load(r) is not None and _row_load(r) > max_load)]
    if hi:
        out["above_load"] = _contended([float(r["wall_ms"]) for r in hi], [_row_load(r) for r in hi])
    if not ok:
        return out
    walls = sorted(float(r["wall_ms"]) for r in ok)
    loads = sorted(_row_load(r) for r in ok)
    inproc = sorted(v for r in ok if (v := _num(r.get("in_process_ms"))) is not None)
    gaps = [float(r["wall_ms"]) - v for r in ok if (v := _num(r.get("in_process_ms"))) is not None]
    out.update(p50_ms=round(_percentile(walls, 0.50), 1), p95_ms=round(_percentile(walls, 0.95), 1),
               max_ms=round(walls[-1], 1), load1_median=round(statistics.median(loads), 2),
               load1_max=round(loads[-1], 2), rc_nonzero=sum(1 for r in ok if r.get("rc") != 0))
    if inproc:
        out["in_process_p95_ms"] = round(_percentile(inproc, 0.95), 1)
        out["startup_gap_median_ms"] = round(statistics.median(gaps), 1)
    return out


def judge_wall(rows: list[dict], max_load: float = MAX_LOAD) -> dict[str, dict]:
    """Per hook, from that hook's most recent run: cold (the verdict) and warm."""
    out: dict[str, dict] = {}
    for h in SYNC_HOOKS:
        mine = [r for r in rows if r.get("hook") == h]
        if not mine:
            out[h] = {"verdict": INSUFFICIENT, "run_id": None, "cold": {"n": 0, "excluded_load": 0},
                      "warm": {"n": 0, "excluded_load": 0}}
            continue
        run_id = mine[-1].get("run_id")
        latest = [r for r in mine if r.get("run_id") == run_id]
        cold = wall_summary(latest, h, "cold", max_load)
        warm = wall_summary(latest, h, "warm", max_load)
        if cold["n"] < WALL_MIN_N:
            verdict = INSUFFICIENT
        else:
            verdict = PASS if cold["p95_ms"] <= budget_ms(h) else FAIL
        out[h] = {"verdict": verdict, "run_id": run_id, "run_ts": latest[-1]["ts"], "cold": cold, "warm": warm}
    return out


def judge_live(live_rows: list[dict], days: float, max_load: float = MAX_LOAD) -> dict[str, dict]:
    """Per hook, ``elapsed_ms`` p95 from ``hook_latency`` rows in the window."""
    out: dict[str, dict] = {}
    for h in SYNC_HOOKS:
        rs = [r for r in live_rows if r.get("hook") == h and _num(r.get("elapsed_ms")) is not None]
        loads = [_num(r.get("load1")) for r in rs]
        kept = [r for r, v in zip(rs, loads) if v is None or v <= max_load]
        unknown = sum(1 for v in loads if v is None)
        need = LIVE_MIN_N[h]
        entry: dict[str, Any] = {"n": len(kept), "need": need, "excluded_load": len(rs) - len(kept),
                                 "load_not_recorded": unknown}
        hi = [(float(r["elapsed_ms"]), v) for r, v in zip(rs, loads) if v is not None and v > max_load]
        if hi:
            entry["above_load"] = _contended([a for a, _ in hi], [b for _, b in hi])
        if len(kept) >= need:
            vals = sorted(float(r["elapsed_ms"]) for r in kept)
            p95 = _percentile(vals, 0.95)
            entry.update(p95_ms=round(p95, 1), verdict=PASS if p95 <= budget_ms(h) else FAIL)
        elif h in LOW_VOLUME and days >= NOT_INFORMATIVE_AFTER_DAYS:
            entry["verdict"] = NOT_INFORMATIVE
        else:
            entry["verdict"] = INSUFFICIENT
        out[h] = entry
    return out


def gate(wall_rows: list[dict], live_rows: list[dict], days: float,
         max_load: float = MAX_LOAD) -> dict:
    """P0.9-g per hook and overall: wall (cold) AND live, each with its n."""
    wall = judge_wall(wall_rows, max_load)
    live = judge_live(live_rows, days, max_load)
    hooks: dict[str, dict] = {}
    for h in SYNC_HOOKS:
        w, lv = wall[h]["verdict"], live[h]["verdict"]
        if FAIL in (w, lv):
            v = FAIL
        elif w == PASS and lv in (PASS, NOT_INFORMATIVE):
            v = PASS
        else:
            v = INSUFFICIENT
        hooks[h] = {"verdict": v, "budget_ms": budget_ms(h), "wall": wall[h], "live": live[h]}
    verdicts = {e["verdict"] for e in hooks.values()}
    overall = FAIL if FAIL in verdicts else (PASS if verdicts == {PASS} else INSUFFICIENT)
    return {"verdict": overall, "max_load": max_load, "window_days": round(days, 3), "hooks": hooks}


def _ms(v: Any) -> str:
    return "-" if v is None else f"{v:.0f}ms"


def _excluded(e: dict, what: str) -> str:
    a = e.get("above_load")
    seen = (f": p50={_ms(a['p50_ms'])} p95={_ms(a['p95_ms'])} at load1 med={a['load1_median']}, not scored"
            if a else "")
    return f"{e['excluded_load']} {what}{seen}"


def render_lines(g: dict) -> list[str]:
    """The kpi / report lines: one head line, one per hook, one per-tool-call sum."""
    lines = [f"P0.9-g sync hooks p95 <= 300ms (wall cold n>={WALL_MIN_N} at load1<={g['max_load']:g}; "
             f"live elapsed n>=200, 30* for cc-usage-track/subagent-start): {g['verdict']}"]
    for h, e in g["hooks"].items():
        w, lv = e["wall"], e["live"]
        c, wm = w["cold"], w["warm"]
        wall = (f"wall cold p50={_ms(c.get('p50_ms'))} p95={_ms(c.get('p95_ms'))} max={_ms(c.get('max_ms'))} "
                f"n={c['n']}/{WALL_MIN_N} load1 med={c.get('load1_median', '-')} max={c.get('load1_max', '-')} "
                f"({_excluded(c, 'above load excluded')}) {w['verdict']}; warm p95={_ms(wm.get('p95_ms'))} "
                f"n={wm['n']}")
        if c.get("startup_gap_median_ms") is not None:
            wall += f"; in-process p95={_ms(c.get('in_process_p95_ms'))}, start-up gap ~{_ms(c['startup_gap_median_ms'])}"
        star = "*" if h in LOW_VOLUME else ""
        live = (f"live elapsed p95={_ms(lv.get('p95_ms'))} n={lv['n']}/{lv['need']}{star} "
                f"({_excluded(lv, 'above load excluded')}, {lv['load_not_recorded']} load not recorded) "
                f"{lv['verdict']}")
        lines.append(f"{h}: {e['verdict']} | {wall} | {live}")
    er = g["hooks"]["enforce-route"]["wall"]["cold"].get("p95_ms")
    for post in ("bash-compress", "playwright-compress"):
        p = g["hooks"][post]["wall"]["cold"].get("p95_ms")
        if er is not None and p is not None:
            lines.append(f"per tool call (report only): enforce-route + {post} wall cold p95 sum = {er + p:.0f}ms")
    return lines


# ── command line ─────────────────────────────────────────────────────────────


def _parse_when(raw: str) -> float:
    from llm_router.commands import kpi

    ts = kpi._parse_when(raw)
    if ts is None:
        raise SystemExit(f"cannot read {raw!r} as a date, an ISO time or epoch seconds")
    return ts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="hook_wall", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="time the hooks and append rows")
    r.add_argument("--hook", action="append", choices=SYNC_HOOKS, help="repeatable; default all five")
    r.add_argument("--fixture", type=Path, help="payload for the one --hook given")
    r.add_argument("--fixture-dir", type=Path, default=None, help="<hook>.json per hook")
    r.add_argument("--runs", type=int, default=WALL_MIN_N)
    grp = r.add_mutually_exclusive_group()
    grp.add_argument("--cold", action="store_true", help="cold only")
    grp.add_argument("--warm", action="store_true", help="warm only")
    r.add_argument("--gap", type=float, default=DEFAULT_GAP_S, help="idle seconds before a cold run")
    r.add_argument("--hooks-dir", type=Path, default=None)
    r.add_argument("--python", default=sys.executable)
    r.add_argument("--out", type=Path, default=None)
    r.add_argument("--seed", type=int, default=None)
    p = sub.add_parser("report", help="P0.9-g lines from the wall rows and the live ledger")
    p.add_argument("--store", type=Path, default=None)
    p.add_argument("--days", type=float, default=7.0)
    p.add_argument("--since", default=None)
    p.add_argument("--until", default=None)
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    if a.cmd == "run":
        hooks = a.hook or list(SYNC_HOOKS)
        if a.fixture is not None:
            if len(hooks) != 1:
                ap.error("--fixture needs exactly one --hook")
            fixtures = {hooks[0]: a.fixture}
        else:
            d = a.fixture_dir or default_fixture_dir()
            fixtures = {h: d / f"{h}.json" for h in hooks}
        missing = [str(p) for p in fixtures.values() if not p.is_file()]
        if missing:
            ap.error(f"fixture not found: {', '.join(missing)}")
        if a.runs < 1:
            ap.error("--runs must be >= 1")
        modes = ["cold"] if a.cold else ["warm"] if a.warm else ["cold", "warm"]
        out = a.out or store_path()
        rows = measure(hooks, fixtures, runs=a.runs, modes=modes, gap_s=a.gap,
                       hooks_dir=a.hooks_dir, python=a.python, out=out, seed=a.seed)
        print(f"wrote {len(rows)} rows to {out} (run {rows[0]['run_id'] if rows else '-'})")
        for h in hooks:
            for mode in modes:
                s = wall_summary(rows, h, mode)
                print(f"{h:20s} {mode:4s} p50={_ms(s.get('p50_ms'))} p95={_ms(s.get('p95_ms'))} "
                      f"max={_ms(s.get('max_ms'))} n={s['n']} excluded_load={s['excluded_load']} "
                      f"load1 med={s.get('load1_median', '-')} max={s.get('load1_max', '-')} "
                      f"start-up gap ~{_ms(s.get('startup_gap_median_ms'))}"
                      + (f" | above load (not scored): n={s['above_load']['n']} "
                         f"p50={_ms(s['above_load']['p50_ms'])} p95={_ms(s['above_load']['p95_ms'])} "
                         f"load1 med={s['above_load']['load1_median']}" if s.get("above_load") else ""))
        return 0

    from llm_router import hook_latency

    if (a.since is None) != (a.until is None):
        ap.error("--since and --until go together")
    if a.since is not None:
        since, until = _parse_when(a.since), _parse_when(a.until)
        if not since < until:
            ap.error("--since must be before --until")
    else:
        until = time.time()
        since = until - a.days * 86400.0
    g = gate(read_rows(a.store), hook_latency.read_rows(since=since, until=until), (until - since) / 86400.0)
    print(json.dumps(g, indent=2) if a.json else "\n".join(render_lines(g)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
