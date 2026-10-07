"""Haiku Option A guard (PLAN v16 P0.11, owner decision D-20 = A).

``haiku_rewrite: true`` in the tier policy ("Option A") serves eligible turns on Claude
Haiku. This module watches the evidence and turns the rewrite OFF when it goes bad. It is
the in-repo port of the research guard ``~/.rsi/research/local-usage/haiku_guard/haiku_guard.py``
(thresholds and the M0.8b changes unchanged), plus the D-20 triggers.

THE LEVER: an override file, never the YAML. A trip writes ``<state dir>/tier_overrides.json``::

    {"haiku_rewrite": false, "reason": "...", "ts": <epoch seconds>}

``ClaudeTierPolicy.load`` reads it after the YAML (override beats YAML), and the running proxy
applies it to its live policy on the next guard tick, so no restart is needed. The override is
one-way: it can turn the rewrite OFF, never on (a ``true`` in it is ignored). This module never
edits ``claude_tiers.yaml``. Rollback: delete ``tier_overrides.json`` (the YAML value applies
again at the next proxy start).

WHEN IT RUNS: on proxy start and then every ``INTERVAL_S`` (one hour) on an asyncio timer inside
the proxy (``guard_loop``), only while the live policy has the rewrite on. Every run appends one
status line to ``<state dir>/haiku_guard.log``.

TRIGGERS (any one trips; a trigger below its minimum n is "not evaluable" and never trips):

* ``redo`` (ported, M0.8b): over the last ``WINDOW_DAYS`` days under ANY tier-policy version,
  Haiku-served human turns n >= ``MIN_N`` (30) and redo rate > ``MAX_REDO`` (15%). Units and the
  redo definition are the O3 KPI's (``offload_share.build_units``: escalation in the turn or the
  next 2, or a receipt-band ``redone`` press), sessions of the kinds in
  ``LLM_ROUTER_HAIKU_GUARD_KINDS`` (comma list, default ``organic``) only. No transcript join here
  (the research guard had none either), so sub-agent first calls count as turns.
* ``audit_batch`` (ported, M0.8b): the newest audit summary with a ``haiku`` arm has n_rated >=
  ``AUDIT_MIN_N`` (30) and acceptable < ``AUDIT_MIN_ACCEPTABLE`` (75%).
* ``audit_daily`` (D-20): the daily blind audit of Haiku-served turns is acceptable below 8/10
  (``DAILY_AUDIT_MIN_RATE``, at n >= ``DAILY_AUDIT_MIN_N`` = 10) on 2 consecutive days.
* ``tier_retry`` (D-20): among Haiku-decided calls (the router chose the ``haiku`` tier for a
  call the client sent to another model; side calls excluded), the share Anthropic refused and
  the proxy retried unchanged (``tier_retry`` set) is > 1% at n >= 100.
* ``shadow`` (D-20, fed by GE4): blind Haiku-vs-Frontier shadow verdicts over the last
  ``SHADOW_WINDOW_DAYS`` days, acceptable below 26/30 at n >= 20.

INPUT FILES the guard reads besides the proxy ledger and ``user_signals.jsonl``:

* audit summaries: ``summary-*.json`` in ``LLM_ROUTER_HAIKU_GUARD_AUDIT_DIR`` (default
  ``<state dir>/haiku_watch/audits``), the M0.10 summary layout: ``arms.haiku.{k_acceptable,
  n_rated}`` (and optionally ``arms.control``), dated by a ``date`` field (``YYYY-MM-DD``) or a
  ``batch`` that starts with ``YYYYMMDD``. One day = its newest file by mtime;
* shadow verdicts: ``LLM_ROUTER_HAIKU_GUARD_SHADOW_VERDICTS`` (default
  ``<state dir>/shadow_frontier/verdicts.jsonl``), one row per judged pair:
  ``{"ts": <epoch or ISO>, "acceptable": true|false, "cannot_judge": false}``; ``cannot_judge``
  rows are counted and left out of n.

No prompt text is read or written by this module: only counts, ids and timestamps.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from llm_router import paths

OVERRIDE_NAME = "tier_overrides.json"
STATUS_NAME = "haiku_guard.log"
INTERVAL_S = 3600.0

WINDOW_DAYS = 7
MIN_N = 30
MAX_REDO = 0.15
AUDIT_MIN_N = 30
AUDIT_MIN_ACCEPTABLE = 0.75
DAILY_AUDIT_MIN_N = 10
DAILY_AUDIT_MIN_RATE = 8 / 10
DAILY_AUDIT_DAYS = 2
RETRY_MIN_N = 100
RETRY_MAX_SHARE = 0.01
SHADOW_MIN_N = 20
SHADOW_MIN_RATE = 26 / 30
SHADOW_WINDOW_DAYS = 14

HAIKU_TIER = "haiku"
TRIGGERS = ("redo", "audit_batch", "audit_daily", "tier_retry", "shadow")
#: The D-20 triggers: the daily watch report FAILS a day on which any of them is not evaluable.
D20_TRIGGERS = ("audit_daily", "tier_retry", "shadow")



# ── paths and inputs ────────────────────────────────────────────────────────


def override_path() -> Path:
    return paths.state_path(OVERRIDE_NAME)


def status_path() -> Path:
    return paths.state_path(STATUS_NAME)


def audit_dir() -> Path:
    raw = os.environ.get("LLM_ROUTER_HAIKU_GUARD_AUDIT_DIR", "").strip()
    return Path(raw).expanduser() if raw else paths.state_path("haiku_watch", "audits")


def shadow_verdicts_path() -> Path:
    raw = os.environ.get("LLM_ROUTER_HAIKU_GUARD_SHADOW_VERDICTS", "").strip()
    return Path(raw).expanduser() if raw else paths.state_path("shadow_frontier", "verdicts.jsonl")


def guard_kinds() -> frozenset[str]:
    raw = os.environ.get("LLM_ROUTER_HAIKU_GUARD_KINDS", "")
    kinds = frozenset(k.strip().lower() for k in raw.split(",") if k.strip())
    return kinds or frozenset({"organic"})


def _num(x: Any) -> float | None:
    if isinstance(x, (int, float)) and not isinstance(x, bool) and x == x and abs(x) < 1e11:
        return float(x)
    if isinstance(x, str):
        try:
            dt = datetime.fromisoformat(x.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    return None


def _int(x: Any) -> int | None:
    return x if isinstance(x, int) and not isinstance(x, bool) else None


def read_jsonl(path: Path) -> list[dict]:
    """Dict rows only; a missing file, a torn line or a non-dict row is skipped."""
    out: list[dict] = []
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return out
    with fh:
        for line in fh:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                out.append(row)
    return out


# ── the override file ───────────────────────────────────────────────────────


def read_override() -> dict | None:
    """The override when it turns the rewrite off (``{"haiku_rewrite": False, "reason", "ts"}``),
    else None: no file, unreadable, not a mapping, or not ``haiku_rewrite: false`` (the override
    can only turn the rewrite off). Never raises."""
    p = override_path()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        from llm_router import failopen
        failopen.record("LR-FO-HAIKU-OVERRIDE-READ", exc)
        return None
    if not isinstance(raw, dict) or raw.get("haiku_rewrite") is not False:
        return None
    return {"haiku_rewrite": False, "reason": raw.get("reason"), "ts": raw.get("ts")}


def write_override(reason: str, ts: float) -> Path:
    """Write the override atomically (temp file + rename). Raises OSError on failure."""
    p = override_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps({"haiku_rewrite": False, "reason": reason, "ts": round(ts, 3)}) + "\n",
                   encoding="utf-8")
    os.replace(tmp, p)
    return p


def apply_override(policy) -> bool:
    """Turn ``policy.haiku_rewrite`` off when the override says so. True when it changed."""
    if policy is None or not getattr(policy, "haiku_rewrite", False):
        return False
    ov = read_override()
    if ov is None:
        return False
    policy.haiku_rewrite = False
    policy.haiku_override = ov
    return True


# ── statistics ──────────────────────────────────────────────────────────────


def wilson(k: int, n: int, z: float = 1.959964) -> tuple[float, float] | None:
    """Wilson 95% interval for k/n; None when n == 0."""
    if n <= 0:
        return None
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def _trigger(name: str, *, n: int, k: int, min_n: int, tripped: bool, value: str,
             **extra: Any) -> dict:
    evaluable = n >= min_n
    return {"name": name, "n": n, "k": k, "min_n": min_n, "evaluable": evaluable,
            "tripped": bool(evaluable and tripped),
            "value": value if evaluable else f"not evaluable (n<min: {n}<{min_n})", **extra}


# ── the five triggers ───────────────────────────────────────────────────────


def _kind_resolver(rows: list[dict]):
    from llm_router import session_kind as sk

    index = sk.KindIndex(rows)
    return lambda sid, stamp=None: index.resolve(sid, stamp=stamp).kind


def redo_trigger(rows: list[dict], *, since: float, until: float, kinds: frozenset[str],
                 band_redone: Iterable[str] = (), kind_of=None) -> dict:
    """Haiku-served human turns and how many were redone, O3's definition (module doc)."""
    from llm_router import offload_share as osh

    kind_of = kind_of or _kind_resolver(rows)
    built = osh.build_units(rows, [], now=until, days=(until - since) / 86400.0, kind_of=kind_of,
                            allowed=kinds, band_redone=set(band_redone))
    haiku = [u for u in osh.turn_units(built["units"]) if u["class"] == osh.CLASS_HAIKU]
    n, k = len(haiku), sum(1 for u in haiku if u["redone"])
    turns = len(osh.turn_units(built["units"]))
    rate = k / n if n else None
    return _trigger("redo", n=n, k=k, min_n=MIN_N, tripped=rate is not None and rate > MAX_REDO,
                    value=f"redone {k}/{n}" + (f" = {rate:.1%}" if rate is not None else ""),
                    turns=turns, window_open=sum(1 for u in haiku if u["window_open"]),
                    bar=f"> {MAX_REDO:.0%} at n>={MIN_N}")


def is_haiku_decided(row: dict) -> bool:
    """The router chose the Haiku tier for a call the client sent to another model."""
    if row.get("tier_reason") == "side_call" or row.get("tier") != HAIKU_TIER:
        return False
    req = row.get("requested_model")
    return not (isinstance(req, str) and HAIKU_TIER in req.lower())


def tier_retry_trigger(rows: list[dict], *, since: float, until: float, kinds: frozenset[str],
                       kind_of=None) -> dict:
    kind_of = kind_of or _kind_resolver(rows)
    n = k = 0
    for r in rows:
        ts = _num(r.get("ts"))
        if ts is None or not since <= ts <= until or not is_haiku_decided(r):
            continue
        if kind_of(r.get("session_id"), r.get("session_kind")) not in kinds:
            continue
        n += 1
        k += 1 if r.get("tier_retry") else 0
    share = k / n if n else None
    return _trigger("tier_retry", n=n, k=k, min_n=RETRY_MIN_N,
                    tripped=share is not None and share > RETRY_MAX_SHARE,
                    value=f"tier_retry {k}/{n}" + (f" = {share:.1%}" if share is not None else ""),
                    bar=f"> {RETRY_MAX_SHARE:.0%} at n>={RETRY_MIN_N}")


def _audit_date(summary: dict) -> str | None:
    d = summary.get("date")
    if isinstance(d, str) and len(d) >= 10:
        try:
            return datetime.strptime(d[:10], "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            return None
    b = summary.get("batch")
    if isinstance(b, str) and len(b) >= 8 and b[:8].isdigit():
        try:
            return datetime.strptime(b[:8], "%Y%m%d").strftime("%Y-%m-%d")
        except ValueError:
            return None
    return None


def _arm(summary: dict, arm: str) -> tuple[int, int] | None:
    try:
        a = summary["arms"][arm]
        k, n = _int(a["k_acceptable"]), _int(a["n_rated"])
    except (KeyError, TypeError):
        return None
    if k is None or n is None or n < 0 or not 0 <= k <= n:
        return None
    return k, n


def read_audits(directory: Path | None = None) -> list[dict]:
    """Every audit summary with a ``haiku`` arm, newest first by mtime:
    ``{"file", "mtime", "date", "haiku": (k, n), "control": (k, n) | None}``."""
    d = directory or audit_dir()
    try:
        files = sorted(d.glob("summary-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []
    out = []
    for f in files:
        try:
            s = json.loads(f.read_text(encoding="utf-8"))
            mtime = f.stat().st_mtime
        except (OSError, ValueError):
            continue
        if not isinstance(s, dict):
            continue
        h = _arm(s, "haiku")
        if h is None:
            continue
        out.append({"file": f.name, "mtime": mtime, "date": _audit_date(s), "haiku": h,
                    "control": _arm(s, "control")})
    return out


def audit_batch_trigger(audits: list[dict]) -> dict:
    """M0.8b: the newest summary with a haiku arm, n >= 30 and acceptable < 75%."""
    a = next((x for x in audits if x["haiku"][1] > 0), None)
    if a is None:
        return _trigger("audit_batch", n=0, k=0, min_n=AUDIT_MIN_N, tripped=False, value="",
                        bar=f"< {AUDIT_MIN_ACCEPTABLE:.0%} at n>={AUDIT_MIN_N}")
    k, n = a["haiku"]
    return _trigger("audit_batch", n=n, k=k, min_n=AUDIT_MIN_N, tripped=k / n < AUDIT_MIN_ACCEPTABLE,
                    value=f"acceptable {k}/{n} = {k / n:.1%} ({a['file']})", file=a["file"],
                    bar=f"< {AUDIT_MIN_ACCEPTABLE:.0%} at n>={AUDIT_MIN_N}")


def audits_by_day(audits: list[dict]) -> dict[str, dict]:
    """One audit per date: the newest file (by mtime) for that date."""
    out: dict[str, dict] = {}
    for a in audits:  # newest first
        if a["date"] and a["date"] not in out:
            out[a["date"]] = a
    return out


def audit_daily_trigger(audits: list[dict], day: str | None) -> dict:
    """D-20: Haiku acceptable < 8/10 on ``day`` and on the day before it, each at n >= 10.
    Evaluable when ``day`` itself has n >= 10. ``day`` None = the newest audited date."""
    days = audits_by_day(audits)
    if day is None:
        day = max(days) if days else None
    bar = f"< {DAILY_AUDIT_MIN_RATE * 10:.0f}/10 at n>={DAILY_AUDIT_MIN_N} on {DAILY_AUDIT_DAYS} consecutive days"
    if day is None or day not in days:
        return _trigger("audit_daily", n=0, k=0, min_n=DAILY_AUDIT_MIN_N, tripped=False, value="",
                        day=day, bar=bar, control=None)
    chain = []
    d = datetime.strptime(day, "%Y-%m-%d")
    for i in range(DAILY_AUDIT_DAYS):
        a = days.get((d - timedelta(days=i)).strftime("%Y-%m-%d"))
        chain.append(a)
    low = [a is not None and a["haiku"][1] >= DAILY_AUDIT_MIN_N
           and a["haiku"][0] / a["haiku"][1] < DAILY_AUDIT_MIN_RATE for a in chain]
    k, n = days[day]["haiku"]
    prev = chain[1] if len(chain) > 1 else None
    prev_txt = f"; previous day {prev['haiku'][0]}/{prev['haiku'][1]}" if prev else "; previous day none"
    ctl = days[day]["control"]
    return _trigger("audit_daily", n=n, k=k, min_n=DAILY_AUDIT_MIN_N, tripped=all(low),
                    value=f"Haiku acceptable {k}/{n} on {day}{prev_txt}", day=day, bar=bar,
                    control=list(ctl) if ctl else None, file=days[day]["file"])


def shadow_trigger(rows: list[dict], *, until: float, since: float | None = None) -> dict:
    """D-20 / GE4: Haiku-vs-Frontier blind verdicts, acceptable below 26/30 at n >= 20."""
    since = until - SHADOW_WINDOW_DAYS * 86400.0 if since is None else since
    n = k = cj = 0
    for r in rows:
        ts = _num(r.get("ts"))
        if ts is None or not since <= ts <= until:
            continue
        if r.get("cannot_judge") is True:
            cj += 1
            continue
        if not isinstance(r.get("acceptable"), bool):
            continue
        n += 1
        k += 1 if r["acceptable"] else 0
    ci = wilson(k, n)
    ci_txt = f" (Wilson 95% {ci[0]:.3f}-{ci[1]:.3f})" if ci else ""
    return _trigger("shadow", n=n, k=k, min_n=SHADOW_MIN_N,
                    tripped=n > 0 and k / n < SHADOW_MIN_RATE,
                    value=f"acceptable {k}/{n}" + (f" = {k / n:.1%}" if n else "") + ci_txt,
                    wilson95=list(ci) if ci else None, cannot_judge=cj,
                    bar=f"< 26/30 at n>={SHADOW_MIN_N}")


# ── evaluation ──────────────────────────────────────────────────────────────


def _utc_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def evaluate(rows: list[dict], *, since: float, until: float, kinds: frozenset[str] | None = None,
             band_redone: Iterable[str] = (), audits: list[dict] | None = None,
             shadow_rows: list[dict] | None = None, audit_day: str | None = None) -> dict:
    """Every trigger over ``[since, until]`` (audit_daily on ``audit_day``; shadow over the
    ``SHADOW_WINDOW_DAYS`` before ``until``). ``tripped`` lists the triggers that fired."""
    kinds = kinds or guard_kinds()
    kind_of = _kind_resolver(rows)
    audits = read_audits() if audits is None else audits
    shadow_rows = read_jsonl(shadow_verdicts_path()) if shadow_rows is None else shadow_rows
    trig = {
        "redo": redo_trigger(rows, since=since, until=until, kinds=kinds, band_redone=band_redone,
                             kind_of=kind_of),
        "audit_batch": audit_batch_trigger(audits),
        "audit_daily": audit_daily_trigger(audits, audit_day),
        "tier_retry": tier_retry_trigger(rows, since=since, until=until, kinds=kinds, kind_of=kind_of),
        "shadow": shadow_trigger(shadow_rows, until=until),
    }
    return {"since": since, "until": until, "kinds": sorted(kinds), "triggers": trig,
            "tripped": [t for t in TRIGGERS if trig[t]["tripped"]]}


def _band_redone(since: float, until: float) -> set[str]:
    from llm_router import user_signal

    try:
        latest = user_signal.latest_by_key(since=since, until=until)
    except Exception as exc:  # noqa: BLE001 - no band rows is a weaker signal, not a crash
        from llm_router import failopen
        failopen.record("LR-FO-HAIKU-GUARD-SIGNALS", exc)
        return set()
    return {k for k, r in latest.items() if r.get("signal") == user_signal.SIGNAL_REDONE}


def _stamp(t: float) -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _append(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line.rstrip("\n") + "\n")


def status_fields(ev: dict) -> str:
    t = ev["triggers"]
    r = t["redo"]
    rate = f"{r['k'] / r['n']:.1%}" if r["n"] else "n/a"
    return (f"n_haiku={r['n']} redo_rate={rate} haiku_redone={r['k']}/{r['n']} units={r['turns']} "
            f"since={_stamp(ev['since'])} "
            + " ".join(f"{name}={'TRIP' if t[name]['tripped'] else ('ok' if t[name]['evaluable'] else 'n/e')}"
                       f"({t[name]['k']}/{t[name]['n']})" for name in TRIGGERS))


def run_once(policy=None, *, now: float | None = None, read_rows: Callable[[], list[dict]] | None = None,
             notify: Callable[[str, dict], None] | None = None) -> dict:
    """One guard run over the last ``WINDOW_DAYS`` days: evaluate, write the override on a trip
    (only while the rewrite is on), append one status line. Returns the evaluation plus
    ``action`` (``ok`` | ``trip`` | ``already_off``). Never raises; an internal error is logged
    as an ERROR status line and never trips."""
    t = time.time() if now is None else now
    since = t - WINDOW_DAYS * 86400.0
    try:
        if read_rows is None:
            from llm_router.proxy import ledger
            rows = ledger.read_rows(days=WINDOW_DAYS + 1)
        else:
            rows = read_rows()
        ev = evaluate(rows, since=since, until=t, band_redone=_band_redone(since, t))
        on = getattr(policy, "haiku_rewrite", None) if policy is not None else None
        overridden = read_override() is not None
        if ev["tripped"] and not overridden and on is not False:
            reason = "; ".join(f"{name}: {ev['triggers'][name]['value']} ({ev['triggers'][name]['bar']})"
                               for name in ev["tripped"])
            write_override(reason, t)
            apply_override(policy)
            ev["action"] = "trip"
            _append(status_path(), f"{_stamp(t)} TRIP {status_fields(ev)} -> {OVERRIDE_NAME} "
                                   f"haiku_rewrite=false; {reason}")
            (notify or _notify)(reason, ev)
        else:
            ev["action"] = "already_off" if (overridden or on is False) else "ok"
            _append(status_path(), f"{_stamp(t)} {ev['action'].upper()} {status_fields(ev)}")
        return ev
    except Exception as exc:  # noqa: BLE001 - a guard bug must never trip, only report
        from llm_router import failopen
        failopen.record("LR-FO-HAIKU-GUARD-RUN", exc)
        try:
            _append(status_path(), f"{_stamp(t)} ERROR {type(exc).__name__}: {str(exc)[:160]}")
        except OSError as werr:
            failopen.record("LR-FO-HAIKU-GUARD-STATUS", werr)
        return {"action": "error", "error": type(exc).__name__, "tripped": []}


def _notify(reason: str, ev: dict) -> None:
    """Tell the owner: the proxy's stderr (launchd log) and the operational alert sink."""
    print(f"llm-router proxy: HAIKU GUARD TRIPPED, haiku_rewrite now off via {override_path()}: {reason}",
          file=sys.stderr, flush=True)
    from llm_router import alerts
    alerts.emit_alert(alerts.HAIKU_GUARD_TRIP, detail={
        "tripped": ev["tripped"], "override": str(override_path()),
        "counts": {k: [v["k"], v["n"]] for k, v in ev["triggers"].items()}})


async def guard_loop(policy, *, interval_s: float | None = None, run=None) -> None:
    """Run the guard now and then every ``interval_s`` (default ``INTERVAL_S``) until cancelled.
    Each tick also applies an override someone else wrote. Never raises but CancelledError."""
    step = INTERVAL_S if interval_s is None else interval_s
    fn = run or run_once
    while True:
        try:
            await asyncio.to_thread(fn, policy)
            apply_override(policy)
        except Exception as exc:  # noqa: BLE001 - the timer must outlive one bad run
            from llm_router import failopen
            failopen.record("LR-FO-HAIKU-GUARD-LOOP", exc)
        await asyncio.sleep(step)


# ── the daily watch (``llm-router kpi --haiku-watch``) ──────────────────────


def watch(since: float, until: float, *, rows: list[dict] | None = None) -> dict:
    """The D-20 daily watch over ``[since, until]``: every trigger evaluated with its n.
    ``day_pass`` is False when any D-20 trigger is not evaluable (n below its minimum)."""
    if rows is None:
        from llm_router.proxy import ledger
        rows = [r for r in ledger.read_rows() if (t := _num(r.get("ts"))) is not None and since <= t <= until]
    ev = evaluate(rows, since=since, until=until, band_redone=_band_redone(since, until),
                  audit_day=_utc_day(until - 1e-3))
    haiku_decided = ev["triggers"]["tier_retry"]["n"]
    not_eval = [t for t in D20_TRIGGERS if not ev["triggers"][t]["evaluable"]]
    ev.update(haiku_decided_calls=haiku_decided, not_evaluable=not_eval, day_pass=not not_eval,
              override=read_override(), override_path=str(override_path()))
    return ev


def render_watch(ev: dict) -> str:
    t = ev["triggers"]
    lines = [f"Haiku watch {_stamp(ev['since'])} .. {_stamp(ev['until'])} (kinds: {', '.join(ev['kinds'])})",
             f"  Haiku-decided calls: n={ev['haiku_decided_calls']}"]
    for name in TRIGGERS:
        x = t[name]
        state = "TRIPPED" if x["tripped"] else ("not tripped" if x["evaluable"] else "NOT EVALUABLE")
        d20 = " [D-20]" if name in D20_TRIGGERS else ""
        lines.append(f"  {name}{d20}: {x['value']} | n={x['n']} min_n={x['min_n']} | bar {x['bar']} | {state}")
    ctl = t["audit_daily"].get("control")
    lines.append(f"  control (Sonnet) audit: {ctl[0]}/{ctl[1]}" if ctl else "  control (Sonnet) audit: none")
    ov = ev.get("override")
    lines.append(f"  override: {'haiku_rewrite=false (' + str(ov.get('reason')) + ')' if ov else 'none'}"
                 f" [{ev['override_path']}]")
    lines.append("  day: PASS (every D-20 trigger evaluated)" if ev["day_pass"] else
                 f"  day: FAIL (not evaluable: {', '.join(ev['not_evaluable'])})")
    return "\n".join(lines)
