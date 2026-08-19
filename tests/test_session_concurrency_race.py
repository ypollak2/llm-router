"""Regression: CHZ-AUD-003 / CHZ-AUD-004 — SessionStore.record_step races.

Audit findings (commit 174941677a88, v0.8.7), src/llm_router/agents/session.py:

  CHZ-AUD-003 (critical): record_step is a read-modify-write (get() then a
    separate UPDATE) with no BEGIN IMMEDIATE / WAL / busy_timeout / lock.
    Under concurrency it silently loses updates — the audit measured up to
    96.5% of budget/step increments dropped with ZERO exceptions raised.

  CHZ-AUD-004 (critical): _UPDATE_STEP writes state='active' unconditionally,
    so a record_step that read ACTIVE before a concurrent cancel() commit
    overwrites 'cancelled' back to 'active' — the operator emergency stop is
    silently defeated.

These are marked xfail until fixed: the assertions describe CORRECT behavior
and must NOT be weakened. When the fix (atomic UPDATE ... SET x = x + ? WHERE
state NOT IN (terminal); rows_affected check; WAL + busy_timeout) lands, drop
the xfail marker so these become hard gates.
"""

from __future__ import annotations

import threading

import pytest

from llm_router.agents.session import SessionStore, TerminalStateViolation


def _store(tmp_path):
    # check_same_thread=False mirrors the admin-API sharing pattern that makes
    # the Python-level read-modify-write race reachable across threads.
    return SessionStore(db_path=tmp_path / "sessions.db", check_same_thread=False)


def test_record_step_no_lost_updates_under_concurrency(tmp_path):
    store = _store(tmp_path)
    sess = store.create(agent_id="race-agent", budget_usd=100.0)
    sid = sess.session_id

    N_THREADS, PER_THREAD = 50, 4
    barrier = threading.Barrier(N_THREADS)

    def worker():
        barrier.wait()
        for _ in range(PER_THREAD):
            try:
                store.record_step(sid, 0.001)
            except Exception:
                pass  # count via the DB, not the return

    threads = [threading.Thread(target=worker) for _ in range(N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    final = store.get(sid)
    expected = N_THREADS * PER_THREAD
    assert final.step_count == expected, (
        f"lost updates: step_count={final.step_count}, expected {expected}"
    )
    assert final.consumed_usd == pytest.approx(expected * 0.001, abs=1e-9), (
        f"lost cost: consumed_usd={final.consumed_usd}, expected {expected * 0.001}"
    )


def test_cancel_is_not_overwritten_by_concurrent_record_step(tmp_path):
    store = _store(tmp_path)
    sess = store.create(agent_id="cancel-agent", budget_usd=100.0)
    sid = sess.session_id

    stop = threading.Event()
    post_cancel_successes = {"n": 0}
    cancelled = threading.Event()

    def stepper():
        while not stop.is_set():
            try:
                store.record_step(sid, 0.0001)
                if cancelled.is_set():
                    post_cancel_successes["n"] += 1
            except TerminalStateViolation:
                stop.set()
                return
            except Exception:
                pass

    t = threading.Thread(target=stepper)
    t.start()
    # let it spin, then emergency-stop
    threading.Event().wait(0.01)
    store.cancel(sid, reason="emergency stop")
    cancelled.set()
    threading.Event().wait(0.02)
    stop.set()
    t.join(timeout=5)

    final = store.get(sid)
    assert final.state == "cancelled", (
        f"emergency stop defeated: final state={final.state!r}, expected 'cancelled'"
    )
    assert post_cancel_successes["n"] == 0, (
        f"{post_cancel_successes['n']} record_step calls succeeded after cancel()"
    )
