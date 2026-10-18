"""Judge quality view (P3.1-6): one summary of what the judge has graded, for every surface.

Reads ``judge_results.jsonl`` (one text-free row per drain outcome) and
``judge_sampler.json`` (per-door seen/picked counters). It never queries a database, so
hooks and the statusline refresher may use it; the Stop hook goes further and only reads
the ``judge_summary.json`` cache that :func:`refresh_cache` writes from the detached child.

Conventions (same as ``ledger_view``): every figure carries its n and window; unknown is
``None`` and prints ``n/a``, never 0. A door with fewer than :data:`MIN_N` judged answers
is flagged ``not_informative`` (the live min-n rule, 50 per door).

Sampler counters are lifetime, not windowed (the sampler keeps no timestamps); every
printed form says so.

``session_id`` is accepted, but ``judge_results.jsonl`` rows carry no session id today, so
a session-scoped summary matches no row and reports ``n/a`` rather than a global figure
under a session label.
"""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from llm_router import failopen, paths
from llm_router.judge_select import same_model

MIN_N = 50
SCHEMA_VERSION = 1
RESULTS_FILENAME = "judge_results.jsonl"
SAMPLER_FILENAME = "judge_sampler.json"
CACHE_FILENAME = "judge_summary.json"
QUEUE_FILENAME = "judge_queue.jsonl"
DRAIN_CLAIM_FILENAME = "judge_drain.claim"
#: At most one auto-triggered drain (session start or session end, one shared claim) per this many seconds.
DRAIN_MIN_INTERVAL_S = 1800.0
#: The window the session-end line and the cache describe.
DEFAULT_WINDOW_DAYS = 14
GRADED = "graded"
#: Display-only relabel of ungraded statuses on the kpi section (the stored status is unchanged).
_UNGRADED_LABELS = {"failed": "not graded"}
UNKNOWN_DOOR = "unknown"
# The closed door set lives in judge_sampler; mirrored here so this module stays import-light.
_DOORS = ("proxy", "sdk", "gateway", "mcp", "hook", "agent", "technical_ops")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_number(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def window_label(days: float) -> str:
    return f"{int(days)}d" if float(days).is_integer() else f"{days:g}d"


@dataclass
class DoorStats:
    """One door. ``n`` is judged answers in the window; ``mean`` is None when ``n`` is 0."""

    n: int | None = None
    mean: float | None = None
    self_judge: int | None = None
    ungraded: dict = field(default_factory=dict)       # status -> rows in the window
    seen: int | None = None                            # sampler counters, lifetime
    picked: int | None = None
    lock_skipped: int | None = None
    not_informative: bool = True
    _sum: float = 0.0

    def to_dict(self) -> dict:
        return {"n": self.n, "mean": self.mean, "self_judge": self.self_judge,
                "ungraded": dict(self.ungraded), "seen": self.seen, "picked": self.picked,
                "lock_skipped": self.lock_skipped, "not_informative": self.not_informative}


@dataclass
class JudgeSummary:
    """Schema v1. ``n``/``mean``/``self_judge`` are None when there is no ``judge_results.jsonl``."""

    window: str
    since_ts: float | None
    until_ts: float
    generated_at: str
    n: int | None
    mean: float | None
    self_judge: int | None
    ungraded: dict
    skipped_rows: int
    seen: int | None
    picked: int | None
    not_informative: bool
    doors: dict
    session_id: str | None = None
    min_n: int = MIN_N
    v: int = SCHEMA_VERSION

    def to_dict(self) -> dict:
        d = {
            "v": self.v, "generated_at": self.generated_at, "generated_ts": self.until_ts,
            "window": {"label": self.window, "since_ts": self.since_ts, "until_ts": self.until_ts},
            "session_id": self.session_id, "min_n": self.min_n, "n": self.n, "mean": self.mean,
            "self_judge": self.self_judge, "ungraded": dict(self.ungraded),
            "skipped_rows": self.skipped_rows, "seen": self.seen, "picked": self.picked,
            "sampler_scope": "lifetime", "not_informative": self.not_informative,
            "doors": {d: s.to_dict() for d, s in self.doors.items()},
        }
        d["line"] = format_line(d)
        return d


def _to_ts(x, default: float | None = None) -> float | None:
    if x is None:
        return default
    if isinstance(x, datetime):
        return x.timestamp()
    if _is_number(x):
        return float(x)
    raise ValueError(f"not a timestamp: {x!r}")


def _read_rows(path) -> tuple[list[dict], int] | None:
    """(rows, malformed-line count); None when the file does not exist (unknown)."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        failopen.record("CHZ-FO-JUDGE-VIEW-READ", exc)
        return None
    rows: list[dict] = []
    bad = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if isinstance(row, dict):
            rows.append(row)
        else:
            bad += 1
    return rows, bad


def _read_sampler(path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        failopen.record("CHZ-FO-JUDGE-VIEW-READ", exc)
        return None
    return data if isinstance(data, dict) else None


def _count(d: dict, key: str) -> int | None:
    v = d.get(key)
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def judge_summary(since, until=None, session_id: str | None = None, *, label: str | None = None,
                  results_path=None, sampler_path=None, now: float | None = None) -> JudgeSummary:
    """Judged-answer figures over ``[since, until]`` (epoch or datetime; ``until`` default now).

    Per door and overall: n judged, mean score, self-judge count, ungraded rows by status, and
    the sampler's seen/picked. Judged = ``status == "graded"`` with a numeric score. Self-judge =
    a graded row whose judge is missing or may be the answering model (``judge_select.same_model``
    on names only, no inventory probe); it must be 0.
    """
    until_ts = _to_ts(until, time.time() if now is None else now)
    since_ts = _to_ts(since)
    if label is None:
        label = ("lifetime" if since_ts is None
                 else window_label(max(until_ts - since_ts, 0.0) / 86400.0))
    res = _read_rows(results_path or paths.state_path(RESULTS_FILENAME))
    sampler = _read_sampler(sampler_path or paths.state_path(SAMPLER_FILENAME))
    have_results = res is not None
    rows, skipped = res if res is not None else ([], 0)

    door_names = list(_DOORS)
    stats: dict[str, DoorStats] = {d: DoorStats() for d in door_names}

    def door_stats(name: str) -> DoorStats:
        if name not in stats:
            stats[name] = DoorStats()
        return stats[name]

    for name, c in (sampler or {}).items():
        if isinstance(c, dict):
            s = door_stats(name)
            s.seen, s.picked, s.lock_skipped = _count(c, "seen"), _count(c, "picked"), _count(c, "lock_skipped")

    if have_results:
        for s in stats.values():
            s.n, s.self_judge = 0, 0
        for r in rows:
            ts = r.get("ts")
            if not _is_number(ts) or (since_ts is not None and ts < since_ts) or ts > until_ts:
                continue
            if session_id is not None and r.get("session_id") != session_id:
                continue
            d = r.get("door") if isinstance(r.get("door"), str) and r.get("door") else UNKNOWN_DOOR
            s = door_stats(d)
            if s.n is None:
                s.n, s.self_judge = 0, 0
            status = r.get("status") if isinstance(r.get("status"), str) else "unrecorded"
            if status == GRADED and _is_number(r.get("score")):
                s.n += 1
                s._sum += float(r["score"])
                if not r.get("judge_model") or same_model(r.get("answering_model"), r.get("judge_model")):
                    s.self_judge += 1
            elif status != GRADED:
                s.ungraded[status] = s.ungraded.get(status, 0) + 1
            else:
                s.ungraded["graded_no_score"] = s.ungraded.get("graded_no_score", 0) + 1
    for s in stats.values():
        s.mean = s._sum / s.n if s.n else None
        s.not_informative = s.n is None or s.n < MIN_N
    doors = {d: s for d, s in stats.items() if s.n is not None or s.seen is not None or s.ungraded}
    doors = {d: doors[d] for d in (*_DOORS, *sorted(set(doors) - set(_DOORS))) if d in doors}

    n = sum(s.n for s in stats.values() if s.n is not None) if have_results else None
    total = sum(s._sum for s in stats.values())
    ungraded: dict = {}
    for s in stats.values():
        for k, v in s.ungraded.items():
            ungraded[k] = ungraded.get(k, 0) + v
    seens = [s.seen for s in stats.values() if s.seen is not None]
    picks = [s.picked for s in stats.values() if s.picked is not None]
    return JudgeSummary(
        window=label, since_ts=since_ts, until_ts=until_ts, generated_at=_iso(until_ts),
        n=n, mean=(total / n) if n else None,
        self_judge=sum(s.self_judge for s in stats.values() if s.self_judge is not None) if have_results else None,
        ungraded=ungraded, skipped_rows=skipped,
        seen=sum(seens) if seens else None, picked=sum(picks) if picks else None,
        not_informative=n is None or n < MIN_N, doors=doors, session_id=session_id)


# -- printed forms --------------------------------------------------------------

def _fmt_mean(mean: float | None) -> str:
    return "n/a" if mean is None else f"{mean:.2f}"


def format_line(js: "JudgeSummary | dict") -> str:
    """The session-end line: ``judge: mean X (n=N, window)`` or ``judge: n/a (no judged calls, window)``."""
    d = js.to_dict() if isinstance(js, JudgeSummary) else js
    win = d["window"]["label"]
    n, mean = d.get("n"), d.get("mean")
    if not n or mean is None:
        return f"judge: n/a (no judged calls, {win})"
    note = f", not informative n<{d.get('min_n', MIN_N)}" if d.get("not_informative") else ""
    return f"judge: mean {mean:.2f} (n={n}, {win}{note})"


def render_lines(js: "JudgeSummary | dict | None") -> list[str]:
    """The kpi judge quality section. Unknown parts print ``n/a``; n and window are always shown."""
    if js is None:
        return []
    d = js.to_dict() if isinstance(js, JudgeSummary) else js
    win, min_n = d["window"]["label"], d.get("min_n", MIN_N)
    n, mean = d.get("n"), d.get("mean")
    na = lambda v: "n/a" if v is None else str(v)                      # noqa: E731
    if not n:
        head = f"  judge quality ({win}): n/a (no judged calls; n={na(n)})"
    else:
        flag = f"; not informative (n<{min_n})" if d.get("not_informative") else ""
        head = f"  judge quality ({win}): mean {_fmt_mean(mean)} (n={n}){flag}"
    ung = ", ".join(f"{_UNGRADED_LABELS.get(k, k)} {v}" for k, v in sorted((d.get("ungraded") or {}).items())) or "none"
    lines = [head,
             f"  judge self-judge: {na(d.get('self_judge'))} (must be 0)   "
             f"ungraded: {ung}   sampler (lifetime): seen {na(d.get('seen'))}, picked {na(d.get('picked'))}"]
    for door, s in (d.get("doors") or {}).items():
        tag = f"  not informative (n<{min_n})" if s.get("not_informative") else ""
        lines.append(f"  judge door {door:<14s} n={na(s.get('n'))}  mean {_fmt_mean(s.get('mean'))}  "
                     f"self-judge {na(s.get('self_judge'))}  seen {na(s.get('seen'))}/picked {na(s.get('picked'))}{tag}")
    if d.get("skipped_rows"):
        lines.append(f"  judge {d['skipped_rows']} malformed judge_results rows skipped")
    return lines


# -- the cache the Stop reads, written by the detached child ---------------------

def cache_path():
    return paths.state_path(CACHE_FILENAME)


def refresh_cache(days: float = DEFAULT_WINDOW_DAYS, *, now: float | None = None) -> dict:
    """Compute the summary and write ``judge_summary.json`` atomically. Child/refresher only."""
    now = time.time() if now is None else now
    data = judge_summary(now - days * 86400.0, now, label=window_label(days), now=now).to_dict()
    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)      # only a failed write or replace leaves the tmp file
        raise
    return data


def read_cache() -> dict | None:
    """The cache, or None (absent, unreadable, another schema version). One small file read."""
    try:
        d = json.loads(cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) and d.get("v") == SCHEMA_VERSION else None


# -- detached drain trigger (shared by session-start and the session-end child) -----

#: argv tail both triggers spawn: lowest priority, then one drain. One script so the two cannot drift (F101).
DRAIN_SCRIPT = (
    "import os\n"
    "try:\n    os.nice(19)\nexcept (AttributeError, OSError):\n    pass\n"
    "import asyncio\nfrom llm_router.judge import drain_queue\nasyncio.run(drain_queue())\n"
)


def bench_active() -> bool:
    """True when the benchmark marker (``daily_offload.bench_marker()``, the ``bench.active`` file) exists.
    A drain calls a local model, which would perturb a running benchmark."""
    from llm_router.daily_offload import bench_marker

    return bench_marker().exists()


def drain_due(now: float | None = None) -> bool:
    """True when a drain should start now: auto-drain not opted out, no benchmark marker, the queue file is
    non-empty (a stat, no parse), and none was claimed in the last ``DRAIN_MIN_INTERVAL_S`` (by either
    trigger: session start or session end share the one claim file). Taking the claim is part of answering
    True. Never raises."""
    try:
        if os.environ.get("LLM_ROUTER_JUDGE_AUTODRAIN", "").strip().lower() in ("0", "off", "false", "no"):
            return False
        if bench_active():
            return False
        if os.stat(paths.state_path(QUEUE_FILENAME)).st_size <= 0:
            return False
        now = time.time() if now is None else now
        claim = paths.state_path(DRAIN_CLAIM_FILENAME)
        try:
            if 0 <= now - os.path.getmtime(claim) < DRAIN_MIN_INTERVAL_S:
                return False
        except OSError:
            pass
        claim.parent.mkdir(parents=True, exist_ok=True)
        claim.write_text(str(now))
        os.utime(claim, (now, now))
        return True
    except FileNotFoundError:
        return False
    except Exception as exc:  # noqa: BLE001
        failopen.record("CHZ-FO-JUDGE-VIEW-DRAIN-CLAIM", exc)
        return False
