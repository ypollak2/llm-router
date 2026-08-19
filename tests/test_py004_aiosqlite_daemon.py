"""Regression: CHZ-PY-004 — aiosqlite worker threads hung the interpreter at exit.

aiosqlite's connection-worker thread is non-daemon by default. A connection
dropped at event-loop shutdown left the worker alive, keeping the interpreter
running until an external SIGKILL. The fix (`mark_worker_daemon`) marks the
worker daemon BEFORE the connection is awaited (the thread hasn't started yet;
marking after start is a no-op). It was originally applied at one call site; it
now covers every `aiosqlite.connect()` site via one shared helper.

Two checks:
  1. mechanism — marking before await yields a daemon worker;
  2. no-hang — a subprocess that opens+drops a connection with the fixed
     pattern exits promptly instead of hanging until killed.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap



def test_mark_before_await_sets_daemon():
    import asyncio
    import os
    import tempfile
    import threading

    import aiosqlite

    from llm_router.aiosqlite_util import mark_worker_daemon

    db_path = os.path.join(tempfile.mkdtemp(), "t.db")

    async def _run():
        conn = aiosqlite.connect(db_path)  # not awaited → worker not started yet
        mark_worker_daemon(conn)
        db = await conn
        worker = getattr(db, "_thread", db)
        daemon = worker.daemon if isinstance(worker, threading.Thread) else None
        await db.close()
        return daemon

    assert asyncio.run(_run()) is True, "worker not daemon-marked before await"


def test_dropped_connection_does_not_hang_exit():
    """A connection opened with the fixed pattern and dropped must not hang exit."""
    prog = textwrap.dedent(
        """
        import asyncio, tempfile, os, aiosqlite
        from llm_router.aiosqlite_util import mark_worker_daemon
        db_path = os.path.join(tempfile.mkdtemp(), "t.db")
        async def main():
            conn = aiosqlite.connect(db_path)
            mark_worker_daemon(conn)
            db = await conn
            await db.execute("PRAGMA journal_mode=WAL")
            # DROP it: no close(), simulating a task cancelled at loop shutdown.
        asyncio.run(main())
        print("EXITED_CLEANLY")
        """
    )
    # If the worker were non-daemon, this process would hang past the timeout.
    proc = subprocess.run(
        [sys.executable, "-c", prog],
        capture_output=True, text=True, timeout=15,
    )
    assert "EXITED_CLEANLY" in proc.stdout
    assert proc.returncode == 0, f"process did not exit cleanly: rc={proc.returncode}"
