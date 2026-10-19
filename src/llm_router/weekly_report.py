"""P35-5 (D-220): the weekly offload report, built from the daily rows only.

``build_week("2026-W41")`` reads ``<state>/daily/<date>.json`` for the seven UTC days of that ISO week
(``daily_offload.day_file``: the highest ``.revN``). It never re-scans a ledger. Per population it gives:

* ``trend``: one entry per calendar day; ``value`` None (printed ``n/a``) when the day is below the daily
  floor, and ``n`` None when the day has no usable row;
* ``offload``: the pooled action line (``k``/``n`` summed from the days' ``counts``), ``value`` and a Wilson
  95% interval only when ``n >= MIN_ACTIONS_DAY`` and the week has ``>= MIN_WEEK_DAYS`` rows, else
  ``informative: false`` and the text ``not informative (n=...)``. The pooled interval is Wilson: the daily
  rows carry no per-session counts, so the session-clustered bootstrap cannot be pooled;
* ``breakdowns``: the same pooling per task group / served model / lane;
* ``delta_vs_previous_week``: pooled value difference, only when both weeks are informative, else None with
  ``delta_note`` saying why. It is a difference of two rates; no significance is claimed.

Written to ``<state>/weekly/<YYYY-Www>.json`` and ``.md`` (atomic; rewritten only when the measurement
changes). ``run_maintenance`` is ``llm-router kpi --maintenance``: the due daily backfill, then the last two
closed weeks. Unknown prints ``n/a``, never 0.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from datetime import date as Date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA = "weekly-offload/1"
MIN_WEEK_DAYS = 5            # a week with fewer daily rows is not compared with another week
BACKFILL_DAYS = 7
LAST_RUN = ".last_run.json"
_WEEK_RE = re.compile(r"^(\d{4})-W(\d{2})$")
_POPULATIONS = ("release_clock", "organic")      # D-210: the release clock first, organic reported apart
_GROUPS = ("by_task_group", "by_served_model", "by_lane")
_VOLATILE = ("generated_at",)


class WeeklyError(Exception):
    pass


def weekly_dir() -> Path:
    from llm_router import paths
    return paths.state_path("weekly")


def week_start(iso_week: str) -> Date:
    m = _WEEK_RE.match(iso_week or "")
    if not m:
        raise WeeklyError(f"expected YYYY-Www, got {iso_week!r}")
    try:
        return Date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)
    except ValueError as exc:
        raise WeeklyError(f"{iso_week!r} is not an ISO week") from exc


def iso_week_of(day: Date) -> str:
    y, w, _ = day.isocalendar()
    return f"{y}-W{w:02d}"


def last_closed_week(now: float) -> str:
    today = datetime.fromtimestamp(now, timezone.utc).date()
    return iso_week_of(today - timedelta(days=today.weekday() + 7))


def _int(x: Any) -> int | None:
    return x if isinstance(x, int) and not isinstance(x, bool) and x >= 0 else None


CI_METHOD = "wilson_pooled_independent"
CI_LABEL = "Wilson 95% CI (assumes independent actions; daily rows carry no per-session counts)"


def _line(k: int, n: int, minimum: int, days: int) -> dict:
    """Pooled ``{k, n, value, ci, informative, reason}``; value and ci only when the floors are met."""
    from llm_router import daily_offload as do
    ok = n >= minimum and days >= MIN_WEEK_DAYS
    return {"k": k, "n": n, "min_n": minimum,
            "value": round(k / n, 4) if ok else None,
            "ci": do.wilson_ci(k, n) if ok else None,
            "ci_method": CI_METHOD,
            "informative": ok,
            "reason": None if ok else (f"not informative (n={n} < {minimum})" if n < minimum else
                                       f"not informative (n={n}, only {days} day(s) with a row, need {MIN_WEEK_DAYS})")}


def _load_rows(start: Date, base: Path) -> tuple[dict[str, dict], dict[str, str]]:
    """``({date: row}, {date: why not used})`` for the seven days. Closed ``daily-offload/2`` rows only."""
    from llm_router import daily_offload as do
    rows: dict[str, dict] = {}
    skipped: dict[str, str] = {}
    for i in range(7):
        ds = (start + timedelta(days=i)).isoformat()
        p = do.day_file(ds, base)
        if p is None:
            skipped[ds] = "no row"
            continue
        try:
            row = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            skipped[ds] = f"unreadable ({type(exc).__name__})"
            continue
        if not isinstance(row, dict) or row.get("schema") != do.SCHEMA or not isinstance(row.get("populations"), dict):
            skipped[ds] = f"schema {row.get('schema') if isinstance(row, dict) else None!r} not {do.SCHEMA}"
        elif row.get("partial"):
            skipped[ds] = "partial snapshot"
        else:
            rows[ds] = row
    return rows, skipped


def _summarise(rows: dict[str, dict], pop: str, start: Date) -> dict | None:
    """Pooled lines, trend and breakdowns for one population; None when no row carries it."""
    from llm_router import daily_offload as do
    trend, ka, na, kt, nt = [], 0, 0, 0, 0
    groups: dict[str, dict[str, list[int]]] = {g: {} for g in _GROUPS}
    used = 0
    for i in range(7):
        ds = (start + timedelta(days=i)).isoformat()
        p = rows[ds]["populations"].get(pop) if ds in rows else None
        c = p.get("counts") if isinstance(p, dict) else None
        a = (c.get("action") if isinstance(c, dict) else None) or {}
        t = (c.get("turn") if isinstance(c, dict) else None) or {}
        k, n = _int(a.get("k")), _int(a.get("n"))
        if k is None or n is None:       # missing day, or a row without this population: unknown, not 0
            trend.append({"date": ds, "value": None, "n": None})
            continue
        used += 1
        ka, na = ka + k, na + n
        kt, nt = kt + (_int(t.get("k")) or 0), nt + (_int(t.get("n")) or 0)
        line = p.get("action") if isinstance(p.get("action"), dict) else {}
        trend.append({"date": ds, "k": k, "n": n,
                      "value": line.get("value") if line.get("measured") else None})
        bd = p.get("breakdown") if isinstance(p.get("breakdown"), dict) else {}
        for g in _GROUPS:
            for key, grp in (bd.get(g) or {}).items():
                gk, gn = _int(grp.get("k")), _int(grp.get("n"))
                if gk is not None and gn is not None:
                    acc = groups[g].setdefault(key, [0, 0])
                    acc[0] += gk
                    acc[1] += gn
    if not used:
        return None
    off = _line(ka, na, do.MIN_ACTIONS_DAY, used)
    off["gap_to_0.60"] = round(off["value"] - do.TARGET, 4) if off["value"] is not None else None
    off["turn"] = _line(kt, nt, do.MIN_TURNS_DAY, used)
    off["unit"] = "action"
    off["population"] = pop
    off["days_pooled"] = used
    breakdowns = {g: {k: _line(v[0], v[1], do.MIN_ACTIONS_DAY, used) for k, v in sorted(m.items())}
                  for g, m in groups.items() if m}
    return {"offload": off, "trend": trend, "breakdowns": breakdowns}


def _primary(rows: dict[str, dict], start: Date) -> tuple[str | None, dict | None, dict]:
    sums = {p: s for p in _POPULATIONS if (s := _summarise(rows, p, start)) is not None}
    for p in _POPULATIONS:
        if p in sums:
            return p, sums[p], sums
    return None, None, sums


def build_week(iso_week: str, *, base: Path | None = None, now: float | None = None,
               _previous: bool = True) -> dict:
    """The week's report from daily rows only. Pure read; see :func:`write_week`."""
    import time
    from llm_router import daily_offload as do
    start = week_start(iso_week)
    base = do.daily_dir() if base is None else base
    rows, skipped = _load_rows(start, base)
    pop, main, sums = _primary(rows, start)
    end = start + timedelta(days=6)
    w: dict[str, Any] = {
        "schema": SCHEMA, "week": iso_week,
        "window": {"from": start.isoformat(), "to": end.isoformat(), "tz": "UTC"},
        "generated_at": datetime.fromtimestamp(time.time() if now is None else now,
                                               timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "days": len(rows), "days_skipped": skipped,
        "population": pop,
        "offload": main["offload"] if main else None,
        "trend": main["trend"] if main else [],
        "breakdowns": main["breakdowns"] if main else {},
        "populations": {p: s["offload"] for p, s in sums.items()},
        "delta_vs_previous_week": None,
        "delta_note": None,
        "source": "daily rows only (never re-scanned from ledgers)",
    }
    if not _previous:
        return w
    prev_id = iso_week_of(start - timedelta(days=7))
    prev = build_week(prev_id, base=base, now=now, _previous=False)
    cur_o, prev_o = w["offload"], prev["offload"]
    if cur_o and prev_o and w["population"] != prev["population"]:
        w["delta_note"] = (f"no delta claimed; different populations ({iso_week}: {w['population']}, "
                           f"{prev_id}: {prev['population']})")
    elif cur_o and prev_o and cur_o["informative"] and prev_o["informative"]:
        w["delta_vs_previous_week"] = {"previous_week": prev_id, "value": round(cur_o["value"] - prev_o["value"], 4),
                                       "population": w["population"], "n": cur_o["n"], "previous_n": prev_o["n"],
                                       "note": "difference of two pooled rates; no significance claimed"}
    else:
        def why(o, label):
            return (f"{label}: n/a (no daily row)" if not o else
                    f"{label}: not informative (n={o['n']}, {o['days_pooled']} day(s))")
        bad = [why(o, lbl) for o, lbl in ((cur_o, iso_week), (prev_o, prev_id)) if not (o and o["informative"])]
        w["delta_note"] = "no delta claimed; " + "; ".join(bad)
    return w


# ---------------------------------------------------------------- render

def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


def _fmt(line: dict) -> str:
    if line["informative"]:
        lo, hi = line["ci"]
        return f"{_pct(line['value'])} ({line['k']}/{line['n']}, 95% CI {_pct(lo)}..{_pct(hi)})"
    return f"n/a ({line['reason']})"


def render(w: dict) -> str:
    win = f"{w['window']['from']}..{w['window']['to']} UTC"
    out = [f"# Weekly offload {w['week']}", "",
           f"window {win}; {w['days']} of 7 days with a daily row; population {w['population'] or 'n/a'}", ""]
    o = w["offload"]
    if not o:
        out += [f"offload: n/a (no daily row in {win})"]
        return "\n".join(out) + "\n"
    out += [f"CI: {CI_LABEL}", "",
            f"offload per action: {_fmt(o)}", f"offload per turn: {_fmt(o['turn'])}",
            f"gap to 0.60: {'n/a' if o['gap_to_0.60'] is None else format(o['gap_to_0.60'], '+.4f')}"]
    d = w["delta_vs_previous_week"]
    out.append("vs previous week: " + (f"{d['value']:+.4f} (n={d['n']} vs {d['previous_n']}; {d['note']})"
                                       if d else w["delta_note"]))
    out += ["", "## Trend (per UTC day)"]
    for t in w["trend"]:
        out.append(f"- {t['date']}: n/a (no row)" if t["n"] is None else
                   f"- {t['date']}: {_pct(t['value'])} (n={t['n']})")
    if w["days_skipped"]:
        out.append("- not used: " + ", ".join(f"{d} ({r})" for d, r in sorted(w["days_skipped"].items())))
    for g, m in w["breakdowns"].items():
        out += ["", f"## {g.replace('_', ' ')}"]
        out += [f"- {k}: {_fmt(v)}" for k, v in m.items()]
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- files

def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _payload(w: dict) -> str:
    return json.dumps({k: v for k, v in w.items() if k not in _VOLATILE}, sort_keys=True)


def write_week(w: dict, *, base: Path | None = None) -> dict:
    """Write ``<week>.json`` and ``.md`` atomically. ``{status, path}``: ``written``, ``unchanged`` (same
    measurement already on disk) or ``skipped`` (no daily row: nothing to report)."""
    if not w["days"]:
        return {"status": "skipped", "path": None}
    base = weekly_dir() if base is None else base
    p = base / f"{w['week']}.json"
    try:
        if _payload(json.loads(p.read_text(encoding="utf-8"))) == _payload(w):
            return {"status": "unchanged", "path": str(p)}
    except (OSError, ValueError):
        pass        # no readable previous file: write a fresh one
    _atomic_text(base / f"{w['week']}.md", render(w))
    _atomic_text(p, json.dumps(w, indent=2, sort_keys=True) + "\n")
    return {"status": "written", "path": str(p)}


# ---------------------------------------------------------------- kpi --maintenance

def run_maintenance(*, now: float | None = None) -> int:
    """``kpi --maintenance``: the due daily backfill (7 closed days, existing rows untouched), then the last
    two closed ISO weeks. Idempotent; prints what it did. Defers (exit 0, writes nothing) while the benchmark
    marker exists; a second run while one holds ``weekly/.lock`` prints ``busy`` and exits 0. Niced."""
    import time
    from llm_router import daily_offload as do, file_lock
    now = time.time() if now is None else now
    if do.bench_marker().exists():
        print("kpi --maintenance: deferred (benchmark marker present); nothing done")
        return 0
    if hasattr(os, "nice"):
        try:
            os.nice(19)
        except OSError as exc:
            print(f"kpi --maintenance: could not lower priority ({type(exc).__name__})", file=sys.stderr)
    status: dict = {}
    with file_lock.exclusive_lock(weekly_dir() / ".lock", timeout=0, status=status) as got:
        if not got and status.get("state") == "held":
            print("kpi --maintenance: busy (another run holds weekly/.lock); exiting 0")
            return 0
        rc, weeks = 1, []
        try:
            print("kpi --maintenance: daily backfill (7 closed days)")
            rc = do.run_daily(date=None, backfill=BACKFILL_DAYS, force=False, partial=False, manifest=None,
                              now=now)
            last = last_closed_week(now)
            for wk in (iso_week_of(week_start(last) - timedelta(days=7)), last):
                w = build_week(wk, now=now)
                res = write_week(w)
                weeks.append({"week": wk, "days": w["days"], **res})
                print(f"kpi --maintenance: weekly {wk}  {res['status']}  {res['path'] or 'no daily rows'}  "
                      f"({w['days']} of 7 days)")
            return rc
        except Exception as exc:  # noqa: BLE001 - reported, recorded, exit code 1; the stamp below still lands
            print(f"kpi --maintenance: failed ({type(exc).__name__}: {exc})", file=sys.stderr)
            from llm_router import failopen
            failopen.record("CHZ-FO-WEEKLY-MAINTENANCE", exc)
            rc = 1
            return rc
        finally:
            try:
                _atomic_text(weekly_dir() / LAST_RUN,
                             json.dumps({"ts": now, "exit": rc, "weeks": weeks}, indent=2, sort_keys=True) + "\n")
            except OSError as exc:
                print(f"kpi --maintenance: could not write {LAST_RUN} ({type(exc).__name__})", file=sys.stderr)
