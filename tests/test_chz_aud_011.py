"""Regression: CHZ-AUD-011 — parent-child cascade cancel race.

Children created concurrently while cancel(cascade=True) is walking the
descendant tree must NOT escape cancellation. A late child born after the
BFS snapshot but before the parent commit previously stayed 'active',
leaving orphaned spenders after an emergency stop.
"""
from __future__ import annotations

import threading

from llm_router.agents.base import SessionState
from llm_router.agents.session import SessionStore


def _make_store(tmp_path):
    return SessionStore(
        db_path=tmp_path / "sessions.db", check_same_thread=False
    )


def test_cascade_cancel_no_child_escapes_under_concurrent_create(tmp_path):
    store = _make_store(tmp_path)
    parent = store.create(agent_id="parent", budget_usd=10.0)

    n_children = 50
    barrier = threading.Barrier(2)
    created_ids: list[str] = []
    create_lock = threading.Lock()

    def spawn_children() -> None:
        barrier.wait()
        for _ in range(n_children):
            try:
                child = store.create(
                    agent_id="child",
                    budget_usd=1.0,
                    parent_session_id=parent.session_id,
                )
            except Exception:
                # A born-cancelled refusal is an acceptable outcome; the
                # invariant is only that no child ends up 'active'.
                continue
            with create_lock:
                created_ids.append(child.session_id)

    def cancel_parent() -> None:
        barrier.wait()
        store.cancel(parent.session_id, cascade=True)

    t_spawn = threading.Thread(target=spawn_children)
    t_cancel = threading.Thread(target=cancel_parent)
    t_spawn.start()
    t_cancel.start()
    t_spawn.join()
    t_cancel.join()

    # Parent must be cancelled.
    assert store.get(parent.session_id).state == SessionState.CANCELLED

    # No surviving child may be active. Re-fetch each created child.
    escaped = [
        cid
        for cid in created_ids
        if store.get(cid).state == SessionState.ACTIVE
    ]
    assert not escaped, (
        f"{len(escaped)}/{len(created_ids)} children escaped cancellation "
        f"and remain active: {escaped[:5]}"
    )

    store.close()


def test_child_created_after_parent_cancelled_is_born_terminal(tmp_path):
    """Deterministic (non-racy) case: once the parent is CANCELLED, any new
    child created under it must not be born ACTIVE."""
    store = _make_store(tmp_path)
    parent = store.create(agent_id="parent", budget_usd=10.0)
    store.cancel(parent.session_id, cascade=True)

    child = store.create(
        agent_id="child",
        budget_usd=1.0,
        parent_session_id=parent.session_id,
    )
    assert store.get(child.session_id).state != SessionState.ACTIVE
    store.close()
