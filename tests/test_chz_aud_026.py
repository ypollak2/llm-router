"""Regression test for CHZ-AUD-026.

Full test suite hangs at exit on leaked non-daemon aiosqlite worker threads.

aiosqlite's ``_connection_worker_thread`` is a non-daemon thread by default.
If a task holding a connection is dropped at event-loop shutdown (its
``finally: await db.close()`` never runs), a non-daemon worker keeps the
interpreter alive forever — the hang-at-exit bug.

The fix in ``cost.py`` marks the worker daemon *before* awaiting the
connection so a leaked worker can never block process exit.
"""
from __future__ import annotations

import asyncio
import threading

from llm_router import cost


def _worker_threads() -> list[threading.Thread]:
    return [
        t
        for t in threading.enumerate()
        if "_connection_worker_thread" in t.name
    ]


def test_get_db_worker_is_daemon(tmp_path, monkeypatch):
    """The connection returned by _get_db must run a daemon worker thread."""
    db_path = tmp_path / "cost.db"

    cfg = cost.get_config()
    monkeypatch.setattr(cfg, "llm_router_db_path", db_path, raising=False)

    async def _run() -> bool:
        db = await cost._get_db()
        try:
            # Every live worker thread must be a daemon so it can never
            # block interpreter exit.
            workers = _worker_threads()
            assert workers, "expected at least one aiosqlite worker thread"
            return all(t.daemon for t in workers)
        finally:
            await db.close()

    assert asyncio.run(_run()) is True


def test_no_nondaemon_worker_leaks_after_logging_cycle(tmp_path, monkeypatch):
    """After a full open/close cost-logging cycle, no non-daemon worker lingers.

    Reproduces the leak scenario: a connection whose close() never runs must
    still leave only daemon workers behind (which cannot block exit).
    """
    db_path = tmp_path / "cost.db"
    cfg = cost.get_config()
    monkeypatch.setattr(cfg, "llm_router_db_path", db_path, raising=False)

    async def _leak() -> None:
        # Deliberately open without closing to emulate a dropped task at
        # loop shutdown (the finally: await db.close() that never fires).
        await cost._get_db()

    asyncio.run(_leak())

    # Any worker still alive from the leaked connection must be a daemon.
    nondaemon = [t for t in _worker_threads() if t.is_alive() and not t.daemon]
    assert not nondaemon, (
        f"non-daemon aiosqlite worker(s) leaked and would hang exit: {nondaemon}"
    )


def test_mark_worker_daemon_does_not_stamp_junk_attr():
    """Regression: the daemon-marker must not silently mask a missing worker.

    The old ``getattr(conn, "_thread", conn)`` fallback set ``daemon`` on the
    Connection object itself when ``_thread`` was absent — a no-op that left
    the real worker non-daemon while looking successful. The hardened helper
    only touches a genuine Thread.
    """
    class _FakeConn:
        pass

    fake = _FakeConn()
    cost._mark_worker_daemon(fake)  # must not raise, must not create attr
    assert not hasattr(fake, "daemon"), (
        "daemon must not be stamped on a non-Thread connection"
    )
