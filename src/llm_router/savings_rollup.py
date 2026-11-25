"""Proxy-ledger savings, read without scanning the ledger on the render path (D-611, D-614).

``proxy_calls.jsonl`` is append-only and time-ordered (240 MB, 115k rows on 2026-11-25) and grows
about 2k rows a day. The status line must never scan it, so:

* TODAY is read by seeking back from the end of the file: a binary search on byte offsets finds
  the first row at or after the window start (``read_rows``), and only those rows are parsed.
* CLOSED DAYS come from ``savings_rollup.json`` under ``LLM_ROUTER_HOME``. It is written ONLY by
  the background refresh (``refresh``, called from ``statusline_segments``), never on a render
  path. A closed day is immutable once written. The file is discarded and rebuilt when the
  ledger's inode changes, the ledger is smaller than when last read, or its first row's ``ts``
  differs (rotation / truncation). Until the days a window needs are built, that window's proxy
  part is unavailable and the history prints ``n/a``: never a partial sum as a total.

A day is the local calendar day (``ledger_view.resolve("today")`` uses the same clock). A served
run that spans midnight is split across the two days (each day is summarised from its own rows
alone), a documented +/- one run. A row that lands more than :data:`SEEK_MARGIN_S` out of
time order is missed by a seek; it is read by a full scan.

Per day it stores what ``proxy.ledger`` computes, never a re-derivation: ``_served_runs`` /
``_net_avoided`` for the local-served part (B) and ``tier_stats`` for the tier-rewrite part (C).
Rows whose ``trace_id`` is already a ``usage.db`` row (``ledger_view.a_trace_ids``) are dropped
first (the dedupe key of ``ledger_view``'s module docstring)."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

from llm_router import paths

ROLLUP_NAME = "savings_rollup.json"
SCHEMA = 1
#: A seek starts this far before the window start, so a few rows written out of time order
#: (two sessions finishing at once) are not missed.
SEEK_MARGIN_S = 300.0
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


# ── seeking reader ────────────────────────────────────────────────────────────

def _row_ts(line: bytes):
    try:
        row = json.loads(line)
    except ValueError:
        return None
    ts = row.get("ts") if isinstance(row, dict) else None
    return ts if isinstance(ts, (int, float)) and not isinstance(ts, bool) else None


def _first_line_at(fh, pos: int, size: int):
    """``(line_start, ts)`` of the first complete row starting at or after byte ``pos``, or None."""
    if pos > 0:
        fh.seek(pos - 1)
        fh.readline()                      # finish the line ``pos`` landed in (or its terminator)
    else:
        fh.seek(0)
    while True:
        start = fh.tell()
        if start >= size:
            return None
        line = fh.readline()
        if not line:
            return None
        ts = _row_ts(line)
        if ts is not None:
            return start, ts


def _seek_start(fh, size: int, since: float) -> int:
    """Byte offset of the first row with ``ts >= since`` (``size`` when none): a binary search,
    valid because the ledger is time-ordered."""
    lo, hi = 0, size
    while lo < hi:
        mid = (lo + hi) // 2
        got = _first_line_at(fh, mid, size)
        if got is None or got[1] >= since:
            hi = mid
        else:
            lo = mid + 1
    got = _first_line_at(fh, lo, size)
    return got[0] if got else size


def read_rows(path, since: float | None, until: float | None, *, until_inclusive: bool = True,
              stats: dict | None = None) -> list[dict]:
    """Rows with ``since <= ts <= until`` (``< until`` when not ``until_inclusive``), in file
    order, without reading the rows before ``since``. ``stats`` (optional) receives
    ``bytes_read``. A missing file is no rows."""
    p = ledger_path(path)
    try:
        fh = p.open("rb")
    except OSError:
        return []
    out: list[dict] = []
    with fh:
        size = os.fstat(fh.fileno()).st_size
        start = 0 if since is None else _seek_start(fh, size, since - SEEK_MARGIN_S)
        fh.seek(start)
        stop_after = None if until is None else until + SEEK_MARGIN_S
        while True:
            line = fh.readline()
            if not line:
                break
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            ts = row.get("ts")
            if not isinstance(ts, (int, float)) or isinstance(ts, bool):
                continue
            if stop_after is not None and ts > stop_after:
                break
            if since is not None and ts < since:
                continue
            if until is not None and (ts > until if until_inclusive else ts >= until):
                continue
            out.append(row)
        if stats is not None:
            stats["bytes_read"] = fh.tell() - start
    return out


def ledger_identity(path=None) -> dict | None:
    """``{ino, size, first_ts}`` of the ledger (``first_ts`` None when the first row has no ts)."""
    p = ledger_path(path)
    try:
        st = p.stat()
        with p.open("rb") as fh:
            first = None
            for line in fh:
                first = _row_ts(line)
                if first is not None:
                    break
    except OSError:
        return None
    return {"ino": st.st_ino, "size": st.st_size, "first_ts": first}


# ── one slice of rows -> numbers ──────────────────────────────────────────────

def summarize(rows: list[dict], drop_trace: "set[str] | frozenset" = frozenset()) -> dict:
    """The proxy figures of ``rows`` (one window slice), through ``proxy.ledger`` only:
    ``_served_runs`` / ``_net_avoided`` (B) and ``tier_stats`` (C). A row whose ``trace_id`` is in
    ``drop_trace`` is a call ``usage.db`` already counts: dropped first."""
    from llm_router.proxy import ledger as L

    kept = [r for r in rows if not (r.get("trace_id") and r["trace_id"] in drop_trace)]
    out = dict(_ZERO)
    out["rows"] = len(kept)
    out["dropped"] = len(rows) - len(kept)
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


def _day_summary(day: str, ledger, db_path) -> dict:
    from llm_router import ledger_view as lv

    start, end = day_start(day), day_start(next_day(day))
    rows = read_rows(ledger, start, end, until_inclusive=False)
    drop = lv.a_trace_ids(lv.Bounds(day, day, start, end), db_path=db_path) if rows else frozenset()
    return summarize(rows, drop)


# ── the rollup file ───────────────────────────────────────────────────────────

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


def refresh(*, ledger=None, rollup=None, db_path=None, now: float | None = None,
            budget_s: float = 3.0) -> dict:
    """Bring the rollup up to date, newest closed day first, within ``budget_s`` seconds. Called
    ONLY by the background refresh. Returns ``{"built": days added, "missing": days still
    missing, "reset": bool}``. A held lock or a missing ledger changes nothing."""
    from llm_router.file_lock import exclusive_lock

    now = time.time() if now is None else now
    ident = ledger_identity(ledger)
    if ident is None or ident["first_ts"] is None:
        return {"built": 0, "missing": 0, "reset": False}
    rp = rollup_path(rollup)
    with exclusive_lock(rp.with_name(rp.name + ".lock"), timeout=0.0) as locked:
        if not locked:
            return {"built": 0, "missing": 0, "reset": False}
        roll = load(rollup)
        reset = False
        if not usable(roll, ledger) or roll.get("first_ts") != ident["first_ts"]:
            reset = roll is not None
            roll = {"v": SCHEMA, "days": {}}
        roll.update(ino=ident["ino"], size=ident["size"], first_ts=ident["first_ts"],
                    first_day=day_of(ident["first_ts"]), written_at=now)
        yesterday = day_of(midnight(now) - 3600.0)
        need = [d for d in reversed(days_between(roll["first_day"], yesterday)) if d not in roll["days"]]
        t0 = time.monotonic()
        built = 0
        for d in need:
            if built and time.monotonic() - t0 > budget_s:
                break
            roll["days"][d] = _day_summary(d, ledger, db_path)
            built += 1
        _write(roll, rollup)
        return {"built": built, "missing": len(need) - built, "reset": reset}


# ── a window's proxy figures ──────────────────────────────────────────────────

def proxy_parts(bounds, *, ledger=None, rollup=None, db_path=None) -> dict:
    """B and C for ``bounds`` (a ledger_view ``Bounds``). Keys: ``available`` (False = a day the
    window needs is not built yet, or the scope is not supported; ``na_reason`` says which), the
    ``_ZERO`` sums, ``first_day`` (the ledger's first day, None without a ledger).

    Today's part (and a partial first day) is read by seek, the closed days from the rollup;
    this is never a scan of the ledger."""
    from llm_router import ledger_view as lv

    out: dict = dict(_ZERO)
    out.update(available=True, na_reason=None, first_day=None)
    if bounds.session_id is not None:
        out.update(available=False, na_reason="session scope")
        return out
    ident = ledger_identity(ledger)
    if ident is None or ident["first_ts"] is None:
        return out                                     # no proxy ledger: no proxy rows, B and C are 0 of n=0
    out["first_day"] = day_of(ident["first_ts"])
    until, since = bounds.until_ts, bounds.since_ts
    today0 = midnight(until)

    def piece(lo: float, hi: float, inclusive: bool) -> dict:
        rows = read_rows(ledger, lo, hi, until_inclusive=inclusive)
        drop = lv.a_trace_ids(lv.Bounds("p", "p", lo, hi), db_path=db_path) if rows else frozenset()
        return summarize(rows, drop)

    _add(out, piece(max(since, today0) if since is not None else today0, until, True))
    if since is None or since < today0:
        roll = load(rollup)
        if not usable(roll, ledger):
            out.update(available=False, na_reason="rollup building")
            return out
        first = max(day_of(since) if since is not None else out["first_day"], out["first_day"])
        if since is not None and since > day_start(first) and first == day_of(since):
            _add(out, piece(since, day_start(next_day(first)), False))    # partial first day, by seek
            first = next_day(first)
        need = days_between(first, day_of(today0 - 3600.0))
        missing = [d for d in need if d not in roll["days"]]
        if missing:
            out.update(available=False, na_reason=f"rollup building ({len(missing)} d)")
            return out
        for d in need:
            _add(out, roll["days"][d])
    return out
