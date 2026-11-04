"""The ChatGPT Plus meter as Codex itself reports it, and the reserve that keeps it for sub-agents.

Why this exists (owner decision D-373, 2026-11-04): agent-route delegates whole sub-agent runs to
Codex and binds the answer, and the Plus WEEKLY meter is the scarce thing it spends. Every other
Codex caller (the ``llm_codex`` MCP tool, the router chain, Codex-served text jobs, the evaluator,
the calibrator, the agentic Codex tier) draws on the same meter. Their share is not attributable
today: the week 2026-10-28..11-01 shows meter points that no router dispatch row accounts for. So once
the weekly meter passes ``DEFAULT_RESERVE_PCT`` every caller except agent-route refuses, and says so in
``codex_meter.jsonl``.

Where the number comes from: Codex writes ``payload.rate_limits`` into every ``token_count`` event of
its rollout files (``$CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl``)::

    {"limit_id": "codex", "primary":   {"used_percent": 89.0, "window_minutes": 300,   "resets_at": 1793789964},
                          "secondary": {"used_percent": 18.0, "window_minutes": 10080, "resets_at": 1794221036}, ...}

``primary`` is the 5 h window and ``secondary`` the weekly one. Only ``limit_id: "codex"`` is the Plus meter; an event with
``limit_id: "premium"`` carries null windows and is skipped, and a ``used_percent`` outside 0-100 is not a percentage. The newest
reading is chosen by the EVENT's own ``timestamp`` (UTC), never by the file name, whose clock is local time.

Rules, all from the decision:

* A reading is TRUSTED for ``MAX_METER_AGE_S`` (30 min): inside that it decides alone. Older, it can still REFUSE: a window's
  ``used_percent`` only rises until its ``resets_at``, so a stale reading at or above a line holds until then (weekly 80% read
  at 10:00 still refuses at 11:00). A stale reading under the line decides nothing.
* Without a deciding reading the guard falls back to the router's own counters (today's agent-route dispatches and the
  rolling 5 h window), never to "unlimited". If even the counters cannot be read the guard refuses: an unknown meter is not
  an open one.
* Both windows refuse: the weekly one at the reserve line, the 5 h one at ``FIVE_HOUR_SPENT_PCT`` (the same line the counter
  branch draws when the router's own 5 h window is spent).
* Every refusal is logged (``codex_meter.jsonl``). A rollout directory that cannot be read, or a reading whose format is not
  the one above, is counted with ``failopen`` (once per process). The reader never writes into ``$CODEX_HOME``.
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
#: 5 h-meter percentage at or above which the same callers refuse: the window is spent, and a call from outside would
#: take what agent-route needs for the rest of it. Also the line ``codex_bench`` reads as "no headroom".
FIVE_HOUR_SPENT_PCT = 90.0
#: A reading older than this does not decide alone (D-373: "never trust a meter older than 30 min").
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
#: At most this many rollouts are looked at, newest first.
_MAX_FILES = 40
#: Day directories scanned (newest first) for a reading that must be fresh: the directory date is local time and a fresh
#: reading is at most 30 min old.
_DAY_DIRS = 3
#: ... and for a stale reading that may still refuse: the weekly window is 7 days, plus the local-time slack.
_STALE_DAY_DIRS = 9
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


_noted: set[tuple[str, str]] = set()


def _note_once(code: str, detail: str, exc: BaseException | None = None) -> None:
    """Count a rollout problem with ``failopen`` once per process and kind: the same unreadable directory or the same new
    format would otherwise write a row per guarded call."""
    key = (code, detail)
    if key in _noted:
        return
    _noted.add(key)
    failopen.record(code, exc, detail=detail)


def codex_home() -> Path:
    """``$CODEX_HOME``, else ``~/.codex``: where Codex keeps its rollouts. Read only."""
    raw = os.environ.get("CODEX_HOME", "").strip()  # literal on purpose: tests/test_env_registry.py reads the source
    return Path(raw).expanduser() if raw else Path.home() / ".codex"


# ------------------------------------------------------------------------------------------ reading the meter


def _num(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _percent(value: object) -> float | None:
    """``value`` as a percentage, or None: it is not a number, or it lies outside 0-100."""
    number = _num(value)
    return number if number is not None and 0.0 <= number <= 100.0 else None


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
    """``(weekly, five_hour)`` window dicts from ``primary`` / ``secondary``, told apart by their length. A window whose
    percentage is missing or outside 0-100 is not one."""
    weekly = five = None
    for key in ("primary", "secondary"):
        win = rate_limits.get(key)
        if not isinstance(win, dict) or _percent(win.get("used_percent")) is None:
            continue
        minutes = _num(win.get("window_minutes"))
        if minutes is None:
            continue
        if minutes >= 0.9 * _WEEK_MINUTES:
            weekly = win
        elif minutes <= 1.2 * _FIVE_HOURS_MINUTES:
            five = win
    return weekly, five


def _reading_from_line(line: str, *, may_be_partial: bool = False) -> Meter | None:
    """The Plus-meter reading in one rollout line, or None when the line has none (most do not). A line that mentions
    ``rate_limits`` but is not what this reader expects is counted once with ``failopen``; ``may_be_partial`` is the last line
    of a file Codex may still be writing."""
    if '"rate_limits"' not in line:
        return None
    try:
        row = json.loads(line)
    except ValueError as exc:
        if not may_be_partial:
            _note_once("CHZ-FO-CODEX-METER-FORMAT", "a rollout line mentioning rate_limits is not JSON", exc)
        return None
    payload = row.get("payload") if isinstance(row, dict) else None
    if not isinstance(payload, dict) or "rate_limits" not in payload:
        return None  # the words appear in some other field: not a reading
    limits = payload["rate_limits"]
    if not isinstance(limits, dict):
        _note_once("CHZ-FO-CODEX-METER-FORMAT", "payload.rate_limits is not an object")
        return None
    limit_id = limits.get("limit_id")
    if limit_id != "codex":
        # "premium" (null windows) and any other bucket are not the Plus meter; a reading that carries windows but no id at
        # all is a format this reader does not know
        if limit_id is None and (limits.get("primary") or limits.get("secondary")):
            _note_once("CHZ-FO-CODEX-METER-FORMAT", "rate_limits with windows and no limit_id")
        return None
    observed = _event_epoch(row.get("timestamp"))
    weekly, five = _windows(limits)
    if observed is None or (weekly is None and five is None):
        _note_once("CHZ-FO-CODEX-METER-FORMAT", "a codex reading has no timestamp or no usable window")
        return None
    return Meter(
        observed_at=observed,
        weekly_used_pct=_percent(weekly.get("used_percent")) if weekly else None,
        weekly_resets_at=_num(weekly.get("resets_at")) if weekly else None,
        five_hour_used_pct=_percent(five.get("used_percent")) if five else None,
        five_hour_resets_at=_num(five.get("resets_at")) if five else None,
        plan=str(limits.get("plan_type") or ""),
    )


def _children(parent: Path) -> list[Path]:
    """Numeric subdirectories of ``parent``, newest name first. An absent directory is normal (Codex never ran); one that
    cannot be listed is counted."""
    try:
        return sorted((p for p in parent.iterdir() if p.is_dir() and p.name.isdigit()), reverse=True)
    except (FileNotFoundError, NotADirectoryError):
        return []
    except OSError as exc:
        _note_once("CHZ-FO-CODEX-METER-READ", f"cannot list {parent.name}", exc)
        return []


def _newest_day_dirs(sessions: Path, count: int) -> list[Path]:
    out: list[Path] = []
    for year in _children(sessions):
        for month in _children(year):
            for day in _children(month):
                out.append(day)
                if len(out) >= count:
                    return out
    return out


def _rollouts(root: Path, now: float, *, day_dirs: int, max_age_s: float | None) -> list[tuple[float, Path]]:
    """Rollout files as ``(mtime, path)``, newest first, at most ``_MAX_FILES``. With ``max_age_s`` only files written that
    recently (an event cannot be newer than its file's last write, so an older file holds no fresh reading)."""
    found: list[tuple[float, Path]] = []
    for day in _newest_day_dirs(root / "sessions", day_dirs):
        try:
            candidates = list(day.glob("rollout-*.jsonl"))
        except OSError as exc:
            _note_once("CHZ-FO-CODEX-METER-READ", f"cannot list {day.name}", exc)
            continue
        for path in candidates:
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue  # gone between the listing and now: Codex pruned or rotated it
            if max_age_s is None or now - mtime <= max_age_s + 120:
                found.append((mtime, path))
    found.sort(key=lambda item: item[0], reverse=True)
    return found[:_MAX_FILES]


def _tail_lines(path: Path) -> list[str]:
    with open(path, "rb") as fh:
        size = fh.seek(0, os.SEEK_END)
        start = max(0, size - _TAIL_BYTES)
        fh.seek(start)
        data = fh.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    return lines[1:] if start > 0 and lines else lines  # the first line of a mid-file read is cut


def _newest_reading(files: list[tuple[float, Path]]) -> Meter | None:
    best: Meter | None = None
    for mtime, path in files:
        if best is not None and best.observed_at >= mtime:
            break  # nothing in this or any older file can be newer
        try:
            lines = _tail_lines(path)
        except OSError as exc:
            _note_once("CHZ-FO-CODEX-METER-READ", f"cannot read {path.name[:60]}", exc)
            continue
        for index, line in enumerate(reversed(lines)):
            reading = _reading_from_line(line, may_be_partial=index == 0)
            if reading is None:
                continue
            if best is None or reading.observed_at > best.observed_at:
                best = reading
            break  # the newest reading of this file
    return best


def _live(best: Meter, now: float) -> Meter | None:
    """``best`` with each window dropped once that window's ``resets_at`` has passed (its percentage belongs to the old
    window); None when no window is left."""
    weekly, weekly_reset = best.weekly_used_pct, best.weekly_resets_at
    five, five_reset = best.five_hour_used_pct, best.five_hour_resets_at
    if weekly_reset is not None and weekly_reset <= now:
        weekly = None
    if five_reset is not None and five_reset <= now:
        five = None
    if weekly is None and five is None:
        return None
    return Meter(best.observed_at, weekly, weekly_reset, five, five_reset, best.plan)


def read_meter(now: float | None = None, *, max_age_s: float = MAX_METER_AGE_S, home: Path | None = None) -> Meter | None:
    """The newest FRESH reading of the Plus meter, or None.

    Fresh means: taken at most ``max_age_s`` ago (and not more than a minute in the future), from a ``limit_id: codex``
    ``token_count`` event, with each window's percentage dropped once that window's ``resets_at`` has passed. Never raises
    for a missing or unreadable rollout: a problem is counted with ``failopen``."""
    now = time.time() if now is None else now
    best = _newest_reading(_rollouts(home or codex_home(), now, day_dirs=_DAY_DIRS, max_age_s=max_age_s))
    if best is None or not (-60.0 <= best.age_s(now) <= max_age_s):
        return None
    return _live(best, now)


def read_last_meter(now: float | None = None, *, home: Path | None = None) -> Meter | None:
    """The newest reading of ANY age whose windows have not reset yet, or None. A window's percentage only rises until its
    ``resets_at``, so an old reading is a floor: it can refuse, it cannot admit."""
    now = time.time() if now is None else now
    best = _newest_reading(_rollouts(home or codex_home(), now, day_dirs=_STALE_DAY_DIRS, max_age_s=None))
    if best is None or best.age_s(now) < -60.0:
        return None
    return _live(best, now)


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
    #: ``meter`` (a fresh reading decided), ``meter_stale`` (an older reading, still a floor, refused), ``counters`` (the
    #: router's own counters decided), ``exempt`` (agent-route), ``off`` (the reserve is switched off) or ``unknown``
    #: (neither could be read: refused).
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


def _clock(epoch: float | None) -> str:
    return time.strftime("%H:%M", time.localtime(epoch)) if epoch else "?"


def _meter_verdict(caller: str, reserve: float, meter: Meter, now: float) -> ReserveVerdict | None:
    """The verdict a reading gives, or None when it decides nothing (a stale reading under both lines)."""
    age = meter.age_s(now)
    fresh = age <= MAX_METER_AGE_S
    weekly, five = meter.weekly_used_pct, meter.five_hour_used_pct
    held = "" if fresh else "; a window only rises until it resets, so it holds"
    if weekly is not None and weekly >= reserve:
        return ReserveVerdict(
            False, caller, "meter" if fresh else "meter_stale",
            f"weekly meter at {weekly:g}% (reserve {reserve:g}%), read {age / 60:.0f} min ago{held} until {_clock(meter.weekly_resets_at)}",
            reserve_pct=reserve, weekly_used_pct=weekly, meter_age_s=age)
    if five is not None and five >= FIVE_HOUR_SPENT_PCT:
        return ReserveVerdict(
            False, caller, "meter" if fresh else "meter_stale",
            f"5 h meter at {five:g}% (line {FIVE_HOUR_SPENT_PCT:g}%), read {age / 60:.0f} min ago{held} until {_clock(meter.five_hour_resets_at)}",
            reserve_pct=reserve, weekly_used_pct=weekly, meter_age_s=age)
    if fresh and weekly is not None:
        return ReserveVerdict(
            True, caller, "meter",
            f"weekly meter at {weekly:g}% (reserve {reserve:g}%), 5 h meter at {'?' if five is None else f'{five:g}%'}",
            reserve_pct=reserve, weekly_used_pct=weekly, meter_age_s=age)
    return None


def check(caller: str, now: float | None = None) -> ReserveVerdict:
    """May ``caller`` spend the Plus meter right now? Pure decision: nothing is logged here (see :func:`refusal`).

    A fresh reading decides. Without one, an older reading can still refuse (it is a floor until its window resets); and when
    nothing refuses and nothing is fresh, the router's own counters decide."""
    now = time.time() if now is None else now
    caller = (caller or "").strip() or "unspecified"
    if caller in EXEMPT_CALLERS:
        return ReserveVerdict(True, caller, "exempt", "agent-route spends the reserved meter")
    reserve = reserve_pct()
    if reserve is None:
        return ReserveVerdict(True, caller, "off", f"{RESERVE_ENV} is off")
    try:
        meter = read_meter(now)
        verdict = _meter_verdict(caller, reserve, meter, now) if meter is not None else None
        if verdict is None:
            last = read_last_meter(now)  # older than 30 min or without a weekly window: only a refusal can come of it
            if last is not None and last is not meter:
                verdict = _meter_verdict(caller, reserve, last, now)
                if verdict is not None and verdict.allowed:
                    verdict = None
    except Exception as exc:  # noqa: BLE001 -- counted; the counters decide instead
        failopen.record("CHZ-FO-CODEX-METER-READ", exc)
        verdict = None
    return verdict if verdict is not None else _counter_verdict(caller, reserve, now)


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
