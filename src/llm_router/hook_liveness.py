"""Was the hook KILLED, or did it choose not to route? R9.

A UserPromptSubmit hook that exceeds its timeout is killed by the host. What
the user sees is a turn that Claude answered directly — which is exactly what
they see when the hook ran fine and decided not to route. The two are
observably identical, and the difference is the whole question of whether
routing is working.

Measured during the audit: a 60s timeout, a maximum observed hook duration of
55.3s, and 5.5% of real invocations reaching no terminal outcome. Nothing
anywhere distinguished "killed at 60s" from "declined in 40ms".

The mechanism is the smallest one that can tell them apart: write a marker
before the expensive work, remove it on the way out. A process that exits — for
ANY reason it controls, including `sys.exit` and an unhandled exception — clears
its own marker. A process that is killed cannot, so its marker survives, and
the NEXT invocation finds it.

WHY A FILE AND NOT A COUNTER. The condition being detected is "this process
stopped executing", so nothing in that process can record it. The evidence has
to already be on disk before the kill, which means the mechanism is a marker
that outlives the writer rather than a message the writer sends.

KNOWN LIMIT, stated rather than discovered later: PID reuse. A marker is judged
an orphan when its pid is no longer alive AND it is older than the hook's
wall-clock budget. If the OS has recycled that pid onto a live process in the
meantime, the marker is left alone and the kill goes uncounted. That
undercounts; it never invents a kill that did not happen, which is the correct
direction for a number an operator will act on.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

__all__ = [
    "marker_path",
    "mark_started",
    "clear_marker",
    "reap_orphans",
    "orphan_count",
    "MARKER_DIR",
]

MARKER_DIR = "hook_running"

#: A marker younger than this is assumed to belong to a hook that is still
#: working. Sized above the hook's own budget (60s) so a slow-but-alive
#: invocation is never reported as a kill — undercounting is the safe
#: direction, and a kill that is one invocation late to be noticed is still
#: noticed.
_STALE_AFTER_SECONDS = 90.0


def _dir() -> Path:
    from llm_router.paths import state_path

    return state_path(MARKER_DIR)


def marker_path(pid: int | None = None) -> Path:
    return _dir() / f"{pid or os.getpid()}.json"


def mark_started(invocation_id: float | None = None) -> None:
    """Record that this process began the expensive part. Never raises.

    Called before the work, not after: a marker written after the thing it is
    meant to survive would only ever exist for invocations that completed.
    """
    try:
        p = marker_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "pid": os.getpid(),
            "started_at": time.time(),
            "invocation_id": invocation_id,
        }
        # 0600 at creation: the marker carries a pid and a timestamp, which is
        # not a secret, but a world-writable marker directory lets any local
        # process forge or delete kill evidence.
        from llm_router.paths import private_opener

        with open(p, "w", opener=private_opener) as fh:
            fh.write(json.dumps(payload))
    except Exception:  # noqa: BLE001 — a hook must never break on telemetry
        from llm_router import failopen

        failopen.record("CHZ-FO-HOOK-MARK-START")


def clear_marker() -> None:
    """This process is exiting under its own control. Never raises."""
    try:
        # `missing_ok`, not `except FileNotFoundError: pass`. An absent marker
        # is the NORMAL outcome when `reap_orphans` in a later process got
        # there first, so it is not an error to swallow — and written as a bare
        # handler it was indistinguishable to the T-14 census from the silent
        # persistence failures this repo has spent the audit removing.
        marker_path().unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        from llm_router import failopen

        # Fail-open but counted: a marker we cannot remove becomes an orphan,
        # and an orphan is reported as a KILL. Left silent, a permissions
        # problem would manufacture kills that never happened.
        failopen.record("CHZ-FO-HOOK-CLEAR-MARKER")


def _pid_alive(pid: int) -> bool:
    """Is a process with this pid running?

    `os.kill(pid, 0)` raises ProcessLookupError when it is not, and
    PermissionError when it exists but belongs to someone else — which is still
    ALIVE, and returning False there would report another user's process as a
    kill of ours.
    """
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:  # noqa: BLE001
        # Unknown means we cannot prove it is dead. Undercount rather than
        # invent a kill.
        return True


def reap_orphans() -> int:
    """Remove markers left by killed hooks, counting each. Returns the count.

    Each orphan is recorded as `CHZ-HOOK-KILLED` so it reaches the counter
    registry, and the marker is deleted so the same kill is not counted again
    on every subsequent invocation — a counter that re-counts the same event
    forever tells an operator that the problem is getting worse when it is not.
    """
    killed = 0
    try:
        d = _dir()
        if not d.exists():
            return 0
        now = time.time()
        mine = os.getpid()
        for marker in d.glob("*.json"):
            try:
                pid = int(marker.stem)
            except ValueError:
                marker.unlink(missing_ok=True)
                continue
            if pid == mine:
                continue
            try:
                started = float(json.loads(marker.read_text()).get("started_at", 0.0))
            except Exception:  # noqa: BLE001
                started = marker.stat().st_mtime
            if now - started < _STALE_AFTER_SECONDS:
                continue  # still plausibly working
            if _pid_alive(pid):
                continue  # see the PID-reuse note in the module docstring
            marker.unlink(missing_ok=True)
            killed += 1
    except Exception:  # noqa: BLE001
        from llm_router import failopen

        failopen.record("CHZ-FO-HOOK-REAP")
        return killed
    if killed:
        from llm_router import failopen

        for _ in range(killed):
            failopen.record(
                "CHZ-HOOK-KILLED",
                detail="hook exceeded its budget and was killed mid-invocation",
            )
    return killed


def orphan_count() -> int:
    """Markers currently on disk that look like kills. Does NOT reap.

    A read-only view for `doctor`, so asking the question does not change the
    answer — a reader with a side effect is how "run doctor twice and the
    number changes" happens.
    """
    try:
        d = _dir()
        if not d.exists():
            return 0
        now = time.time()
        mine = os.getpid()
        n = 0
        for marker in d.glob("*.json"):
            try:
                pid = int(marker.stem)
            except ValueError:
                continue
            if pid == mine:
                continue
            try:
                started = float(json.loads(marker.read_text()).get("started_at", 0.0))
            except Exception:  # noqa: BLE001
                started = marker.stat().st_mtime
            if now - started >= _STALE_AFTER_SECONDS and not _pid_alive(pid):
                n += 1
        return n
    except Exception:  # noqa: BLE001
        return 0
