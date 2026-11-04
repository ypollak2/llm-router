"""The ChatGPT Plus meter as Codex itself reports it, and the reserve that keeps it for sub-agents.

Why this exists (owner decision D-373, 2026-11-04): agent-route delegates whole sub-agent runs to
Codex and binds the answer, and the Plus WEEKLY meter is the scarce thing it spends. Every other
Codex caller (the ``llm_codex`` MCP tool, the router chain, Codex-served text jobs, the evaluator
and the calibrator) draws on the same meter. Their share is not attributable today: the week
2026-10-28..11-01 shows meter points that no router dispatch row accounts for. So once the weekly
meter passes ``DEFAULT_RESERVE_PCT`` every caller except agent-route refuses, and says so in
``codex_meter.jsonl``.

Where the number comes from: Codex writes ``payload.rate_limits`` into every ``token_count`` event of
its rollout files (``$CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl``)::

    {"limit_id": "codex", "primary":   {"used_percent": 89.0, "window_minutes": 300,   "resets_at": 1793789964},
                          "secondary": {"used_percent": 18.0, "window_minutes": 10080, "resets_at": 1794221036}, ...}

``primary`` is the 5 h window and ``secondary`` the weekly one. An event with ``limit_id: "premium"``
carries null windows and is skipped. The newest reading is chosen by the EVENT's own ``timestamp``
(UTC), never by the file name, whose clock is local time.

Rules, all from the decision:

* The newest reading is trusted for ``MAX_METER_AGE_S`` (30 min) and no longer. A reading older than
  that, or one whose window has already reset, is not used.
* With no usable reading the guard falls back to the router's own counters (today's agent-route dispatches
  and the rolling 5 h window), never to "unlimited". If even the counters cannot be read the guard
  refuses: an unknown meter is not an open one.
* Every refusal is logged (``codex_meter.jsonl``). The reader never writes into ``$CODEX_HOME``.
"""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from llm_router import capped_log, failopen, paths

#: Weekly-meter percentage at or above which every caller except agent-route refuses (D-373).
DEFAULT_RESERVE_PCT = 60.0
#: Env override for the line above. A number from 0 to 100 (0 refuses every non-agent-route caller,
#: 100 only at the wall); ``off`` disables the reserve.
RESERVE_ENV = "LLM_ROUTER_CODEX_RESERVE_PCT"
#: A reading older than this is not a reading (D-373: "never trust a meter older than 30 min").
MAX_METER_AGE_S = 30 * 60
#: Callers that spend the meter by design. Anything else, including a caller that names nothing, is guarded.
EXEMPT_CALLERS = frozenset({"agent_route"})

#: The router's own daily agent-route counter (``hooks/agent-route.py``). Keep both equal: a test loads the hook
#: and compares.
DEFAULT_DAILY_BUDGET = 100
DAILY_COUNTER_FILE = "agent_route_codex_budget.json"

EVENT_LOG = "codex_meter.jsonl"
_EVENT_LOG_MAX_BYTES = 512 * 1024

#: Rollout files are read from their tail; ``token_count`` events are frequent, so the newest reading is near the end.
_TAIL_BYTES = 1 << 20
#: At most this many recently modified rollouts are looked at, newest first.
_MAX_FILES = 40
#: Day directories scanned (newest first). The directory date is local time and a reading is at most 30 min old.
_DAY_DIRS = 3
_WEEK_MINUTES = 7 * 24 * 60
_FIVE_HOURS_MINUTES = 5 * 60


@dataclass(frozen=True)
class Meter:
    """One reading of the Plus meter. A percentage is ``None`` when the reading did not carry that window or the window had
    already reset when the reading was taken."""

    observed_at: float
    weekly_used_pct: float | None
    weekly_resets_at: float | None
    five_hour_used_pct: float | None
    five_hour_resets_at: float | None
    plan: str = ""

    def age_s(self, now: float) -> float:
        return now - self.observed_at


def codex_home() -> Path:
    """``$CODEX_HOME``, else ``~/.codex``: where Codex keeps its rollouts. Read only."""
    raw = os.environ.get("CODEX_HOME", "").strip()  # literal on purpose: tests/test_env_registry.py reads the source
    return Path(raw).expanduser() if raw else Path.home() / ".codex"


# ------------------------------------------------------------------------------------------ reading the meter


def _num(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _event_epoch(stamp: object) -> float | None:
    if not isinstance(stamp, str):
        return None
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _windows(rate_limits: dict) -> tuple[dict | None, dict | None]:
    """``(weekly, five_hour)`` window dicts from ``primary`` / ``secondary``, told apart by their length."""
    weekly = five = None
    for key in ("primary", "secondary"):
        win = rate_limits.get(key)
        if not isinstance(win, dict) or _num(win.get("used_percent")) is None:
            continue
        minutes = _num(win.get("window_minutes"))
        if minutes is None:
            continue
        if minutes >= 0.9 * _WEEK_MINUTES:
            weekly = win
        elif minutes <= 1.2 * _FIVE_HOURS_MINUTES:
            five = win
    return weekly, five


def _reading_from_line(line: str) -> Meter | None:
    """The reading in one rollout line, or None when the line has none (most do not)."""
    if '"rate_limits"' not in line:
        return None
    try:
        row = json.loads(line)
    except ValueError:
        return None
    payload = row.get("payload") if isinstance(row, dict) else None
    limits = payload.get("rate_limits") if isinstance(payload, dict) else None
    if not isinstance(limits, dict):
        return None
    observed = _event_epoch(row.get("timestamp"))
    if observed is None:
        return None
    weekly, five = _windows(limits)
    if weekly is None and five is None:
        return None  # a "premium" event: null windows
    return Meter(
        observed_at=observed,
        weekly_used_pct=_num(weekly.get("used_percent")) if weekly else None,
        weekly_resets_at=_num(weekly.get("resets_at")) if weekly else None,
        five_hour_used_pct=_num(five.get("used_percent")) if five else None,
        five_hour_resets_at=_num(five.get("resets_at")) if five else None,
        plan=str(limits.get("plan_type") or ""),
    )


def _newest_day_dirs(sessions: Path, count: int) -> list[Path]:
    out: list[Path] = []

    def children(parent: Path) -> list[Path]:
        try:
            return sorted((p for p in parent.iterdir() if p.is_dir() and p.name.isdigit()), reverse=True)
        except OSError:
            return []

    for year in children(sessions):
        for month in children(year):
            for day in children(month):
                out.append(day)
                if len(out) >= count:
                    return out
    return out


def _recent_rollouts(root: Path, now: float, max_age_s: float) -> list[Path]:
    found: list[tuple[float, Path]] = []
    for day in _newest_day_dirs(root / "sessions", _DAY_DIRS):
        try:
            candidates = list(day.glob("rollout-*.jsonl"))
        except OSError:
            continue
        for path in candidates:
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            # An event cannot be newer than the file's last write, so an old file holds no fresh reading.
            if now - mtime <= max_age_s + 120:
                found.append((mtime, path))
    found.sort(key=lambda item: item[0], reverse=True)
    return [path for _mtime, path in found[:_MAX_FILES]]


def _tail_lines(path: Path) -> list[str]:
    with open(path, "rb") as fh:
        size = fh.seek(0, os.SEEK_END)
        start = max(0, size - _TAIL_BYTES)
        fh.seek(start)
        data = fh.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    return lines[1:] if start > 0 and lines else lines  # the first line of a mid-file read is cut


def read_meter(now: float | None = None, *, max_age_s: float = MAX_METER_AGE_S,
               home: Path | None = None) -> Meter | None:
    """The newest usable reading of the Plus meter, or None.

    Usable means: taken at most ``max_age_s`` ago (and not in the future), from a ``token_count`` event that carried a window,
    with each window's percentage dropped once that window's ``resets_at`` has passed. Never raises for a missing or
    unreadable rollout: a file that cannot be read is skipped and counted with ``failopen``."""
    now = time.time() if now is None else now
    best: Meter | None = None
    for path in _recent_rollouts(home or codex_home(), now, max_age_s):
        try:
            lines = _tail_lines(path)
        except OSError as exc:
            failopen.record("CHZ-FO-CODEX-METER-READ", exc, detail=path.name[:80])
            continue
        for line in reversed(lines):
            reading = _reading_from_line(line)
            if reading is None:
                continue
            if best is None or reading.observed_at > best.observed_at:
                best = reading
            break  # the newest reading of this file
    if best is None or not (-60.0 <= best.age_s(now) <= max_age_s):
        return None
    weekly, weekly_reset = best.weekly_used_pct, best.weekly_resets_at
    five, five_reset = best.five_hour_used_pct, best.five_hour_resets_at
    if weekly_reset is not None and weekly_reset <= now:
        weekly = None  # that window has rolled over since the reading
    if five_reset is not None and five_reset <= now:
        five = None
    if weekly is None and five is None:
        return None
    return Meter(best.observed_at, weekly, weekly_reset, five, five_reset, best.plan)


# ------------------------------------------------------------------------------------------ the reserve


def reserve_pct() -> float | None:
    """The reserve line in percent of the weekly meter, or None when the reserve is switched off."""
    raw = os.environ.get("LLM_ROUTER_CODEX_RESERVE_PCT", "").strip().lower()
    if not raw:
        return DEFAULT_RESERVE_PCT
    if raw in ("off", "none", "disabled"):
        return None
    try:
        value = float(raw)
    except ValueError:
        failopen.record("CHZ-FO-CODEX-RESERVE-ENV", detail=f"{RESERVE_ENV}={raw[:20]!r} is not a number; using the default")
        return DEFAULT_RESERVE_PCT
    return min(max(value, 0.0), 100.0) if math.isfinite(value) else DEFAULT_RESERVE_PCT


@dataclass(frozen=True)
class ReserveVerdict:
    allowed: bool
    caller: str
    #: ``meter`` (a fresh reading decided), ``counters`` (the router's own counters decided), ``exempt`` (agent-route),
    #: ``off`` (the reserve is switched off) or ``unknown`` (neither could be read: refused).
    source: str
    reason: str
    reserve_pct: float | None = None
    weekly_used_pct: float | None = None
    meter_age_s: float | None = None


def _daily_budget() -> int:
    try:
        return max(0, int(os.environ.get("LLM_ROUTER_AGENT_ROUTE_CODEX_DAILY_BUDGET", str(DEFAULT_DAILY_BUDGET))))
    except (TypeError, ValueError):
        return DEFAULT_DAILY_BUDGET


def _daily_used(now: float) -> int:
    """Agent-route dispatches counted for today's UTC calendar day. A missing file or an older date is 0."""
    try:
        data = json.loads(paths.state_path(DAILY_COUNTER_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return 0
    today = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
    if not isinstance(data, dict) or data.get("date") != today:
        return 0
    return max(0, int(data.get("count", 0)))


def _counter_verdict(caller: str, reserve: float, now: float) -> ReserveVerdict:
    """Decide from the router's own counters when no fresh meter exists.

    Agent-route alone drawing ``reserve`` percent of its daily budget is a lower bound on what the weekly meter shows,
    at about one point per run; a spent 5 h window means the 5 h meter is near its wall. Either is "the meter is busy".
    Nothing readable at all refuses: the meter is unknown, and unknown is not open."""
    from llm_router import codex_window

    try:
        budget = _daily_budget()
        used = _daily_used(now)
        stamps = codex_window._in_window(codex_window._state_file(), now)
        window_used, window_budget = len(stamps), codex_window.budget()
    except Exception as exc:  # noqa: BLE001 -- counted, then refused
        failopen.record("CHZ-FO-CODEX-RESERVE-COUNTERS", exc)
        return ReserveVerdict(False, caller, "unknown",
                              "no fresh Codex meter and the router's own counters are unreadable",
                              reserve_pct=reserve)
    if budget > 0 and used >= reserve / 100.0 * budget:
        return ReserveVerdict(False, caller, "counters",
                              f"no fresh Codex meter; agent-route has used {used} of its {budget} dispatches today "
                              f"(reserve {reserve:g}%)", reserve_pct=reserve)
    if window_budget > 0 and window_used >= window_budget:
        return ReserveVerdict(False, caller, "counters",
                              f"no fresh Codex meter; the 5 h window is spent ({window_used}/{window_budget})",
                              reserve_pct=reserve)
    return ReserveVerdict(True, caller, "counters",
                          f"no fresh Codex meter; counters under the reserve ({used}/{budget} today, "
                          f"{window_used}/{window_budget} in 5 h)", reserve_pct=reserve)


def check(caller: str, now: float | None = None) -> ReserveVerdict:
    """May ``caller`` spend the Plus meter right now? Pure decision: nothing is logged here (see :func:`refusal`)."""
    now = time.time() if now is None else now
    caller = (caller or "").strip() or "unspecified"
    if caller in EXEMPT_CALLERS:
        return ReserveVerdict(True, caller, "exempt", "agent-route spends the reserved meter")
    reserve = reserve_pct()
    if reserve is None:
        return ReserveVerdict(True, caller, "off", f"{RESERVE_ENV} is off")
    try:
        meter = read_meter(now)
    except Exception as exc:  # noqa: BLE001 -- counted; the counters decide instead
        failopen.record("CHZ-FO-CODEX-METER-READ", exc)
        meter = None
    if meter is not None and meter.weekly_used_pct is not None:
        age = meter.age_s(now)
        if meter.weekly_used_pct >= reserve:
            return ReserveVerdict(
                False, caller, "meter",
                f"weekly meter at {meter.weekly_used_pct:g}% (reserve {reserve:g}%), read {age / 60:.0f} min ago",
                reserve_pct=reserve, weekly_used_pct=meter.weekly_used_pct, meter_age_s=age)
        return ReserveVerdict(
            True, caller, "meter", f"weekly meter at {meter.weekly_used_pct:g}% (reserve {reserve:g}%)",
            reserve_pct=reserve, weekly_used_pct=meter.weekly_used_pct, meter_age_s=age)
    return _counter_verdict(caller, reserve, now)


def log_event(kind: str, **fields: object) -> None:
    """Append one row to ``codex_meter.jsonl``. Never raises; a lost row is counted."""
    try:
        row = {"ts": round(time.time(), 3), "kind": kind, **fields}
        capped_log.append(paths.state_path(EVENT_LOG), (json.dumps(row, separators=(",", ":")) + "\n").encode("utf-8"),
                          _EVENT_LOG_MAX_BYTES)
    except Exception as exc:  # noqa: BLE001
        failopen.record("CHZ-FO-CODEX-METER-LOG", exc, detail=kind[:40])


def refusal(caller: str, now: float | None = None) -> str | None:
    """The refusal message for ``caller``, or None when it may spend the meter. A refusal is logged, every time.

    The wording avoids the words a usage-limit report uses (limit, quota, rate, try again): the router chain parses the
    text of a failed Codex call for a reset time and benches the provider, and this refusal must never become a bench."""
    verdict = check(caller, now)
    if verdict.allowed:
        return None
    log_event("refused", caller=verdict.caller, source=verdict.source, reason=verdict.reason[:200],
              reserve_pct=verdict.reserve_pct, weekly_used_pct=verdict.weekly_used_pct,
              meter_age_s=None if verdict.meter_age_s is None else round(verdict.meter_age_s, 1))
    return (f"Codex is reserved for sub-agent delegation (D-373): {verdict.reason}. The caller "
            f"'{verdict.caller}' was not run on Codex.")
