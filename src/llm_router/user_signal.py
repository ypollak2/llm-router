"""The person's own verdict on a routed answer: ``kept`` or ``redone``.

Written when they press ``k`` (keep) or ``r`` (redo on Claude) on the receipt
band (``src/llm_router/mods/llm-router-receipt``), one ``user_signal`` row per press,
to ``<router home>/user_signals.jsonl``::

    {"ts": 1791234000.0, "key": "msg_01ABC...", "signal": "kept", "surface": "terminal"}

``key`` is the routed turn's route id (the proxy ledger's ``msg_id`` of the
served reply). The row holds nothing else: no prompt, no answer, no model, no
session id. A key, a signal and a surface that do not match their patterns are
refused, so free text cannot ride in through them.

HOW THE KPI READS IT (owner rule, 2026-10-05)
--------------------------------------------
"Used" in the North Star requires a passing test. A press of ``k`` is NOT a
test, so ``kept`` is never counted as used: it never touches NS, D1, D2 or D3's
denominator, and ``llm-router kpi`` shows it on its own line. ``redone`` is a
clear negative: it is added to D3 (redo rate) as a decided redo event.
``tests/test_user_signal_kpi.py`` fails if keep ever moves NS, D1 or D2.

Per key the LAST signal wins (a keep that is followed by a redo of the same
turn is a redo), so pressing twice never counts twice.

Writes: 0600 at creation, appended under an exclusive lock (``file_lock``) as
one ``os.write`` of a complete line on an ``O_APPEND`` descriptor
(``capped_log``), and capped like the other KPI ledgers (two generations of
:data:`MAX_BYTES`).
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from llm_router import capped_log, paths

LEDGER_FILENAME = "user_signals.jsonl"
SIGNAL_KEPT = "kept"
SIGNAL_REDONE = "redone"
SIGNALS = (SIGNAL_KEPT, SIGNAL_REDONE)
#: Two generations of this many bytes. A row is ~90 bytes, so ~11k presses each.
MAX_BYTES = 1_000_000
LOCK_TIMEOUT_S = 2.0

_KEY_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_SURFACE_RE = re.compile(r"[a-z0-9_-]{1,32}")


def ledger_path() -> Path:
    return paths.state_path(LEDGER_FILENAME)


def record(key: str, signal: str, surface: str, *, now: float | None = None) -> dict:
    """Append one row and return it. Raises ``ValueError`` for a bad field and
    ``OSError`` when the write itself fails: the caller (a key press) reports
    it rather than pretending the signal was kept."""
    if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
        raise ValueError("key must match [A-Za-z0-9_.:-]{1,128}")
    if signal not in SIGNALS:
        raise ValueError(f"signal must be one of {SIGNALS}")
    if not isinstance(surface, str) or not _SURFACE_RE.fullmatch(surface):
        raise ValueError("surface must match [a-z0-9_-]{1,32}")
    row = {"ts": time.time() if now is None else float(now), "key": key,
           "signal": signal, "surface": surface}
    data = (json.dumps(row, separators=(",", ":")) + "\n").encode("utf-8")
    path = ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    from llm_router.file_lock import exclusive_lock

    lock_path = path.with_name(path.name + ".write.lock")
    if not lock_path.exists():  # file_lock opens "a+" at the umask default; make it 0600 first
        os.close(os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600))
    with exclusive_lock(lock_path, timeout=LOCK_TIMEOUT_S) as locked:
        if not locked:
            raise OSError(f"could not lock {path.name} within {LOCK_TIMEOUT_S}s; signal not recorded")
        capped_log.append(path, data, MAX_BYTES)
    return row


def read_rows(since: float | None = None, until: float | None = None) -> list[dict]:
    """Valid rows in ``[since, until]``, oldest first. A torn or foreign line is
    skipped, never read as a signal. Never raises."""
    out: list[dict] = []
    for row in capped_log.read_dicts(ledger_path()):
        ts = row.get("ts")
        if isinstance(ts, bool) or not isinstance(ts, (int, float)):
            continue
        if row.get("signal") not in SIGNALS or not isinstance(row.get("key"), str):
            continue
        if (since is not None and ts < since) or (until is not None and ts > until):
            continue
        out.append(row)
    out.sort(key=lambda r: r["ts"])
    return out


def latest_by_key(since: float | None = None, until: float | None = None) -> dict[str, dict]:
    """The last row per key (a later press replaces an earlier one)."""
    latest: dict[str, dict] = {}
    for row in read_rows(since, until):
        latest[row["key"]] = row
    return latest


def summarize(days: float, *, now: float | None = None) -> dict:
    """``{"kept": n, "redone": n, "presses": n, "newest_ts": ts|None}`` over the
    window, one verdict per key (the last). ``presses`` counts every row, repeats
    included."""
    now_ts = time.time() if now is None else now
    since = now_ts - days * 86400.0
    rows = read_rows(since, now_ts)
    latest: dict[str, dict] = {}
    for row in rows:
        latest[row["key"]] = row
    kept = sum(1 for r in latest.values() if r["signal"] == SIGNAL_KEPT)
    redone = sum(1 for r in latest.values() if r["signal"] == SIGNAL_REDONE)
    return {"kept": kept, "redone": redone, "presses": len(rows),
            "newest_ts": rows[-1]["ts"] if rows else None}
