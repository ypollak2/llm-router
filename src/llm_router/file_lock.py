"""Cross-platform advisory exclusive file lock.

A tiny, dependency-free lock backed by a sibling ``.lock`` file (POSIX
``fcntl.flock`` / Windows ``msvcrt.locking``). Introduced to fix
CHZ-AUD-C-01: ``session_store.record_event()``'s append-then-maybe-compact
critical section had no cross-process coordination, so a concurrent
``os.replace()`` compaction could silently orphan another process's
just-appended write (22/1200 = 1.83% loss observed under 6-process load).

Locking the JSONL file itself would interact awkwardly with the
temp-file-then-``os.replace()`` swap used by compaction (the replaced file
would carry a stale lock state), so callers lock a *sibling* file instead —
the data file's inode can be freely swapped while the lock file's identity
stays stable for the duration of the critical section.
"""

from __future__ import annotations

import contextlib
import sys
import time
from pathlib import Path
from typing import Iterator

_IS_WINDOWS = sys.platform.startswith("win")

if not _IS_WINDOWS:  # pragma: no cover - platform-specific import
    import fcntl
else:  # pragma: no cover - platform-specific import
    import msvcrt

_DEFAULT_TIMEOUT_SECONDS = 30.0
_POLL_INTERVAL_SECONDS = 0.02


@contextlib.contextmanager
def exclusive_lock(
    lock_path: Path, timeout: float = _DEFAULT_TIMEOUT_SECONDS
) -> Iterator[bool]:
    """Hold an exclusive advisory lock on *lock_path* for the block body.

    Blocks (polling, bounded by *timeout*) until the lock is acquired, and
    always releases it on exit (including exceptions raised inside the
    ``with`` body). Yields ``True`` if the lock was actually acquired,
    ``False`` if acquisition timed out — callers that need to fail rather
    than silently proceed unlocked should check the yielded value; callers
    that only want best-effort serialization (mirroring this module's
    fail-open philosophy elsewhere) can ignore it.

    Never raises for lock-acquisition failures (permissions, missing
    platform module, etc.) — degrades to "unlocked" so a locking problem can
    never turn into a hard outage for a caller that was previously unlocked
    entirely.
    """
    locked = False
    fh = None
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(lock_path, "a+")
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                if _IS_WINDOWS:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(_POLL_INTERVAL_SECONDS)
            except Exception:
                # Locking primitive unavailable/unsupported: degrade to
                # unlocked rather than raising.
                break
    except Exception:
        locked = False
    try:
        yield locked
    finally:
        if fh is not None:
            if locked:
                try:
                    if _IS_WINDOWS:
                        fh.seek(0)
                        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
            try:
                fh.close()
            except Exception:
                pass
