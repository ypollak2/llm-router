"""Was a provider bench wrong? -- the missing half of KPI G4.

KPI G4 is "wrongly benched providers = 0". ``llm-router kpi`` could only say how
many providers were benched RIGHT NOW: ``provider_reset.json`` records "blocked
until T" and nothing else, so once a bench lapsed or was cleared there was no
record that it had ever happened, let alone whether it was a mistake.

This module is that record. ``provider_reset`` appends to ``provider_bench.jsonl``
at the three moments that matter, one small line each::

    {"kind":"bench",  "provider":"codex","trigger":"cli","until":1790700000.0,"ts":1790600000.0}
    {"kind":"unban",  "provider":"codex","until":1790700000.0,"ts":1790600900.0}
    {"kind":"success","provider":"codex","until":1790700000.0,"ts":1790601200.0}

``bench``    a provider was benched until ``until`` (the value actually persisted,
             after the 7-day / 24-hour caps). ``trigger`` is where the reset time
             came from: ``header`` (``Retry-After`` / ``anthropic-ratelimit-*-reset``
             on an API response), ``cli`` (the full output of a CLI subprocess --
             Codex, Gemini CLI, Claude CLI) or ``text`` (an API error message).
``unban``    the owner cleared the bench with ``llm-router provider unban``.
             ``until`` is the reset time the entry held when it was cleared.
``success``  a call to the provider SUCCEEDED while the bench was in force.

WHAT "WRONG" MEANS, exactly as the owner defined it -- a bench is wrong when

(a) the owner cleared it with ``provider unban`` before it lapsed, or
(b) a call to that provider succeeded before its reset time.

Both are evidence that the provider was usable while the router held it out.
A bench the owner never contradicted, and that no call contradicted, is NOT
proven right: it is simply not (yet) shown wrong, which is why :func:`judge`
reports benches still in force separately -- they can still turn out wrong.

A later bench of the same provider REPLACES the earlier one in
``provider_reset.json``, so each bench is judged only over the span it was the
bench in force: from its own ``ts`` to the earlier of its ``until`` and the
next bench of that provider. An unban or a success is attributed to the bench
that was in force when it happened, once.

KNOWN LIMIT: a call that was already in flight when the bench was recorded and
then succeeds counts under (b). Two concurrent Codex tasks, one hitting the
limit while the other finishes, produce exactly that. It is still a call that
succeeded before the reset time, and the definition counts it.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field

from llm_router import capped_log, failopen
from llm_router.paths import state_path

__all__ = [
    "STORE_FILENAME",
    "TRIGGERS",
    "BenchJudgement",
    "log_bench",
    "log_unban",
    "log_success",
    "read_events",
    "judge",
    "store_path",
]

STORE_FILENAME = "provider_bench.jsonl"

#: Two generations of at most this many bytes. Benches are rare (a handful a
#: week on a busy machine); the cap only has to bound a runaway writer.
_MAX_BYTES = 1024 * 1024

TRIGGERS = ("header", "cli", "text")

#: ``(provider, until)`` pairs this process already logged a success for. A
#: benched provider that keeps succeeding must not write a row per call; one per
#: bench per process is the evidence, and ``judge`` counts a bench once anyway.
_success_logged: set[tuple[str, float]] = set()


def store_path():
    return state_path(STORE_FILENAME)


def _write(row: dict) -> None:
    try:
        capped_log.append(
            store_path(), (json.dumps(row, separators=(",", ":")) + "\n").encode("utf-8"), _MAX_BYTES
        )
    except Exception as exc:  # noqa: BLE001 -- never break routing for telemetry
        failopen.record("CHZ-FO-BENCH-LOG-WRITE", exc, detail=str(row.get("provider", ""))[:40])


def log_bench(provider: str, trigger: str, until: float, now: float | None = None) -> None:
    """A provider was benched until ``until``. Never raises."""
    if not _num(until):
        # ``judge`` drops a bench row with a non-numeric ``until`` anyway, so write
        # nothing; record the miss (its own code: a caller bug, not a disk failure) rather
        # than raising out of the routing path.
        failopen.record("CHZ-FO-BENCH-LOG-ARG", TypeError("non-finite or non-numeric until"),
                        detail=str(provider)[:40])
        return
    _write({
        "kind": "bench",
        "provider": provider,
        "trigger": trigger if trigger in TRIGGERS else "unknown",
        "until": round(until, 3),
        "ts": round(time.time() if now is None else now, 3),
    })


def log_unban(provider: str, until: float | None, now: float | None = None) -> None:
    """The owner cleared ``provider``'s bench. ``until`` is what the entry held."""
    row: dict = {"kind": "unban", "provider": provider,
                 "ts": round(time.time() if now is None else now, 3)}
    if isinstance(until, (int, float)) and not isinstance(until, bool):
        row["until"] = round(until, 3)
    _write(row)


def log_success(provider: str, until: float, now: float | None = None) -> None:
    """A call to ``provider`` succeeded while benched until ``until``."""
    key = (provider, round(until, 3))
    if key in _success_logged:
        return
    _success_logged.add(key)
    _write({
        "kind": "success",
        "provider": provider,
        "until": key[1],
        "ts": round(time.time() if now is None else now, 3),
    })


def _num(value: object) -> bool:
    """A usable timestamp: a real, finite number (not bool, NaN or inf)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def read_events(until: float | None = None) -> list[dict]:
    """Well-formed events with ``ts <= until``, oldest first. Never raises."""
    try:
        events = []
        for row in capped_log.read_dicts(store_path()):
            if row.get("kind") not in ("bench", "unban", "success"):
                continue
            if not isinstance(row.get("provider"), str) or not _num(row.get("ts")):
                continue
            if row["kind"] == "bench" and not _num(row.get("until")):
                continue
            if until is not None and row["ts"] > until:
                continue
            events.append(row)
        events.sort(key=lambda r: r["ts"])
        return events
    except Exception:  # noqa: BLE001
        return []


@dataclass(frozen=True)
class BenchJudgement:
    """Benches recorded in a window, and how many were shown wrong."""

    benches: int = 0
    #: Benches shown wrong by (a) an owner unban or (b) a success before the
    #: reset time. A bench that is both is counted once.
    wrong: int = 0
    wrong_by_unban: int = 0
    wrong_by_success: int = 0
    #: Benches still in force at ``now`` and not (yet) shown wrong. They can
    #: still turn out wrong, so ``wrong`` is a floor while this is above zero.
    active: int = 0
    by_trigger: dict[str, int] = field(default_factory=dict)
    #: ``ts`` of the newest bench in the window, or ``None`` with no bench.
    newest_ts: float | None = None


def judge(since: float, now: float) -> BenchJudgement:
    """Judge every bench recorded in ``[since, now]`` as of ``now``."""
    by_provider: dict[str, list[dict]] = {}
    for event in read_events(until=now):
        by_provider.setdefault(event["provider"], []).append(event)

    benches = wrong = by_unban = by_success = active = 0
    by_trigger: dict[str, int] = {}
    newest: float | None = None
    for events in by_provider.values():
        ordered = [e for e in events if e["kind"] == "bench"]
        for i, bench in enumerate(ordered):
            if bench["ts"] < since:
                continue
            # The span this bench was THE bench in force: a later bench of the
            # same provider replaced it in provider_reset.json.
            end = bench["until"]
            superseded = i + 1 < len(ordered)
            if superseded:
                end = min(end, ordered[i + 1]["ts"])
            benches += 1
            newest = bench["ts"] if newest is None else max(newest, bench["ts"])
            trigger = str(bench.get("trigger") or "unknown")
            by_trigger[trigger] = by_trigger.get(trigger, 0) + 1
            unbanned = any(
                e["kind"] == "unban" and bench["ts"] < e["ts"] < end for e in events
            )
            succeeded = any(
                e["kind"] == "success" and bench["ts"] < e["ts"] < end for e in events
            )
            by_unban += unbanned
            by_success += succeeded
            if unbanned or succeeded:
                wrong += 1
            elif not superseded and bench["until"] > now:
                active += 1
    return BenchJudgement(benches, wrong, by_unban, by_success, active, by_trigger, newest)
