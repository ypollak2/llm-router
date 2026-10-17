"""P3.3: one offload row per closed UTC day, and the 7-day streak that PLAN v16 section 7.1 c3 needs (D-210).

``compute_day`` measures one UTC day [00:00, 24:00) with ``offload_share.action_units`` / ``build_units`` /
``turn_units`` (reused unchanged) and splits it into populations that are reported separately:

* ``organic``: session kind organic. Always reported on its own, never merged silently into another number.
* ``synthetic (D-167)``: kind research AND the session id is in the manifest given with
  ``--synthetic-manifest`` (the synth-live driver's ``manifest.jsonl``; ids are read, nothing else).
* ``other``: every other research session, harness, headless. Counts only, never a rate. Never counts.
* ``release_clock`` (D-210): organic UNION synthetic, with its own n, floors, value and CI. The streak reads
  this block. Without ``--synthetic-manifest`` it is organic only (the row's ``note`` says so).

A line below ``MIN_ACTIONS_DAY`` actions / ``MIN_TURNS_DAY`` turns is ``{measured: false, reason:
"unmeasured", n}`` with no value and no interval (D-193; the floors of 30 actions / 10 turns and the 7-day streak
are owner-ratified, D-210, 2026-10-17). ``cluster95`` is a session-clustered bootstrap
(:func:`boot_rate`), seeded from the date so a rerun matches.

Rows live at ``paths.state_path("daily")/<date>.json``. Writes are atomic and idempotent: a closed day with a
row is left alone; ``--force`` writes ``<date>.revN.json`` and never overwrites. An open day is refused unless
``partial`` is asked for, and its row (``<date>.partial.json``) is never read by the streak.

No prompt text is read anywhere: rows hold ids, models, enums and counts. Nothing here installs a schedule.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import random
import re
import sys
import tempfile
import time
from datetime import date as Date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = "daily-offload/1"
MIN_ACTIONS_DAY = 30
MIN_TURNS_DAY = 10
STREAK_REQUIRED = 7                # D-210 (2026-10-17): 7 consecutive measured days, not 14
BOOT_B = 2000
TARGET = 0.60                    # == offload_share.TARGET (asserted by a test)
SYNTH_LABEL = "synthetic (D-167)"
SYNTH_SCHEMA = "synth-live-manifest/1"
T_P2_FILE = "prereg_t_p2.json"
_REV_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.rev(\d+)\.json$")
_MAX_WALK = 400


class DailyError(Exception):
    """A refusal the CLI prints and exits 2 on."""


def _now() -> float:
    return time.time()


# ---------------------------------------------------------------- days and seeds

def day_bounds(day: Date) -> tuple[float, float]:
    """``[start, end)`` epoch seconds of a UTC calendar day (never the local day)."""
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


def utc_date(ts: float) -> Date:
    return datetime.fromtimestamp(ts, timezone.utc).date()


def last_closed_day(now: float) -> Date:
    return utc_date(now) - timedelta(days=1)


def seed_for(day: str, population: str, label: str) -> int:
    """Bootstrap seed from the date (and line), so a rerun reproduces the interval. Never the clock."""
    h = hashlib.sha256(f"daily-offload/{day}/{population}/{label}".encode()).hexdigest()
    return int(h[:8], 16)


def boot_rate(items: Iterable[tuple[Any, int]], B: int = BOOT_B, seed: int = 0) -> list[float] | None:
    """Session-clustered bootstrap 95% interval of a 0/1 rate (sibling of ``shadow_eval.boot_diff``).
    ``items`` are ``(session_id, 0 or 1)``; sessions are resampled with replacement, the rate is
    sum(k) / sum(n) over the draw. None with no items."""
    per: dict[Any, list[int]] = {}
    for sid, v in items:
        c = per.setdefault(sid, [0, 0])
        c[0] += 1 if v else 0
        c[1] += 1
    keys = sorted(per, key=str)
    if not keys:
        return None
    ks = [per[s][0] for s in keys]
    ns = [per[s][1] for s in keys]
    m = len(keys)
    rng = random.Random(seed)
    out = []
    for _ in range(B):
        k = n = 0
        for _ in range(m):
            i = rng.randrange(m)
            k += ks[i]
            n += ns[i]
        out.append(k / n)
    out.sort()
    return [round(out[int(0.025 * B)], 4), round(out[int(0.975 * B) - 1], 4)]


# ---------------------------------------------------------------- manifest

def read_manifest(path: "str | Path") -> frozenset[str]:
    """Session ids of a synth-live ``manifest.jsonl`` (``schema synth-live-manifest/1``): the ``session_id``
    field of each row, dry-run rows skipped. Nothing else is read or kept. A missing file raises."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise DailyError(f"--synthetic-manifest: cannot read {path} ({type(exc).__name__})") from exc
    ids = set()
    for line in text.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict) and not d.get("dry_run") and isinstance(d.get("session_id"), str) \
                and d["session_id"]:
            ids.add(d["session_id"])
    return frozenset(ids)


# ---------------------------------------------------------------- inputs

class Inputs:
    """Everything ``action_units`` needs, injectable. ``kind_of(session_id, stamp)`` -> kind or None."""

    def __init__(self, proxy_rows, mcp_rows, techops, edit_rows, quality_rows, kind_of, *,
                 outcome_redos=(), band_redone=frozenset(), thread_of=None):
        self.proxy_rows, self.mcp_rows, self.techops = proxy_rows, mcp_rows, techops
        self.edit_rows, self.quality_rows, self.kind_of = edit_rows, quality_rows, kind_of
        self.outcome_redos, self.band_redone, self.thread_of = list(outcome_redos), band_redone, thread_of


def load_inputs(first_day: Date, now: float) -> Inputs:
    """The live ledgers, read the way ``kpi`` reads them for the offload block."""
    from llm_router import lane_quality, northstar as ns, o3_transcripts, session_kind, technical_ops
    from llm_router.commands import kpi
    from llm_router.proxy import ledger as pl

    days = max(1, int((now - day_bounds(first_day)[0]) // 86400) + 2)
    rows = pl.read_rows()
    index = session_kind.KindIndex(rows)
    _, band, outcomes, edits, _ = kpi._o3_unit_inputs(days, index, now)
    return Inputs(rows, list(ns.local_shadow_units(days=days, all_providers=True)), technical_ops.read_ops(),
                  edits, lane_quality.read_rows(), lambda sid, stamp: index.resolve(sid, stamp=stamp).kind,
                  outcome_redos=outcomes, band_redone=band, thread_of=o3_transcripts.thread_lookup())


# ---------------------------------------------------------------- one day

def _line(rows: list[tuple[Any, bool]], minimum: int, seed: int) -> dict:
    """``{measured: true, k, n, value, wilson95, cluster95, n_sessions, largest_session_share}`` or the
    unmeasured stub. ``rows`` are ``(session_id, hit)``."""
    from llm_router import offload_share as osh
    n = len(rows)
    if n < minimum:
        return {"measured": False, "reason": "unmeasured", "n": n, "min_n": minimum}
    base = osh.rate_line(sum(1 for _, h in rows if h), n, (s for s, _ in rows))
    base.pop("window", None)
    return {"measured": True, **base, "cluster95": boot_rate(((s, 1 if h else 0) for s, h in rows), seed=seed),
            "min_n": minimum}


def _counts(rows: list[tuple[Any, bool]]) -> dict:
    return {"k": sum(1 for _, h in rows if h), "n": len(rows)}


def _breakdown(actions: list[dict]) -> dict:
    def split(keyf) -> dict:
        groups: dict[str, list[dict]] = {}
        for a in actions:
            groups.setdefault(keyf(a), []).append(a)
        out = {}
        for key, rows in sorted(groups.items()):
            k, n = sum(1 for a in rows if a["numerator"]), len(rows)
            out[key] = {"k": k, "n": n, "value": round(k / n, 4) if n >= MIN_ACTIONS_DAY else None}
        return out
    return {"by_task_group": split(lambda a: a["task_group"]),
            "by_served_model": split(lambda a: str(a.get("model") or "none")),
            "by_lane": split(lambda a: "/".join(a["lane"]))}


def _t_p2() -> tuple[float | None, str | None]:
    from llm_router import paths
    p = paths.state_path(T_P2_FILE)
    if not p.is_file():
        return None, f"no {T_P2_FILE} in the state dir: the preregistered T_P2 is not available to this command"
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        v = d.get("T_P2", d.get("t_p2")) if isinstance(d, dict) else d
        v = float(v)
        if not math.isfinite(v):
            raise ValueError
        return v, None
    except (OSError, ValueError, TypeError):
        return None, f"{T_P2_FILE} is unreadable (expected a number or {{\"T_P2\": number}})"


def _population(label: str, day: str, key: str, actions: list[dict], turns: list[dict], sampled: dict,
                excluded: dict, caveats: list[str]) -> dict:
    from llm_router import offload_share as osh
    from llm_router.offload_share import DOOR_PROXY, DOOR_SUBAGENT, OFFLOADED_CLASSES

    def seed(name: str) -> int:
        return seed_for(day, key, name)

    act = [(a["session_id"], a["numerator"]) for a in actions]
    call = [(a["session_id"], a["numerator"]) for a in actions if a["door"] in (DOOR_PROXY, DOOR_SUBAGENT)]
    spread = [(a["session_id"], a["spread"]) for a in actions]
    turn = [(u["session_id"], u["class"] != osh.CLASS_CLAUDE and not u["redone"]) for u in turns]
    action_l = _line(act, MIN_ACTIONS_DAY, seed("action"))
    turn_l = _line(turn, MIN_TURNS_DAY, seed("turn"))
    t_p2, t_note = _t_p2()

    def gap(line: dict, ref: float | None) -> float | None:
        return round(line["value"] - ref, 4) if line.get("measured") and ref is not None else None
    would = sum(1 for a in actions if a["served_class"] in OFFLOADED_CLASSES and not a["redone"]
                and not a["reread"])
    return {
        "label": label, "action": action_l, "turn": turn_l,
        "call": _line(call, MIN_ACTIONS_DAY, seed("call")),
        "spread": _line(spread, MIN_ACTIONS_DAY, seed("spread")),
        "counts": {"action": _counts(act), "turn": _counts(turn), "call": _counts(call),
                   "spread": _counts(spread)},
        "breakdown": _breakdown(actions),
        "gap_to_0.60": {"action": gap(action_l, TARGET), "turn": gap(turn_l, TARGET)},
        "t_p2": t_p2,
        "gap_to_T_P2": {"action": gap(action_l, t_p2), "turn": gap(turn_l, t_p2), "note": t_note},
        "diagnostics": {
            "k_if_all_lanes_sampled": would, "n": len(actions),
            "sampled_lanes": sorted("/".join(lane) for lane in sampled),
            "sampled_lane_count": len(sampled),
            "note": "k_if_all_lanes_sampled ignores the lane-quality rule: offloaded and not redone"},
        "excluded": {"scope": "day_wide_all_populations", **excluded},
        "caveats": caveats,
    }


def compute_day(day: Date, inputs: Inputs, *, now: float | None = None,
                manifest_ids: Iterable[str] = (), code_rev: str | None = None,
                local_by_cause=None) -> dict:
    """The row for one UTC ``day`` (see the module docstring). ``partial`` is True when the day has not
    ended at ``now``; the window then ends at ``now``. ``local_by_cause``: None reads ``usage.db``
    (``ledger_view.local_by_cause``, F23), or pass a dict / ``False`` (skip)."""
    from llm_router import offload_share as osh
    from llm_router import session_kind as sk

    now = _now() if now is None else now
    start, end = day_bounds(day)
    partial = end > now
    until = min(end, now)
    ds = day.isoformat()
    ids = frozenset(manifest_ids)
    kinds: dict[Any, Any] = {}

    def kind_of(sid, stamp):
        k = inputs.kind_of(sid, stamp)
        kinds[sid] = k
        return k

    res = osh.action_units(
        start, until, proxy_rows=inputs.proxy_rows, mcp_rows=inputs.mcp_rows, techops=inputs.techops,
        edit_rows=inputs.edit_rows, kind_of=kind_of, allowed=frozenset(sk.VALID_KINDS),
        thread_of=inputs.thread_of, band_redone=inputs.band_redone, outcome_redos=inputs.outcome_redos,
        quality_rows=inputs.quality_rows)

    def pop_of(sid) -> str:
        k = kinds.get(sid)
        if k == sk.KIND_ORGANIC:
            return "organic"
        return "synthetic" if k == sk.KIND_RESEARCH and sid in ids else "other"

    def in_day(x: dict) -> bool:
        return isinstance(x.get("ts"), (int, float)) and start <= x["ts"] < until

    actions = [a for a in res["actions"] if in_day(a)]
    turns = [u for u in osh.turn_units(res["built"]["units"]) if in_day(u)]
    by_pop: dict[str, tuple[list[dict], list[dict]]] = {p: ([], []) for p in ("organic", "synthetic", "other")}
    for a in actions:
        by_pop[pop_of(a["session_id"])][0].append(a)
    for u in turns:
        by_pop[pop_of(u["session_id"])][1].append(u)

    excluded = {k: v for k, v in res["excluded"].items()}
    base_cav = [
        "the redo detector is not informative (PLAN section 1.1): 'not redone' is true almost always",
        "redo look-ahead is limited to this day's window: an action late in the day may still turn into a redo "
        "(window_open)"]
    if not res["sampled"]:
        base_cav.insert(0, "no quality-sampled lane: the action numerator is 0 by rule (PLAN section 1.1); see "
                           "diagnostics.k_if_all_lanes_sampled")
    rc_note = ("release_clock = organic + synthetic (D-210); synthetic membership from --synthetic-manifest" if ids
               else "no --synthetic-manifest given: release_clock = organic only (D-210)")
    rc = _population("release_clock (organic + synthetic, D-210)", ds, "release_clock",
                     by_pop["organic"][0] + by_pop["synthetic"][0], by_pop["organic"][1] + by_pop["synthetic"][1],
                     res["sampled"], excluded, base_cav + [rc_note])
    rc["note"] = rc_note
    pops = {
        "organic": _population("organic", ds, "organic", *by_pop["organic"], res["sampled"], excluded, base_cav),
        "synthetic": _population(SYNTH_LABEL, ds, "synthetic", *by_pop["synthetic"], res["sampled"], excluded,
                                 base_cav + ["synthetic sessions (D-167): never in the organic numerator or "
                                             "denominator; counted only in release_clock (D-210)"]),
        "release_clock": rc,
    }
    oa, ot = by_pop["other"]
    kind_counts: dict[str, int] = {}
    for a in oa:
        kind_counts[str(kinds.get(a["session_id"]))] = kind_counts.get(str(kinds.get(a["session_id"])), 0) + 1
    pops["other"] = {
        "label": "other (counts only)",
        "action": {"measured": False, "reason": "counts_only", "n": len(oa)},
        "turn": {"measured": False, "reason": "counts_only", "n": len(ot)},
        "call": {"measured": False, "reason": "counts_only",
                 "n": sum(1 for a in oa if a["door"] in (osh.DOOR_PROXY, osh.DOOR_SUBAGENT))},
        "spread": {"measured": False, "reason": "counts_only", "n": sum(1 for a in oa if a["spread"])},
        "breakdown": {"by_kind": dict(sorted(kind_counts.items()))},
        "excluded": {"scope": "day_wide_all_populations", **excluded},
        "caveats": ["research sessions not in the --synthetic-manifest, harness and headless sessions: "
                    "counted, never rated"],
    }
    if local_by_cause is None:
        from llm_router import ledger_view as lv
        # ledger_view's upper bound is inclusive; end-1 keeps the day half-open at 1 s resolution.
        local_by_cause = lv.local_by_cause(lv.Bounds("day", f"UTC day {ds}", start, until - 1))
    return {
        "schema": SCHEMA, "date": ds, "tz": "UTC", "window": {"since": start, "until": until},
        "generated_at": datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "code_rev": code_rev if code_rev is not None else _code_rev(),
        "partial": partial,
        "synthetic_manifest_ids": len(ids),
        "populations": pops,
        "day_wide": {"local_by_cause": local_by_cause or None,
                     "local_by_cause_note": "usage.db routing_decisions served local that day (proxy ledger rows are NOT "
                                            "included); NOT filtered to organic sessions, cross-check only"},
    }


def _code_rev() -> str:
    import subprocess
    try:
        out = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], cwd=Path(__file__).resolve().parent,
                             capture_output=True, text=True, timeout=3)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        from importlib.metadata import version
        return "v" + version("llm-router")
    except Exception:  # noqa: BLE001
        return "unknown"


# ---------------------------------------------------------------- files

def daily_dir() -> Path:
    from llm_router import paths
    return paths.state_path("daily")


def _payload(row: dict) -> str:
    return json.dumps({k: v for k, v in row.items() if k not in ("generated_at", "code_rev")},
                      sort_keys=True)


def _atomic_write(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(row, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _revs(base: Path, ds: str) -> list[tuple[int, Path]]:
    out = []
    if base.is_dir():
        for p in base.iterdir():
            m = _REV_RE.match(p.name)
            if m and m.group(1) == ds:
                out.append((int(m.group(2)), p))
    return sorted(out)


def day_file(ds: str, base: Path | None = None) -> Path | None:
    """The row the streak reads for a date: the highest ``.revN`` if any, else ``<date>.json``."""
    base = daily_dir() if base is None else base
    revs = _revs(base, ds)
    if revs:
        return revs[-1][1]
    p = base / f"{ds}.json"
    return p if p.is_file() else None


def write_day(row: dict, *, force: bool = False, partial: bool = False, base: Path | None = None) -> dict:
    """Write a row. Returns ``{status, path}`` with status ``written``, ``exists`` (closed day already has a
    row: no-op), ``unchanged`` (force, same measurement), ``rev`` (force, new ``.revN``), ``partial`` or
    ``refused_open_day`` (an open day without ``partial``)."""
    base = daily_dir() if base is None else base
    ds = row["date"]
    if row["partial"]:
        if not partial:
            return {"status": "refused_open_day", "path": None}
        p = base / f"{ds}.partial.json"
        _atomic_write(p, row)
        return {"status": "partial", "path": str(p)}
    cur = day_file(ds, base)
    if cur is None:
        p = base / f"{ds}.json"
        _atomic_write(p, row)
        return {"status": "written", "path": str(p)}
    if not force:
        return {"status": "exists", "path": str(cur)}
    try:
        same = _payload(json.loads(cur.read_text(encoding="utf-8"))) == _payload(row)
    except (OSError, ValueError):
        same = False
    if same:
        return {"status": "unchanged", "path": str(cur)}
    revs = _revs(base, ds)
    p = base / f"{ds}.rev{(revs[-1][0] if revs else 0) + 1}.json"
    _atomic_write(p, row)
    return {"status": "rev", "path": str(p)}


# ---------------------------------------------------------------- streak

def _day_state(ds: str, base: Path, pop: str = "release_clock") -> tuple[bool, str | None]:
    """``(measured, reason)`` for a date on population ``pop``; reason is why the file does not count (None
    when it does). A row written before D-210 has no ``release_clock`` block: invalid_schema (rerun --force)."""
    p = day_file(ds, base)
    if p is None:
        return False, "missing"
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        org = d["populations"][pop]
        ok = d.get("schema") == SCHEMA and d.get("date") == ds and isinstance(org, dict)
    except (OSError, ValueError, KeyError, TypeError):
        ok = False
    if not ok:
        return False, "invalid_schema"
    if d.get("partial") is not False:
        return False, "partial"
    a, t = org.get("action") or {}, org.get("turn") or {}
    if a.get("measured") is not True:
        return False, "unmeasured_action"
    if t.get("measured") is not True:
        return False, "unmeasured_turn"
    return True, None


def _written(ds: str, base: Path) -> tuple[bool, str | None]:
    ok, why = _day_state(ds, base)
    return (ok or why in ("unmeasured_action", "unmeasured_turn")), why


def _organic_state(ds: str, base: Path) -> tuple[bool, str | None]:
    return _day_state(ds, base, "organic")


def streak(end: Date | None = None, *, now: float | None = None, base: Path | None = None,
           required: int = STREAK_REQUIRED) -> dict:
    """Walk back from ``end`` (default: the last closed UTC day) over the ``release_clock`` block (organic +
    synthetic, D-210). A day counts when its row exists, the schema is valid, ``partial`` is false and the
    release_clock action AND turn lines are measured. ``organic_streak`` is the same walk over the organic
    block alone, reported beside it and never merged into it.
    ``written_streak`` counts valid closed rows whether or not measured. Pass (section 7.1 c3): measured_streak
    >= ``required``. A date at or after today is never counted (an open day has no closed row)."""
    base = daily_dir() if base is None else base
    now = _now() if now is None else now
    last = last_closed_day(now)
    end = last if end is None else min(end, last)

    def walk(ok_fn) -> tuple[int, dict | None]:
        n, d = 0, end
        while n < _MAX_WALK:
            ok, why = ok_fn(d.isoformat(), base)
            if not ok:
                return n, {"date": d.isoformat(), "reason": why}
            n, d = n + 1, d - timedelta(days=1)
        return n, None

    ms, mbreak = walk(_day_state)
    ws, wbreak = walk(_written)
    os_, obreak = walk(_organic_state)
    return {"end": end.isoformat(), "required": required, "measured_streak": ms, "written_streak": ws,
            "first_break": mbreak, "written_first_break": wbreak, "pass": ms >= required,
            "organic_streak": os_, "organic_first_break": obreak,
            "basis": "release_clock (organic + synthetic, D-210) action AND turn measured, closed (not partial) rows"}


# ---------------------------------------------------------------- scheduler text (print only)

def render_plist(label: str = "com.llm-router.kpi-daily") -> str:
    """A launchd plist as text. Printed, never written or loaded: installing it is an owner action (D-104)."""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!-- PRINT ONLY. Nothing is installed by this command. Installing is an owner action (D-104):
     save as ~/Library/LaunchAgents/{label}.plist and load it with launchd yourself.
     --backfill 3 makes a missed run heal itself; rows are per UTC day. launchd times are LOCAL. -->
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array><string>llm-router</string><string>kpi</string><string>--daily</string>
         <string>--backfill</string><string>3</string></array>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>3</integer><key>Minute</key><integer>15</integer></dict>
  <key>RunAtLoad</key><false/>
</dict>
</plist>
"""


# ---------------------------------------------------------------- CLI

def _parse_day(text: str, flag: str) -> Date:
    try:
        return Date.fromisoformat(text)
    except ValueError as exc:
        raise DailyError(f"{flag}: expected YYYY-MM-DD, got {text!r}") from exc


def run_daily(*, date: str | None, backfill: int, force: bool, partial: bool, manifest: str | None,
              as_json: bool = False, inputs: Inputs | None = None, now: float | None = None) -> int:
    """``kpi --daily``. Exit 0 ok, 2 refused."""
    now = _now() if now is None else now
    try:
        target = _parse_day(date, "--date") if date else last_closed_day(now)
        if backfill < 0:
            raise DailyError("--backfill: must be >= 0")
        ids = read_manifest(manifest) if manifest else frozenset()
        if day_bounds(target)[1] > now and not partial:
            raise DailyError(f"{target} is not a closed UTC day: refused (use --partial for a snapshot that "
                             "never counts toward the streak)")
        if partial and day_bounds(target)[1] <= now:
            raise DailyError(f"--partial: {target} is already closed")
        days = [target - timedelta(days=i) for i in range(backfill - 1, -1, -1)] if backfill else [target]
        if partial:
            days = [target]
    except DailyError as exc:
        print(f"kpi --daily: {exc}", file=sys.stderr)
        return 2
    base = daily_dir()
    todo = [d for d in days if force or partial or day_file(d.isoformat(), base) is None]
    inp = inputs
    if todo and inp is None:
        inp = load_inputs(min(todo), now)
    out = []
    for d in days:
        if d not in todo:
            out.append({"date": d.isoformat(), "status": "exists", "path": str(day_file(d.isoformat(), base))})
            continue
        row = compute_day(d, inp, now=now, manifest_ids=ids)
        res = write_day(row, force=force, partial=partial, base=base)
        org = row["populations"]["organic"]
        rc = row["populations"]["release_clock"]
        out.append({"date": d.isoformat(), **res, "organic_action": org["action"], "organic_turn": org["turn"],
                    "release_clock_action": rc["action"], "release_clock_turn": rc["turn"]})
    if as_json:
        print(json.dumps(out, indent=2, sort_keys=True))
    else:
        for o in out:
            line = f"{o['date']}  {o['status']}  {o['path']}"
            if "organic_action" in o:
                a, t = o["organic_action"], o["organic_turn"]
                fmt = lambda x: (f"{x['k']}/{x['n']}={x['value']}" if x.get("measured") else f"unmeasured n={x['n']}")  # noqa: E731
                line += f"  organic action {fmt(a)}  turn {fmt(t)}"
                line += (f"  release_clock action {fmt(o['release_clock_action'])}  "
                         f"turn {fmt(o['release_clock_turn'])}")
            print(line)
    return 0


def run_streak(*, end: str | None, as_json: bool = False, now: float | None = None) -> int:
    try:
        e = _parse_day(end, "--end") if end else None
    except DailyError as exc:
        print(f"kpi --daily-streak: {exc}", file=sys.stderr)
        return 2
    r = streak(e, now=now)
    if as_json:
        print(json.dumps(r, indent=2, sort_keys=True))
    else:
        print(f"end {r['end']}  release_clock measured_streak {r['measured_streak']}  "
              f"written_streak {r['written_streak']}  required {r['required']}  {'PASS' if r['pass'] else 'FAIL'}")
        ob = r["organic_first_break"]
        print(f"organic (reported separately) measured_streak {r['organic_streak']}  first break: "
              + (f"{ob['date']} ({ob['reason']})" if ob else "none within the walk"))
        b = r["first_break"]
        print("first break: " + (f"{b['date']} ({b['reason']})" if b else "none within the walk"))
        wb = r["written_first_break"]
        print("written break: " + (f"{wb['date']} ({wb['reason']})" if wb else "none within the walk"))
    return 0 if r["pass"] else 1
