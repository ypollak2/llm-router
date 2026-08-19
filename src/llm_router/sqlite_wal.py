"""RED5-01/02 (P0) — surviving the cold start of a shared SQLite database.

Every SQLite store in this codebase opens with the same two lines:

    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")

and every one of them was wrong in the same two ways.

**1. Switching to WAL needs an exclusive lock.** It only has to happen once —
``journal_mode`` lives in the database header and persists — but that once is the
*cold start*, when a fresh install has several hooks firing at the same instant
against a database that does not exist yet. Whichever process loses the race
gets ``database is locked`` from the PRAGMA itself. Unguarded, that propagates
out of the constructor and takes the caller with it. Measured at N=12 concurrent
cold starts: 4 of 12 ``LineageStore`` constructions raised.

**2. The PRAGMA reports failure by returning, not by raising.**
``PRAGMA journal_mode=WAL`` answers with the mode actually in effect. Lose the
race in a way SQLite considers non-exceptional and you get ``"delete"`` back and
no error at all — so the connection proceeds in rollback-journal mode, where a
writer blocks every reader, and the *next* operation is the one that fails. That
is how 66 events went missing across 2400 concurrent writes with nothing in any
log: the failure was reported to a caller who was not reading it.

Both are fixed by the same small function, and neither is fixed by a longer
``busy_timeout`` alone — a timeout helps the case that raises and does nothing
for the case that quietly returns the wrong mode.

Falling back to rollback-journal mode is acceptable and is NOT an error: the
database still works correctly, just with less concurrency. Failing to *notice*
is the bug, which is why this returns a value the caller can act on and logs at
warning level when WAL could not be established.
"""

from __future__ import annotations

import logging
import sqlite3
import time

logger = logging.getLogger("llm_router.sqlite")

#: Long enough for a writer to drain under pathological CI-runner load. Applied
#: per connection — unlike journal_mode, it is not persisted in the header.
DEFAULT_BUSY_TIMEOUT_MS = 30_000

#: Cold-start contention is measured in milliseconds, so a handful of quick
#: retries covers it. This is not a substitute for busy_timeout; it exists for
#: the case where the PRAGMA returns the wrong mode rather than blocking.
_ATTEMPTS = 6
_BACKOFF_BASE_S = 0.02


def _is_contention(exc: sqlite3.Error) -> bool:
    text = str(exc).lower()
    return "locked" in text or "busy" in text


def enable_wal(
    conn: sqlite3.Connection,
    *,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    label: str = "sqlite",
) -> bool:
    """Put ``conn`` into WAL mode, tolerating a contended cold start.

    Returns True when WAL is in effect. Returns False when it is not — the
    connection is still usable, in rollback-journal mode, and the caller has been
    told rather than left to find out at the next write.

    ``busy_timeout`` is set FIRST and deliberately: it governs how long the
    journal_mode PRAGMA itself will wait for the exclusive lock, so setting it
    afterwards (as two of the three original call sites did) leaves the one
    statement that most needs it running with the 5-second default.
    """
    try:
        conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    except sqlite3.Error as exc:  # pragma: no cover — a connection this broken
        logger.warning("%s: could not set busy_timeout: %s", label, exc)

    last_mode: str | None = None
    for attempt in range(_ATTEMPTS):
        try:
            row = conn.execute("PRAGMA journal_mode = WAL").fetchone()
            last_mode = str(row[0]).lower() if row else None
            if last_mode == "wal":
                return True
        except sqlite3.Error as exc:
            if not _is_contention(exc):
                # A real error (corrupt file, readonly mount) is not ours to
                # swallow — swallowing it is how a broken database looks healthy.
                raise
            last_mode = f"error: {exc}"
        if attempt < _ATTEMPTS - 1:
            time.sleep(_BACKOFF_BASE_S * (2**attempt))

    logger.warning(
        "%s: could not enable WAL after %d attempts (mode=%s). Continuing in "
        "rollback-journal mode: correct, but writers will block readers.",
        label,
        _ATTEMPTS,
        last_mode,
    )
    return False
