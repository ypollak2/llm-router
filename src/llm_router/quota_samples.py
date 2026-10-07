"""Quota-burn samples (PLAN v16 GE6, PRD S3).

Two append-only series, both read from the cached ``usage.json`` (never a network
call from here):

* ``quota_samples.jsonl`` -- one row per SessionStart and per Stop:
  ``{session_id, kind: start|stop, ts, five_hour_pct, weekly_pct, updated_at,
  source: measured|stale, session_kind}``. Written by the session-start and
  session-end (Stop) hooks through :func:`append_session_sample`.
* ``quota_history.jsonl`` -- one row at most every :data:`HISTORY_INTERVAL_S`
  (the 5-minute series): ``{ts, five_hour_pct, weekly_pct, updated_at, source}``.
  Written by the status-line tick (``statusline_tick.maybe_append_history``),
  which is stdlib-only and keeps its own copy of :func:`sample_from_usage`;
  ``tests/test_quota_samples.py`` pins the two copies to the same answers.

A snapshot is ``measured`` only when it carries real numbers (not the install
placeholder ``pending``, not the failed-fetch ``is_fallback`` 50s) and its
``updated_at`` is at most :data:`STALE_AFTER_S` old. Everything else is
``stale``; a value that is not a real number is recorded as null, never 0.

:func:`quota_burn` turns the rows into ``kpi --quota-burn``: burn per session and
per human turn (one Stop = one turn) from measured samples only; stale samples
form a separate line labelled ``estimated``. The rows hold percentages, a
session id and timestamps -- no prompt or response text.
"""

from __future__ import annotations

import json
import math
import os
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from llm_router import paths

SAMPLES_NAME = "quota_samples.jsonl"
HISTORY_NAME = "quota_history.jsonl"
#: A ``usage.json`` older than this is not a measurement (same 30 min as auto-route).
STALE_AFTER_S = 30 * 60.0
#: The history series' spacing (the status line's quota TTL).
HISTORY_INTERVAL_S = 300.0
KINDS = ("start", "stop")
SOURCE_MEASURED = "measured"
SOURCE_STALE = "stale"
_BOOT_RESAMPLES = 2000
#: Two 5h reset times further apart than this name two different windows.
RESET_TIME_TOLERANCE_S = 120.0
#: With no reset time to decide, a drop smaller than this is jitter, not a reset.
RESET_MIN_DROP_PTS = 5.0


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value in (math.inf, -math.inf):
        return None
    return float(value)


def _epoch(value: Any) -> float | None:
    """Epoch seconds from ``usage.json``'s ``session_resets_at`` (ISO string or a
    number); None when absent or unreadable. A naive time is read as UTC."""
    if isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    v = _num(value)
    return v if v is not None and 0 < v < float("inf") else None


def sample_from_usage(usage: Any, *, now: float) -> dict[str, Any]:
    """``{five_hour_pct, weekly_pct, updated_at, five_hour_resets_at, source}`` for
    one ``usage.json`` dict. ``five_hour_resets_at`` tells a 5h window reset from
    jitter in :func:`_burn`."""
    resets_at = _epoch(usage.get("session_resets_at")) if isinstance(usage, dict) else None
    if not isinstance(usage, dict) or usage.get("pending") or usage.get("is_fallback"):
        return {"five_hour_pct": None, "weekly_pct": None,
                "updated_at": _num(usage.get("updated_at")) if isinstance(usage, dict) else None,
                "five_hour_resets_at": resets_at, "source": SOURCE_STALE}
    h5, wk = _num(usage.get("session_pct")), _num(usage.get("weekly_pct"))
    updated = _num(usage.get("updated_at"))
    fresh = updated is not None and updated > 0 and 0 <= now - updated <= STALE_AFTER_S
    measured = fresh and h5 is not None and wk is not None
    return {"five_hour_pct": h5, "weekly_pct": wk, "updated_at": updated,
            "five_hour_resets_at": resets_at,
            "source": SOURCE_MEASURED if measured else SOURCE_STALE}


def _read_usage() -> dict | None:
    """Parsed ``usage.json``; one without ``updated_at`` is dated by its mtime,
    as the status line does."""
    path = paths.state_path("usage.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        mtime = path.stat().st_mtime
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if _num(data.get("updated_at")) in (None, 0.0):
        data["updated_at"] = mtime
    return data


def _append(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, (json.dumps(row, separators=(",", ":")) + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def append_session_sample(session_id: str | None, kind: str, *, now: float | None = None) -> bool:
    """Append one SessionStart (``kind="start"``) or Stop (``"stop"``) sample.

    Never raises; returns True when a row was written."""
    try:
        if not session_id or not isinstance(session_id, str) or kind not in KINDS:
            return False
        ts = time.time() if now is None else now
        row: dict[str, Any] = {"session_id": session_id, "kind": kind, "ts": ts}
        row.update(sample_from_usage(_read_usage(), now=ts))
        try:
            from llm_router import session_kind

            sk = session_kind.kind_of(session_id)
        except Exception:  # noqa: BLE001 - a missing tag is "untagged", not an error
            sk = None
        if sk:
            row["session_kind"] = sk
        _append(paths.state_path(SAMPLES_NAME), row)
        return True
    except Exception:  # noqa: BLE001 - telemetry never blocks a hook
        try:
            from llm_router import failopen

            failopen.record("CHZ-FO-QUOTA-SAMPLE", RuntimeError("quota sample not written"))
        except Exception:  # noqa: BLE001
            pass
        return False


# ── reading ─────────────────────────────────────────────────────────────────

def _read_jsonl(path: Path) -> tuple[list[dict], int]:
    rows: list[dict] = []
    skipped = 0
    try:
        fh = open(path, encoding="utf-8")
    except OSError:
        return rows, 0
    with fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                skipped += 1
                continue
            if not isinstance(row, dict) or _num(row.get("ts")) is None:
                skipped += 1
                continue
            rows.append(row)
    return rows, skipped


def _tagged_sessions(since: float, until: float) -> dict[str, str]:
    """session_id -> kind for every session tagged (session start) inside the window."""
    out: dict[str, str] = {}
    home = paths.llm_router_home()
    try:
        names = os.listdir(home)
    except OSError:
        return out
    for name in names:
        if not (name.startswith("session_kind_") and name.endswith(".json")):
            continue
        if name == "session_kind_overrides.json":
            continue
        try:
            tag = json.loads((home / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        ts = _num(tag.get("ts")) if isinstance(tag, dict) else None
        sid = tag.get("session_id") if isinstance(tag, dict) else None
        if ts is None or not sid or not since <= ts <= until:
            continue
        out[str(sid)] = str(tag.get("kind") or "")
    return out


def _kind_for(session_id: str, rows: list[dict], tagged: dict[str, str]) -> str | None:
    try:
        from llm_router import session_kind

        override = session_kind.override_of(session_id)
    except Exception:  # noqa: BLE001
        override = None
    if override:
        return override
    if tagged.get(session_id):
        return tagged[session_id]
    for r in rows:
        if r.get("session_kind"):
            return str(r["session_kind"])
    try:
        from llm_router import session_kind

        return session_kind.tag_kind_of(session_id)
    except Exception:  # noqa: BLE001
        return None


def _burn(values: list[float], resets_at: list[float | None] | None = None) -> tuple[float, int]:
    """Sum of the increases between consecutive readings, and the count of window resets.

    A reset is a changed 5h reset time (``resets_at``), or, where either reading has
    no reset time, a drop of at least :data:`RESET_MIN_DROP_PTS`. After a reset the
    window restarted at ~0, so the new reading is all burn, even when it is higher
    than the old one. A smaller drop inside one window is jitter and burns nothing."""
    total, resets = 0.0, 0
    marks = resets_at if resets_at is not None else [None] * len(values)
    for (a, b), (ra, rb) in zip(zip(values, values[1:]), zip(marks, marks[1:])):
        known = ra is not None and rb is not None
        if known and abs(rb - ra) > RESET_TIME_TOLERANCE_S:
            resets += 1
            total += b
        elif b >= a:
            total += b - a
        elif not known and a - b >= RESET_MIN_DROP_PTS:
            resets += 1
            total += b
    return total, resets


def _wilson(k: int, n: int) -> tuple[float | None, float | None]:
    if n <= 0:
        return None, None
    z = 1.959963984540054
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, centre - half), min(1.0, centre + half)


def _boot_ratio(pairs: list[tuple[float, int]], seed: int = 0) -> tuple[float, float] | None:
    """Session-clustered bootstrap 95% CI of sum(burn) / sum(turns)."""
    pairs = [p for p in pairs if p[1] > 0]
    if len(pairs) < 2:
        return None
    rng = random.Random(seed)
    stats = []
    n = len(pairs)
    for _ in range(_BOOT_RESAMPLES):
        draw = [pairs[rng.randrange(n)] for _ in range(n)]
        turns = sum(t for _, t in draw)
        stats.append(sum(b for b, _ in draw) / turns)
    stats.sort()
    return stats[int(0.025 * _BOOT_RESAMPLES)], stats[int(0.975 * _BOOT_RESAMPLES) - 1]


def _line(per_session: list[dict], label: str) -> dict[str, Any]:
    n = len(per_session)
    turns = sum(s["turns"] for s in per_session)
    h5 = sum(s["five_hour"] for s in per_session)
    wk = sum(s["weekly"] for s in per_session)
    out: dict[str, Any] = {
        "label": label, "n_sessions": n, "n_turns": turns,
        "five_hour_delta_sum": h5, "weekly_delta_sum": wk,
        "five_hour_per_session": h5 / n if n else None,
        "weekly_per_session": wk / n if n else None,
        "five_hour_per_turn": h5 / turns if turns else None,
        "weekly_per_turn": wk / turns if turns else None,
        "five_hour_per_turn_ci": _boot_ratio([(s["five_hour"], s["turns"]) for s in per_session]),
        "weekly_per_turn_ci": _boot_ratio([(s["weekly"], s["turns"]) for s in per_session]),
        "five_hour_resets": sum(s["resets"] for s in per_session),
        "largest_session_share": (max(s["turns"] for s in per_session) / turns) if turns else None,
        "top3_sessions": sorted((round(s["five_hour"], 3) for s in per_session), reverse=True)[:3],
    }
    return out


def quota_burn(since: float, until: float, *, include_research: bool = False) -> dict[str, Any]:
    """The ``kpi --quota-burn`` result for the absolute window ``[since, until]``."""
    allowed = {"organic", "research"} if include_research else {"organic"}
    samples, skipped = _read_jsonl(paths.state_path(SAMPLES_NAME))
    tagged = _tagged_sessions(since, until)

    by_session: dict[str, list[dict]] = {}
    for r in samples:
        sid = r.get("session_id")
        if not sid or r.get("kind") not in KINDS:
            skipped += 1
            continue
        if not since <= float(r["ts"]) <= until:
            continue
        by_session.setdefault(str(sid), []).append(r)

    measured, estimated = [], []
    n_measured_rows = n_stale_rows = 0
    excluded_kind = untagged = 0
    with_start = with_stop = covered = 0
    sessions = {sid for sid, k in tagged.items() if k in allowed}
    for sid, rows in by_session.items():
        kind = _kind_for(sid, rows, tagged)
        if kind is None:
            untagged += 1
            continue
        if kind not in allowed:
            excluded_kind += 1
            continue
        sessions.add(sid)
        rows.sort(key=lambda r: float(r["ts"]))
        has_start = any(r["kind"] == "start" for r in rows)
        turns = sum(1 for r in rows if r["kind"] == "stop")
        with_start += has_start
        with_stop += turns > 0
        covered += has_start and turns > 0
        good = [r for r in rows if r.get("source") == SOURCE_MEASURED
                and _num(r.get("five_hour_pct")) is not None and _num(r.get("weekly_pct")) is not None]
        numeric = [r for r in rows if _num(r.get("five_hour_pct")) is not None
                   and _num(r.get("weekly_pct")) is not None]
        n_measured_rows += sum(1 for r in rows if r.get("source") == SOURCE_MEASURED)
        n_stale_rows += sum(1 for r in rows if r.get("source") != SOURCE_MEASURED)
        use, bucket = (good, measured) if len(good) >= 2 else (numeric, estimated)
        if len(use) < 2:
            continue
        h5, resets = _burn([float(r["five_hour_pct"]) for r in use],
                           [_num(r.get("five_hour_resets_at")) for r in use])
        wk, _ = _burn([float(r["weekly_pct"]) for r in use])
        bucket.append({"session_id": sid, "turns": turns, "five_hour": h5, "weekly": wk,
                       "resets": resets})

    n_sessions = len(sessions)
    lo, hi = _wilson(covered, n_sessions)
    history = _history(since, until)
    m_line = _line(measured, "measured")
    return {
        "window": {"since": since, "until": until, "days": (until - since) / 86400.0},
        "population": "organic + research" if include_research else "organic",
        "measured": m_line,
        "estimated": _line(estimated, "estimated"),
        "samples": {"measured": n_measured_rows, "stale": n_stale_rows, "skipped": skipped,
                    "sessions_untagged": untagged, "sessions_other_kind": excluded_kind},
        "coverage": {"sessions": n_sessions, "with_start": with_start, "with_stop": with_stop,
                     "covered": covered, "rate": covered / n_sessions if n_sessions else None,
                     "wilson_lo": lo, "wilson_hi": hi},
        "history": history,
        "informative": m_line["n_sessions"] > 0,
    }


def _history(since: float, until: float) -> dict[str, Any]:
    rows, skipped = _read_jsonl(paths.state_path(HISTORY_NAME))
    ts = sorted(float(r["ts"]) for r in rows if since <= float(r["ts"]) <= until)
    inwin = [r for r in rows if since <= float(r["ts"]) <= until]
    slots = max(1, math.ceil((until - since) / HISTORY_INTERVAL_S))
    filled = {int((t - since) // HISTORY_INTERVAL_S) for t in ts}
    gaps = [b - a for a, b in zip(ts, ts[1:])]
    return {
        "rows": len(inwin),
        "measured": sum(1 for r in inwin if r.get("source") == SOURCE_MEASURED),
        "stale": sum(1 for r in inwin if r.get("source") != SOURCE_MEASURED),
        "skipped": skipped,
        "slots": slots, "slots_filled": len(filled),
        "first_ts": ts[0] if ts else None, "last_ts": ts[-1] if ts else None,
        "largest_gap_s": max(gaps) if gaps else None,
    }


# ── rendering ───────────────────────────────────────────────────────────────

def _iso(ts: float | None) -> str:
    if ts is None:
        return "n/a"
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _f(v: float | None, digits: int = 2) -> str:
    return "n/a" if v is None else f"{v:.{digits}f}"


def _ci(ci: tuple[float, float] | list | None) -> str:
    return "n/a" if not ci else f"[{ci[0]:.2f}, {ci[1]:.2f}]"


def render_quota_burn(r: dict[str, Any]) -> str:
    w = r["window"]
    lines = [f"Quota burn (S3) {_iso(w['since'])}..{_iso(w['until'])} ({w['days']:.1f} d), "
             f"population {r['population']}"]
    for key in ("measured", "estimated"):
        m = r[key]
        tag = "" if key == "measured" else " (stale usage.json: estimated, not a measurement)"
        if m["n_sessions"] == 0:
            lines.append(f"  {key}: not informative (n=0 sessions with >= 2 {key} samples){tag}")
            continue
        lines.append(
            f"  {key}{tag}: n_sessions={m['n_sessions']} n_turns={m['n_turns']} | "
            f"5h pts/session {_f(m['five_hour_per_session'])}, /turn {_f(m['five_hour_per_turn'], 3)} "
            f"95% CI {_ci(m['five_hour_per_turn_ci'])} | weekly pts/session {_f(m['weekly_per_session'])}, "
            f"/turn {_f(m['weekly_per_turn'], 3)} 95% CI {_ci(m['weekly_per_turn_ci'])} | "
            f"5h resets {m['five_hour_resets']} | largest session {_f(m['largest_session_share'])} of turns"
            + (" | n_sessions < 30: indicative only" if m["n_sessions"] < 30 else ""))
    s, c, h = r["samples"], r["coverage"], r["history"]
    lines.append(f"  samples: measured {s['measured']}, stale {s['stale']}, skipped {s['skipped']}, "
                 f"untagged sessions {s['sessions_untagged']}, other-kind sessions {s['sessions_other_kind']}")
    if c["sessions"]:
        lines.append(f"  coverage: {c['covered']}/{c['sessions']} sessions with start+stop samples "
                     f"({_f(c['rate'] * 100, 1)}%, Wilson {_f(c['wilson_lo'] * 100, 1)}-"
                     f"{_f(c['wilson_hi'] * 100, 1)}%); with start {c['with_start']}, with stop {c['with_stop']}")
    else:
        lines.append("  coverage: not informative (n=0 sessions in the window)")
    lines.append(f"  history (5-min series): {h['rows']} rows ({h['measured']} measured, {h['stale']} stale), "
                 f"{h['slots_filled']}/{h['slots']} 5-min slots filled, first {_iso(h['first_ts'])}, "
                 f"last {_iso(h['last_ts'])}, largest gap {_f(h['largest_gap_s'], 0)} s")
    return "\n".join(lines)
