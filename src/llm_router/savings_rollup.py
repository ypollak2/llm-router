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
midnight is attributed to the day it STARTS in, with the toll of its next Claude call even when that call is
on the next day (D-627): a day's B is computed over the rows of the day before, the day and the day after,
keeping the runs that start in the day. Baselines (the mean cache write of control calls, see
``proxy.ledger._write_baselines``) therefore come from that three-day context, not from the whole window:
a session with < 5 control calls in 3 days uses the 3-day mean of all sessions, where the whole-window estimator
uses the window mean. That is the remaining difference between the per-day sum and one pass over the window.
The render path (``proxy_parts``) reads no neighbour day: today, a partial day and a day without a stored sum
are summarised from their own rows (a run is priced with no prefix from the day before).
A day's summary depends on its neighbours, so a row landing in day X marks X-1 and X+1 ``stale`` (kept in use,
rebuilt by the next refresh).
A served row arriving for a day older than KEEP_OFFS_DAYS is priced as its own run: +/- about one run's net per
such row (measured 2026-11-25: |error| <= $0.05 per row on 5 samples; 306 of 116k rows arrived > 300 s late).
``late_old_rows`` in the rollup counts the served rows applied that way.

Per day it stores what ``proxy.ledger`` computes (``_served_runs`` / ``_net_avoided`` for the
local-served part B, ``tier_stats`` for the tier part C). Rows whose ``trace_id`` is already a
``usage.db`` row (``ledger_view.a_trace_ids``) are dropped first."""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

from llm_router import paths

ROLLUP_NAME = "savings_rollup.json"
SCHEMA = 5   # 5: B per start day with day-neighbour context (D-627); 4: per-part C sums, D-626
_ZERO = {"b_net": 0.0, "b_runs": 0, "b_steps": 0, "b_unpriced": 0,
         "c_usd": 0.0, "c_rewritten": 0,
         "c_unpriced": 0,                  # REWRITTEN rows with no price (usage unknown, or a model not in pricing.py)
         "c_unpriced_usage_unknown": 0,    # tier rows that are NOT rewrites, usage null: counted apart, never hidden
         "c_unpriced_unknown_model": 0,    # tier rows that are NOT rewrites, model not in the price table
         "c_rw_priced": 0, "c_rw_usd": 0.0, "c_rw_neg_n": 0, "c_rw_neg_usd": 0.0,
         "c_other_n": 0, "c_other_usd": 0.0,
         "dropped": 0, "rows": 0}
_PAIRS = "c_pairs"   # {"from>to": [n, usd]}: a dict, merged key by key (not in _ZERO: it must not be shared)


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

def summarize(rows: list[dict], drop_trace: "set[str] | frozenset" = frozenset(),
              before: list[dict] = (), after: list[dict] = ()) -> dict:
    """The proxy figures of ``rows`` (one window slice), through ``proxy.ledger`` only:
    ``_served_runs`` / ``_net_avoided`` (B) and ``tier_stats`` (C). A row whose ``trace_id`` is in
    ``drop_trace`` is a call ``usage.db`` already counts: dropped first. ``before`` / ``after`` are the rows
    of the neighbouring days (context only): B prices the runs that START in ``rows``, with prefix, next call
    and baselines read over ``before + rows + after``; the C part and the counts use ``rows`` alone."""
    from llm_router.proxy import ledger as L

    from llm_router import ledger_view as lv

    # executed calls only (the same rows ``ledger_view.executed_rows`` counts): a refused ``local:`` prefix and
    # an upstream HTTP error are not calls anyone made, so they are neither a served step nor a prefix.
    def _keep(rs):
        return [r for r in rs if lv._proxy_label(r) is not None
                and not (r.get("trace_id") and r["trace_id"] in drop_trace)]

    kept = _keep(rows)
    out = dict(_ZERO)
    out["rows"] = len(kept)
    out["dropped"] = sum(1 for r in rows if r.get("trace_id") and r["trace_id"] in drop_trace)
    if before or after:
        own = {id(r) for r in kept}
        ctx = _keep(before) + kept + _keep(after)
        runs = [t for t in L._served_runs(ctx) if id(t[0][0]) in own]
        d = L._net_avoided_detail(ctx, only=own)
    else:
        runs = L._served_runs(kept)
        d = L._net_avoided_detail(kept)
    net, priced = d["total"], d["n"]
    out["b_net"] = round(net, 6)
    out["b_runs"] = priced
    out["b_steps"] = sum(len(run) for run, _p, _n in runs)
    out["b_unpriced"] = len(runs) - priced
    out[_PAIRS] = {}
    ts = L.tier_stats(kept)
    if ts:
        out["c_usd"] = ts["est_saving_usd"]
        out["c_rewritten"] = ts["rewritten"]
        # D-626: c_unpriced is the REWRITTEN rows without a price. It used to be ``calls - priced_n`` (every
        # tier-decided row), which showed 627 on 30 d when 3 rewrites were unpriced (diag 2026-11-26).
        out["c_unpriced"] = ts["rewritten_unpriced"]
        out["c_unpriced_usage_unknown"] = ts["nonrewrite_unpriced_usage_unknown"]
        out["c_unpriced_unknown_model"] = ts["nonrewrite_unpriced_unknown_model"]
        out["c_rw_priced"], out["c_rw_usd"] = ts["rewrites_priced_n"], ts["rewrites_gross_usd"]
        out["c_rw_neg_n"], out["c_rw_neg_usd"] = ts["rewrites_negative_n"], ts["rewrites_negative_usd"]
        out["c_other_n"], out["c_other_usd"] = ts["nonrewrite_priced_n"], ts["nonrewrite_usd"]
        out[_PAIRS] = {f"{f}>{t}": [n, usd] for f, t, n, usd in ts["rewrite_pairs"]}
    return out


def _add(total: dict, part: dict) -> None:
    for k in _ZERO:
        total[k] = total.get(k, 0) + part.get(k, 0)
    pairs = total.setdefault(_PAIRS, {})
    for k, (n, usd) in (part.get(_PAIRS) or {}).items():
        cur = pairs.setdefault(k, [0, 0.0])
        cur[0] += n
        cur[1] = round(cur[1] + usd, 6)


# ── the rollup file ───────────────────────────────────────────────────────────

#: A window is unavailable while more than this many unindexed bytes trail the ledger's end.
TAIL_MAX_BYTES = 16 * 1024 * 1024
#: Row offsets are kept only for days younger than this many days (the file would otherwise grow by about
#: 9 bytes per row forever and be rewritten whole on every refresh). An older closed day keeps its ``sum``
#: alone; a LATE row for it is added incrementally as a one-row slice: exact for the tier part (C) and the
#: counts, and for a late served row B is priced as its own run (+/- one run, like a run split at midnight).
KEEP_OFFS_DAYS = 3
HEAD_BYTES = 64 * 1024


def head_hash(path, n: int) -> str | None:
    """sha1 of the first ``n`` bytes of the ledger (None when it cannot be read): a same-inode rewrite that
    grew past the old size keeps its first row but not its first 64 KB."""
    try:
        with ledger_path(path).open("rb") as fh:
            return hashlib.sha1(fh.read(n)).hexdigest()
    except OSError:
        return None


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
    size, n = roll.get("size"), roll.get("head_len")
    if not isinstance(size, int) or not isinstance(n, int):   # absent = unknown: rebuild, never read as 0
        return False
    if st.st_ino != roll.get("ino") or st.st_size < size:
        return False
    return n == 0 or (st.st_size >= n and head_hash(ledger, n) == roll.get("head"))


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


def _context(ledger, roll: dict, day: str, db_path):
    """``((rows_before, drop_before), (rows_after, drop_after))``: the indexed rows of the days next to
    ``day``. A neighbour without offsets (an old day, or none) gives no context. Refresher only: the
    render path (``proxy_parts``) never reads a neighbour day."""
    out = []
    for nd in (day_of(day_start(day) - 1.0), next_day(day)):
        rec = roll["days"].get(nd)
        rows = rows_at(ledger, rec["offs"]) if rec is not None and rec["offs"] else []
        out.append((rows, _drop(nd, db_path, rows)))
    return out[0], out[1]


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
            held = load(rollup)
            off = held.get("offset") if held else None   # absent offset = unknown, so not caught up
            return {**idle, "caught_up": isinstance(off, int) and ident["size"] - off <= 0}
        roll = load(rollup)
        reset = False
        if not usable(roll, ledger) or roll.get("first_ts") != ident["first_ts"]:
            reset = roll is not None
            roll = {"v": SCHEMA, "offset": 0, "days": {}}
        t0 = time.monotonic()
        entries, nxt = read_from(ledger, roll["offset"], max_lines=pass_lines, deadline=t0 + budget_s / 2)
        late: dict[str, list] = {}
        for off, ts, row in entries:
            d = day_of(ts)
            rec = roll["days"].setdefault(d, {"offs": [], "sum": None})
            if rec["offs"] is None:                # an old closed day keeps no offsets: add the late row to its sum
                late.setdefault(d, []).append(row)
                continue
            rec["offs"].append(off)
            rec["sum"] = None                      # a late row reopens its (closed) day
            for nd in (day_of(day_start(d) - 1.0), next_day(d)):
                nb = roll["days"].get(nd)          # its neighbours' B reads this row as context (D-627)
                if nb is not None and nb["sum"] is not None and nb["offs"] is not None:
                    nb["stale"] = True
        for d, rows in late.items():
            roll["late_old_rows"] = roll.get("late_old_rows", 0) + sum(1 for r in rows if r.get("decision") == "served")
            part = summarize(rows, _drop(d, db_path, rows))
            total = dict(roll["days"][d]["sum"] or _ZERO)
            _add(total, part)
            roll["days"][d]["sum"] = total
        head_len = min(HEAD_BYTES, ident["size"])
        roll.update(offset=nxt, ino=ident["ino"], size=ident["size"], first_ts=ident["first_ts"],
                    head_len=head_len, head=head_hash(ledger, head_len),
                    first_day=day_of(ident["first_ts"]), written_at=now)
        today = day_of(now)
        todo = sorted((d for d, r in roll["days"].items()
                       if d < today and (r["sum"] is None or r.get("stale"))), reverse=True)
        built = 0
        for d in todo:
            if built and time.monotonic() - t0 > budget_s:
                break
            rows = rows_at(ledger, roll["days"][d]["offs"])
            before, after = _context(ledger, roll, d, db_path)
            roll["days"][d]["sum"] = summarize(rows, _drop(d, db_path, rows) | before[1] | after[1], before[0], after[0])
            roll["days"][d].pop("stale", None)
            built += 1
        caught = ident["size"] - nxt <= 0
        if caught:                                 # never mid-pass: a day's later rows would then be "late" ones
            horizon = day_of(now - KEEP_OFFS_DAYS * 86400.0)
            for d, rec in roll["days"].items():
                if d < horizon and rec["sum"] is not None and rec["offs"] is not None:
                    rec["offs"] = None             # old and summarised: the sum alone is kept
        _write(roll, rollup)
        return {"caught_up": caught, "built": built, "missing": len(todo) - built, "reset": reset}


# ── a window's proxy figures ──────────────────────────────────────────────────

def proxy_parts(bounds, *, ledger=None, rollup=None, db_path=None) -> dict:
    """B and C for ``bounds`` (a ledger_view ``Bounds``). Keys: ``available`` (False = the forward pass has
    not caught up, or a closed day the window needs has no summary yet, or the scope is not supported;
    ``na_reason`` says which), the ``_ZERO`` sums, ``first_day`` (the ledger's first day, None without one).

    Reads the summaries of closed days, the rows of today (by recorded offsets) and the unindexed tail
    after ``offset``; never scans the ledger."""
    out: dict = dict(_ZERO)
    out[_PAIRS] = {}
    out.update(available=True, na_reason=None, first_day=None, late_old_rows=0)
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
    out["late_old_rows"] = roll.get("late_old_rows", 0)
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
        if rec is not None and rec["offs"] is None:
            if not full:                            # an old day without offsets cannot be sliced by time
                missing += 1
                continue
            total = dict(rec["sum"])                # sum + the unindexed late rows, as the refresher would add them
            _add(total, summarize(extra, _drop(d, db_path, extra)))
            _add(out, total)
            continue
        rows = (rows_at(ledger, rec["offs"]) if rec else []) + extra
        rows = [r for r in rows if (since is None or r["ts"] >= since) and r["ts"] <= until]
        _add(out, summarize(rows, _drop(d, db_path, rows)))   # no neighbour context: the render path reads today only
    if missing:
        out.update(available=False, na_reason=f"rollup building ({missing} d)")
    return out
