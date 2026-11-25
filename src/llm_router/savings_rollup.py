"""Proxy-ledger savings, without scanning the ledger on the render path (D-611, D-614).

``proxy_calls.jsonl`` is append-only but NOT time-ordered: rows are written when a call ends, so a
row of day D can land hours after D ended (measured 2026-11-25 on the live ledger, numeric ``ts``
only: 115,993 rows, 29,159 behind the running maximum, 306 more than 300 s late, worst 47,066 s).
So nothing here seeks by time or stops early on a late ``ts``.

``savings_rollup.json`` (``LLM_ROUTER_HOME``, written ONLY by the background refresh) holds a
resumable forward pass over the file: ``offset`` (a line boundary, everything before it is indexed)
and, per local calendar day, ``offs`` (the byte offset of every row whose OWN ``ts`` is in that
day) plus ``sum`` (that day's figures, or None when it must be recomputed). The pass buckets each
row by its own ts day, so a late row simply joins its day, and a closed day whose offsets grew has
its ``sum`` reset: closed days are updatable, never frozen. The pass spreads over refreshes under a
time budget and resumes at ``offset``. The file is discarded and rebuilt when the ledger's inode
changes, the ledger is smaller than when last read, or its first row's ts differs.

A window's proxy part is read from the summaries of its closed days plus the day rows of today and
of any day with rows after ``offset``; until the pass has caught up and every needed closed day has a
summary it is unavailable and prints ``n/a`` (never a partial sum as a total). A served run spanning
midnight is split across the two days (each day is summarised from its own rows), +/- one run.

Per day it stores what ``proxy.ledger`` computes (``_served_runs`` / ``_net_avoided`` for the
local-served part B, ``tier_stats`` for the tier part C). Rows whose ``trace_id`` is already a
``usage.db`` row (``ledger_view.a_trace_ids``) are dropped first."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

from llm_router import paths

ROLLUP_NAME = "savings_rollup.json"
SCHEMA = 2
_ZERO = {"b_net": 0.0, "b_runs": 0, "b_steps": 0, "b_unpriced": 0,
         "c_usd": 0.0, "c_rewritten": 0, "c_unpriced": 0, "dropped": 0, "rows": 0}


def rollup_path(path=None) -> Path:
    return Path(path) if path is not None else paths.state_path(ROLLUP_NAME)


def ledger_path(path=None) -> Path:
    return Path(path) if path is not None else paths.state_path("proxy_calls.jsonl")


# ── day arithmetic (local calendar days) ──────────────────────────────────────

def day_of(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def day_start(day: str) -> float:
    return datetime.strptime(day, "%Y-%m-%d").timestamp()


def next_day(day: str) -> str:
    return (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")


def midnight(ts: float) -> float:
    return day_start(day_of(ts))


def days_between(first: str, last: str) -> list[str]:
    """Every local day from ``first`` to ``last``, both included (empty when ``last < first``)."""
    out: list[str] = []
    d = first
    while d <= last:
        out.append(d)
        d = next_day(d)
    return out


# ── one slice of rows -> numbers ──────────────────────────────────────────────

def summarize(rows: list[dict], drop_trace: "set[str] | frozenset" = frozenset()) -> dict:
    """The proxy figures of ``rows`` (one window slice), through ``proxy.ledger`` only:
    ``_served_runs`` / ``_net_avoided`` (B) and ``tier_stats`` (C). A row whose ``trace_id`` is in
    ``drop_trace`` is a call ``usage.db`` already counts: dropped first."""
    from llm_router.proxy import ledger as L

    from llm_router import ledger_view as lv

    # executed calls only (the same rows ``ledger_view.executed_rows`` counts): a refused ``local:`` prefix and
    # an upstream HTTP error are not calls anyone made, so they are neither a served step nor a prefix.
    kept = [r for r in rows if lv._proxy_label(r) is not None
            and not (r.get("trace_id") and r["trace_id"] in drop_trace)]
    out = dict(_ZERO)
    out["rows"] = len(kept)
    out["dropped"] = sum(1 for r in rows if r.get("trace_id") and r["trace_id"] in drop_trace)
    runs = L._served_runs(kept)
    net, priced = L._net_avoided(kept)
    out["b_net"] = round(net, 6)
    out["b_runs"] = priced
    out["b_steps"] = sum(len(run) for run, _p, _n in runs)
    out["b_unpriced"] = len(runs) - priced
    ts = L.tier_stats(kept)
    if ts:
        out["c_usd"] = ts["est_saving_usd"]
        out["c_rewritten"] = ts["rewritten"]
        out["c_unpriced"] = ts["calls"] - ts["priced_n"]
    return out


def _add(total: dict, part: dict) -> None:
    for k in _ZERO:
        total[k] = total.get(k, 0) + part.get(k, 0)


# ── the rollup file ───────────────────────────────────────────────────────────

#: A window is unavailable while more than this many unindexed bytes trail the ledger's end.
TAIL_MAX_BYTES = 16 * 1024 * 1024


def _row_ts(row):
    ts = row.get("ts") if isinstance(row, dict) else None
    return ts if isinstance(ts, (int, float)) and not isinstance(ts, bool) else None


def ledger_identity(path=None) -> dict | None:
    """``{ino, size, first_ts}`` of the ledger (``first_ts`` None when no row has a ts)."""
    p = ledger_path(path)
    try:
        st = p.stat()
        first = None
        with p.open("rb") as fh:
            for line in fh:
                try:
                    first = _row_ts(json.loads(line))
                except ValueError:
                    continue
                if first is not None:
                    break
    except OSError:
        return None
    return {"ino": st.st_ino, "size": st.st_size, "first_ts": first}


def read_from(path, offset: int, *, max_lines: int | None = None, deadline: float | None = None):
    """``([(byte_offset, ts, row)], next_offset)`` for the complete lines from ``offset``: a forward read,
    no seeking by time. Stops at ``max_lines`` / ``deadline`` (monotonic) and before an unterminated last
    line (still being written), so ``next_offset`` is always a line boundary."""
    out: list = []
    try:
        fh = ledger_path(path).open("rb")
    except OSError:
        return out, offset
    with fh:
        fh.seek(offset)
        pos = offset
        n = 0
        while True:
            line = fh.readline()
            if not line or not line.endswith(b"\n"):
                break
            start, pos = pos, pos + len(line)
            n += 1
            try:
                row = json.loads(line)
            except ValueError:
                row = None
            ts = _row_ts(row)
            if ts is not None:
                out.append((start, ts, row))
            if max_lines is not None and n >= max_lines:
                break
            if deadline is not None and n % 500 == 0 and time.monotonic() > deadline:
                break
    return out, pos


def rows_at(path, offsets: list[int]) -> list[dict]:
    """The rows at the given byte offsets, in offset order."""
    out: list[dict] = []
    try:
        fh = ledger_path(path).open("rb")
    except OSError:
        return out
    with fh:
        for off in sorted(offsets):
            fh.seek(off)
            try:
                row = json.loads(fh.readline())
            except ValueError:
                continue
            if isinstance(row, dict):
                out.append(row)
    return out


def load(path=None) -> dict | None:
    try:
        data = json.loads(rollup_path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("v") != SCHEMA or not isinstance(data.get("days"), dict):
        return None
    return data


def usable(roll: dict | None, ledger=None) -> bool:
    """The rollup describes THIS ledger: same inode, not shrunk (cheap: one ``stat``)."""
    if roll is None:
        return False
    try:
        st = ledger_path(ledger).stat()
    except OSError:
        return False
    return st.st_ino == roll.get("ino") and st.st_size >= roll.get("size", 0)


def _write(roll: dict, path=None) -> None:
    p = rollup_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8", opener=paths.private_opener) as fh:
        json.dump(roll, fh, separators=(",", ":"))
    os.replace(tmp, p)


def _drop(day: str, db_path, rows: list[dict]):
    from llm_router import ledger_view as lv

    if not rows:
        return frozenset()
    return lv.a_trace_ids(lv.Bounds(day, day, day_start(day), day_start(next_day(day))), db_path=db_path)


def refresh(*, ledger=None, rollup=None, db_path=None, now: float | None = None,
            budget_s: float = 3.0, pass_lines: int | None = None) -> dict:
    """Advance the rollup within ``budget_s`` seconds: first the forward pass from ``offset`` (bucketing every
    row by its own ts day; a closed day that gains a row loses its ``sum``), then the summaries of closed
    days that lack one, newest first. Called ONLY by the background refresh. ``pass_lines`` caps the lines
    one call indexes (tests; the budget does it in production). Returns ``{"caught_up", "built" (summaries
    made), "missing" (closed days still without a sum), "reset"}``."""
    from llm_router.file_lock import exclusive_lock

    now = time.time() if now is None else now
    ident = ledger_identity(ledger)
    idle = {"caught_up": False, "built": 0, "missing": 0, "reset": False}
    if ident is None or ident["first_ts"] is None:
        return idle
    rp = rollup_path(rollup)
    with exclusive_lock(rp.with_name(rp.name + ".lock"), timeout=0.0) as locked:
        if not locked:
            return idle
        roll = load(rollup)
        reset = False
        if not usable(roll, ledger) or roll.get("first_ts") != ident["first_ts"]:
            reset = roll is not None
            roll = {"v": SCHEMA, "offset": 0, "days": {}}
        t0 = time.monotonic()
        entries, nxt = read_from(ledger, roll["offset"], max_lines=pass_lines, deadline=t0 + budget_s / 2)
        for off, ts, _row in entries:
            rec = roll["days"].setdefault(day_of(ts), {"offs": [], "sum": None})
            rec["offs"].append(off)
            rec["sum"] = None                      # a late row reopens its (closed) day
        roll.update(offset=nxt, ino=ident["ino"], size=ident["size"], first_ts=ident["first_ts"],
                    first_day=day_of(ident["first_ts"]), written_at=now)
        today = day_of(now)
        todo = sorted((d for d, r in roll["days"].items() if d < today and r["sum"] is None), reverse=True)
        built = 0
        for d in todo:
            if built and time.monotonic() - t0 > budget_s:
                break
            rows = rows_at(ledger, roll["days"][d]["offs"])
            roll["days"][d]["sum"] = summarize(rows, _drop(d, db_path, rows))
            built += 1
        _write(roll, rollup)
        caught = ident["size"] - nxt <= 0
        return {"caught_up": caught, "built": built, "missing": len(todo) - built, "reset": reset}


# ── a window's proxy figures ──────────────────────────────────────────────────

def proxy_parts(bounds, *, ledger=None, rollup=None, db_path=None) -> dict:
    """B and C for ``bounds`` (a ledger_view ``Bounds``). Keys: ``available`` (False = the forward pass has
    not caught up, or a closed day the window needs has no summary yet, or the scope is not supported;
    ``na_reason`` says which), the ``_ZERO`` sums, ``first_day`` (the ledger's first day, None without one).

    Reads the summaries of closed days, the rows of today (by recorded offsets) and the unindexed tail
    after ``offset``; never scans the ledger."""
    out: dict = dict(_ZERO)
    out.update(available=True, na_reason=None, first_day=None)
    if bounds.session_id is not None:
        out.update(available=False, na_reason="session scope")
        return out
    ident = ledger_identity(ledger)
    if ident is None or ident["first_ts"] is None:
        return out                                     # no proxy ledger: no proxy rows, B and C are 0 of n=0
    out["first_day"] = day_of(ident["first_ts"])
    roll = load(rollup)
    if not usable(roll, ledger) or roll.get("first_ts") != ident["first_ts"]:
        roll = {"offset": 0, "days": {}}               # nothing indexed: a small ledger is read whole, as the tail
    if ident["size"] - roll["offset"] > TAIL_MAX_BYTES:
        out.update(available=False, na_reason="rollup building")
        return out
    since, until = bounds.since_ts, bounds.until_ts
    tail, _ = read_from(ledger, roll["offset"])
    tail_by_day: dict[str, list] = {}
    for _off, ts, row in tail:
        tail_by_day.setdefault(day_of(ts), []).append(row)
    first = max(day_of(since) if since is not None else out["first_day"], out["first_day"])
    missing = 0
    for d in days_between(first, day_of(until)):
        lo, hi = day_start(d), day_start(next_day(d))
        rec = roll["days"].get(d)
        extra = tail_by_day.get(d, [])
        if rec is None and not extra:
            continue
        full = (since is None or since <= lo) and hi <= until
        if full and rec is not None and rec["sum"] is not None and not extra:
            _add(out, rec["sum"])
            continue
        if full and d < day_of(until) and rec is not None and rec["sum"] is None and not extra:
            missing += 1                                # a closed day not summarised yet: wait for the refresher
            continue
        rows = (rows_at(ledger, rec["offs"]) if rec else []) + extra
        rows = [r for r in rows if (since is None or r["ts"] >= since) and r["ts"] <= until]
        _add(out, summarize(rows, _drop(d, db_path, rows)))
    if missing:
        out.update(available=False, na_reason=f"rollup building ({missing} d)")
    return out
