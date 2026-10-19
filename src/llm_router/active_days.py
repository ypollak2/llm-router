"""Local retention marker (PLAN v16 P3.7, PRD S7, decision D-26 option A / D-251).

``active_days.jsonl`` under the llm-router state home records which LOCAL calendar days the
router was active: one row per day, ``{"day": "YYYY-MM-DD", "install_id": "<random hex>"}``.
The install id is generated locally on the first row and copied to later rows. No prompt text,
no session id, no path, no network: nothing leaves the machine, and nothing here is sent anywhere.

Written by :func:`mark_active`, called from ``quota_samples.append_session_sample`` (the session
start and Stop samples), so a session costs one ``stat`` of the file; the tail read and the append happen at most once
per day (a file whose mtime is today already holds today's row).

Bound: one row per day, at most :data:`MAX_ROWS` rows (about 13 months). Past that the file is
rewritten atomically keeping the FIRST row (it holds the install day and id) and the newest
``MAX_ROWS - 1``. Retention rule: docs/RETENTION.md.

The install date is the first marker day, not the operating-system install time: no earlier
dated record exists. A router that ran before this marker shipped reads its install day as the
first day the marker was written.

:func:`status` answers the install's own question, "was I active on day 14 after install?":
``yes`` / ``no`` / ``not yet`` (today is on or before day 14, so day 14 can still happen), or
``n/a`` when no marker exists. The reading is strict: active on day 15 but not on day 14 is ``no``.
"""
from __future__ import annotations

import json
import os
import secrets
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from llm_router import paths

NAME = "active_days.jsonl"
MAX_ROWS = 400
RETENTION_DAY = 14
_TAIL_BYTES = 4096
#: Size of one row: {"day":"YYYY-MM-DD","install_id":<16 hex>} plus newline is 53 bytes.
_ROW_BYTES = len(json.dumps({"day": "2000-01-01", "install_id": "0" * 16}, separators=(",", ":"))) + 1


def _path() -> Path:
    return paths.state_path(NAME)


def _today(now: float | None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(time.time() if now is None else now))


def _parse_row(line: str) -> dict | None:
    try:
        row = json.loads(line)
    except ValueError:
        return None
    if not isinstance(row, dict) or not isinstance(row.get("day"), str):
        return None
    try:
        date.fromisoformat(row["day"])
    except ValueError:
        return None
    return row


def _read_rows(path: Path) -> list[dict]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    rows = [r for r in (_parse_row(ln) for ln in text.splitlines() if ln.strip()) if r]
    return rows


def _last_row(path: Path) -> dict | None:
    """The last valid row, read from the tail only."""
    try:
        with open(path, "rb") as fh:
            size = fh.seek(0, os.SEEK_END)
            fh.seek(max(0, size - _TAIL_BYTES))
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    for ln in reversed(tail.splitlines()):
        row = _parse_row(ln) if ln.strip() else None
        if row:
            return row
    return None


def _new_install_id(path: Path) -> str:
    """The install id, created exactly once even when two processes start fresh together.
    The id is written to a private temp file and hard-linked to ``active_days.id``: the link is
    atomic and exclusive, so the file is never observed empty and the loser reads the winner's."""
    idp = path.with_name("active_days.id")
    fresh = secrets.token_hex(8)
    tmp = path.with_name(f"active_days.id.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        try:
            os.write(fd, fresh.encode("utf-8"))
        finally:
            os.close(fd)
        try:
            os.link(str(tmp), str(idp))
        except FileExistsError:
            try:
                got = idp.read_text(encoding="utf-8").strip()
            except OSError:
                got = ""
            return got if got else fresh
        return fresh
    finally:
        try:
            os.unlink(str(tmp))
        except OSError as exc:
            from llm_router import failopen

            failopen.record("P37-ACTIVE-DAY-TMP", exc)


def _append(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        if hasattr(os, "fchmod"):  # absent on Windows; tightens a file created with a wider mode
            try:
                os.fchmod(fd, 0o600)
            except OSError as exc:
                from llm_router import failopen

                failopen.record("P37-ACTIVE-DAY-CHMOD", exc)
        os.write(fd, (json.dumps(row, separators=(",", ":")) + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def _dedupe(rows: list[dict]) -> list[dict]:
    """First row per day, in order: two processes marking the same day leave one row."""
    seen: set[str] = set()
    out = []
    for r in rows:
        if r["day"] not in seen:
            seen.add(r["day"])
            out.append(r)
    return out


def _trim(path: Path) -> None:
    raw = _read_rows(path)
    rows = _dedupe(raw)
    if len(rows) == len(raw) and len(rows) <= MAX_ROWS:
        return
    keep = rows if len(rows) <= MAX_ROWS else [rows[0]] + rows[-(MAX_ROWS - 1):]
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in keep).encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _has_dup_tail(path: Path) -> bool:
    """True when the last two rows share a day (a concurrent writer raced the check)."""
    try:
        with open(path, "rb") as fh:
            size = fh.seek(0, os.SEEK_END)
            fh.seek(max(0, size - _TAIL_BYTES))
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return False
    rows = [r for r in (_parse_row(ln) for ln in tail.splitlines() if ln.strip()) if r]
    return len(rows) >= 2 and rows[-1]["day"] == rows[-2]["day"]


def mark_active(now: float | None = None) -> bool:
    """Record today (local date) as an active day. True when a row was appended.

    Never raises: the marker is telemetry and must never block a hook."""
    try:
        path = _path()
        today = _today(now)
        if now is None:  # the hooks' path; an injected clock is not the file's clock
            try:
                if _today(path.stat().st_mtime) == today:
                    return False  # written today: today's row is there (one stat, no read)
            except OSError:
                pass
        last = _last_row(path)
        if last and last["day"] >= today:
            return False
        install_id = last.get("install_id") if last else None
        if not isinstance(install_id, str) or not install_id:
            install_id = _new_install_id(path)
        _append(path, {"day": today, "install_id": install_id})
        # Trim past a full file (MAX_ROWS rows of _ROW_BYTES), or when a racing writer left
        # a same-day duplicate. Only reached once a day.
        if path.stat().st_size > MAX_ROWS * _ROW_BYTES or _has_dup_tail(path):
            _trim(path)
        return True
    except Exception:  # noqa: BLE001 - telemetry never blocks a hook
        try:
            from llm_router import failopen

            failopen.record("P37-ACTIVE-DAY", RuntimeError("active day not written"))
        except Exception:  # noqa: BLE001
            pass
        return False


def status(now: float | None = None) -> dict[str, Any]:
    """The install's own S7 status. Unknown is ``n/a``, never 0."""
    rows = _read_rows(_path())
    today = _today(now)
    if not rows:
        return {"instrumented": False, "install_day": None, "install_id": None,
                "active_days": None, "days_since_install": None, "day14": None,
                "active_on_day14": "n/a", "today": today}
    days = sorted({r["day"] for r in rows})
    install = date.fromisoformat(days[0])
    day14 = (install + timedelta(days=RETENTION_DAY)).isoformat()
    if day14 in days:
        verdict = "yes"
    elif today <= day14:
        verdict = "not yet"  # day 14 can still happen today
    else:
        verdict = "no"
    return {"instrumented": True, "install_day": days[0], "install_id": rows[0].get("install_id"),
            "active_days": len(days),
            "days_since_install": (date.fromisoformat(today) - install).days,
            "day14": day14, "active_on_day14": verdict, "today": today}


def render_status(st: dict[str, Any]) -> str:
    if not st["instrumented"]:
        return ("Retention (S7, local only): n/a (no active-day marker yet; it is written at the "
                "next session start)")
    return (f"Retention (S7, local only, nothing sent): install day {st['install_day']} "
            f"(first marker day), {st['active_days']} active day(s) of "
            f"{st['days_since_install'] + 1} since, day 14 = {st['day14']}, "
            f"active on day 14: {st['active_on_day14']}")
