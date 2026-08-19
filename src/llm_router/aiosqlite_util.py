"""Shared aiosqlite worker-thread daemon marking (CHZ-PY-004 / CHZ-AUD-026).

aiosqlite's ``_connection_worker_thread`` is non-daemon by default. If a task
holding a connection is dropped at event-loop shutdown (its ``finally: await
db.close()`` never runs), a non-daemon worker keeps the interpreter alive
forever — the hang-at-exit bug. The fix was originally applied at a single
call site in ``cost.py``; this module makes it the single shared implementation
so every ``aiosqlite.connect()`` site can mark its worker without duplicating
the logic (and without pulling in ``cost.py``'s heavy imports).
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite


def mark_worker_daemon(conn: "aiosqlite.Connection") -> None:
    """Best-effort mark the aiosqlite worker thread as a daemon.

    aiosqlite >=0.22 keeps the worker in a private ``_thread``; on older
    releases the Connection itself was a ``threading.Thread``. We only touch an
    object that is genuinely a Thread, so an unexpected layout is ignored rather
    than stamping a junk ``daemon`` attribute. Setting ``daemon`` on an
    already-started thread raises RuntimeError; that is fine — a started worker
    was already daemon-marked pre-await.
    """
    worker = getattr(conn, "_thread", conn)
    if not isinstance(worker, threading.Thread):
        return
    try:
        worker.daemon = True
    except RuntimeError:
        # Thread already started — it was daemon-marked before it started.
        pass
