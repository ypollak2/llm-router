"""Quota burn rate: the median weekly-quota percentage points per ACTIVE hour (P35-1, D-220).

One question: "how many points of the weekly window does an hour of my work cost?" The answer
turns a quota-% saved figure into ``~work-hours`` (:func:`work_hours`). It comes only from
``quota_history.jsonl`` (the 5-minute series written by ``statusline_tick``), never from a plan
name or a price: the user's own history is the calibration (D-205).

Definitions (each checked by tests/test_p35_fixtures.py and tests/test_quota_rate.py):

* A row counts only when ``source == "measured"`` and ``weekly_pct`` is a real number (stale rows
  and nulls are skipped, never read as 0).
* Between two consecutive counted rows at most :data:`MAX_GAP_S` apart, a RISE of ``weekly_pct``
  is quota burned. A fall is a weekly reset and is ignored. A larger gap is not attributable to
  one hour and is skipped.
* The rise is booked to the UTC hour of the later row. An ACTIVE hour is an hour with a rise > 0;
  its value is the sum of that hour's rises.
* The rate is the median over active hours in the window. Below :data:`MIN_ACTIVE_HOURS` it is
  ``None`` with ``na_reason = "n/a (k of 10 active hours)"``: a median of three hours is not a rate.
"""
from __future__ import annotations

import json
import statistics
import time
from dataclasses import dataclass
from pathlib import Path

from llm_router import paths

HISTORY_NAME = "quota_history.jsonl"
MIN_ACTIVE_HOURS = 10
MAX_GAP_S = 3600.0
_HOUR_S = 3600


@dataclass(frozen=True)
class Rate:
    """Median pp per active hour. ``value`` None = unknown (never 0); ``n`` = active hours behind it."""

    value: float | None
    n: int
    window: str
    na_reason: str | None = None
    bad_rows: int = 0            # history lines that were not valid JSON objects (skipped, counted)


def _num(x) -> float | None:
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and x == x else None


def active_hour_pps(rows: list[dict]) -> list[float]:
    """The pp burned in each active UTC hour, from history rows in file order (pure)."""
    per_hour: dict[int, float] = {}
    prev: tuple[float, float] | None = None
    for r in sorted((r for r in rows if r.get("source") == "measured"
                     and _num(r.get("ts")) is not None and _num(r.get("weekly_pct")) is not None),
                    key=lambda r: r["ts"]):
        ts, wk = float(r["ts"]), float(r["weekly_pct"])
        if prev is not None and 0 < ts - prev[0] <= MAX_GAP_S and wk > prev[1]:
            hour = int(ts // _HOUR_S)
            per_hour[hour] = per_hour.get(hour, 0.0) + (wk - prev[1])
        prev = (ts, wk)
    return [v for _, v in sorted(per_hour.items())]


def _read(path: Path, since: float) -> tuple[list[dict], int]:
    rows: list[dict] = []
    bad = 0
    with path.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except ValueError:
                bad += 1
                continue
            if not isinstance(r, dict):
                bad += 1
            elif (_num(r.get("ts")) or 0.0) >= since:
                rows.append(r)
    return rows, bad


def median_pp_per_active_hour(*, now: float | None = None, days: int = 14,
                              path: str | Path | None = None) -> Rate:
    """The median weekly pp per active hour over the last ``days`` days (see module docstring)."""
    now = time.time() if now is None else now
    window = f"{days}d"
    p = Path(path) if path is not None else paths.state_path(HISTORY_NAME)
    try:
        rows, bad = _read(p, now - days * 86400.0)
    except FileNotFoundError:
        return Rate(None, 0, window, "n/a (no quota history)")
    except OSError as exc:
        return Rate(None, 0, window, f"n/a (quota history unreadable: {exc.__class__.__name__})")
    pps = active_hour_pps(rows)
    if len(pps) < MIN_ACTIVE_HOURS:
        return Rate(None, len(pps), window,
                    f"n/a ({len(pps)} of {MIN_ACTIVE_HOURS} active hours)", bad)
    return Rate(float(statistics.median(pps)), len(pps), window, None, bad)


def work_hours(pp_saved: float | None, rate: Rate) -> float | None:
    """~work-hours = quota-% saved / median pp per active hour. None when either is unknown."""
    if pp_saved is None or rate.value is None or rate.value <= 0:
        return None
    return pp_saved / rate.value


def work_hours_figure(pp_saved: float | None, n_saved: int | None, rate: Rate, *,
                      window: str, as_of: str, source: str):
    """The ``~work-hours`` :class:`ledger_view.Figure`: always estimated, carrying BOTH n's
    (``n`` = rows behind the quota-% saved, ``n_basis`` = active hours behind the rate)."""
    from llm_router.ledger_view import Figure

    value = work_hours(pp_saved, rate)
    why = None
    if value is None:
        why = rate.na_reason or ("n/a (quota saved unknown)" if pp_saved is None else "n/a (rate is 0)")
    return Figure(value, n_saved, window, as_of, source, "estimated", why, rate.n)
