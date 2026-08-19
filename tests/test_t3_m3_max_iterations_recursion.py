"""T3-M3 (Track-3 agent safety, Medium): iterations + recursion guards.

Per-session caps on agent runaway:

* ``max_iterations`` — hard cap on ``step_count``. The very next
  ``record_step`` past the cap raises ``IterationsExceeded`` and
  transitions the session to ``BUDGET_EXCEEDED`` (terminal). Checked
  BEFORE the budget breach so a 1-cent loop stops at the cheapest
  point.
* ``max_recursion_depth`` — hard cap on the parent chain. Checked at
  child-create time by walking the parent chain; the new child must
  sit at a depth less than the parent's cap.

See: Docs/audit/post-remediation/GAP_ANALYSIS.md G-008.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from llm_router.agents.base import SessionState
from llm_router.agents.budget import (
    IterationsExceeded,
    RecursionDepthExceeded,
)
from llm_router.agents.session import (
    SessionStore,
    TerminalStateViolation,
)


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(db_path=tmp_path / "s.db")


# ── 1. max_iterations ────────────────────────────────────────────────────────


def test_create_rejects_non_positive_max_iterations(store: SessionStore) -> None:
    with pytest.raises(ValueError, match="max_iterations must be positive"):
        store.create(agent_id="a", budget_usd=1.0, max_iterations=0)
    with pytest.raises(ValueError, match="max_iterations must be positive"):
        store.create(agent_id="a", budget_usd=1.0, max_iterations=-1)


def test_no_cap_is_uncapped(store: SessionStore) -> None:
    """No max_iterations → unlimited. Pin the baseline so a future
    refactor doesn't silently introduce a default cap."""
    sess = store.create(agent_id="a", budget_usd=100.0)
    for _ in range(20):
        store.record_step(sess.session_id, 0.0001)
    assert store.get(sess.session_id).step_count == 20


def test_iterations_cap_halts_at_threshold(store: SessionStore) -> None:
    """A 1000-iteration synthetic loop halts exactly at the cap."""
    sess = store.create(agent_id="a", budget_usd=100.0, max_iterations=5)

    for _ in range(5):
        store.record_step(sess.session_id, 0.0001)

    # The 6th attempt must raise.
    with pytest.raises(IterationsExceeded) as excinfo:
        store.record_step(sess.session_id, 0.0001)
    assert excinfo.value.max_iterations == 5
    assert excinfo.value.current_step_count == 5

    # Session is now terminal.
    s = store.get(sess.session_id)
    assert s.state == SessionState.BUDGET_EXCEEDED
    assert s.step_count == 5  # final step NOT recorded — the cap stops first


def test_subsequent_step_on_iter_capped_session_raises_terminal(
    store: SessionStore,
) -> None:
    """After IterationsExceeded transitions the session to terminal,
    further record_step calls hit the terminal-state guard."""
    sess = store.create(agent_id="a", budget_usd=100.0, max_iterations=2)
    store.record_step(sess.session_id, 0.0001)
    store.record_step(sess.session_id, 0.0001)
    with pytest.raises(IterationsExceeded):
        store.record_step(sess.session_id, 0.0001)
    with pytest.raises(TerminalStateViolation):
        store.record_step(sess.session_id, 0.0001)


def test_iterations_check_precedes_budget_check(store: SessionStore) -> None:
    """If both caps would be breached, the iteration cap wins (cheaper
    failure mode). The budget cap is generous; the iteration cap is
    tight; the call that hits both must raise IterationsExceeded, not
    BudgetExceeded."""
    sess = store.create(
        agent_id="a",
        budget_usd=0.01,  # tight: 100x 0.0001 → 0.01 = breach on 101st
        max_iterations=3,  # tighter
    )
    store.record_step(sess.session_id, 0.0001)
    store.record_step(sess.session_id, 0.0001)
    store.record_step(sess.session_id, 0.0001)
    with pytest.raises(IterationsExceeded):
        store.record_step(sess.session_id, 0.0001)


def test_iterations_exceeded_carries_context(store: SessionStore) -> None:
    sess = store.create(agent_id="a", budget_usd=10.0, max_iterations=3)
    for _ in range(3):
        store.record_step(sess.session_id, 0.001)
    with pytest.raises(IterationsExceeded) as excinfo:
        store.record_step(sess.session_id, 0.001)
    assert excinfo.value.session_id == sess.session_id
    assert "max_iterations=3" in str(excinfo.value)
    assert "current step" in str(excinfo.value)


# ── 2. max_recursion_depth ───────────────────────────────────────────────────


def test_create_rejects_non_positive_max_recursion_depth(
    store: SessionStore,
) -> None:
    with pytest.raises(ValueError, match="max_recursion_depth must be positive"):
        store.create(agent_id="a", budget_usd=1.0, max_recursion_depth=0)


def test_no_recursion_cap_is_uncapped(store: SessionStore) -> None:
    """Without a cap, arbitrarily-deep parent chains succeed."""
    parent = store.create(agent_id="a", budget_usd=10.0)
    current = parent
    for _ in range(20):
        current = store.create(
            agent_id="a",
            budget_usd=10.0,
            parent_session_id=current.session_id,
        )
    # Final session is depth-20 from the root.
    assert current.parent_session_id is not None


def test_recursion_depth_2_blocks_grandchild(store: SessionStore) -> None:
    """Parent cap=2 means: parent (depth 0) + 1 child (depth 1) ok,
    grandchild (depth 2) must raise."""
    parent = store.create(agent_id="p", budget_usd=10.0, max_recursion_depth=2)
    child = store.create(
        agent_id="c", budget_usd=10.0, parent_session_id=parent.session_id
    )
    with pytest.raises(RecursionDepthExceeded) as excinfo:
        store.create(
            agent_id="g",
            budget_usd=10.0,
            parent_session_id=child.session_id,
        )
    assert excinfo.value.max_recursion_depth == 2
    assert excinfo.value.current_depth == 2


def test_recursion_depth_1_blocks_first_child(store: SessionStore) -> None:
    """Parent cap=1 → no children allowed at all."""
    parent = store.create(agent_id="p", budget_usd=10.0, max_recursion_depth=1)
    with pytest.raises(RecursionDepthExceeded):
        store.create(
            agent_id="c",
            budget_usd=10.0,
            parent_session_id=parent.session_id,
        )


def test_recursion_depth_inherited_by_intermediate_ancestor(
    store: SessionStore,
) -> None:
    """An ancestor's cap applies even when intermediate ancestors don't
    have one. Build a chain parent → mid → and require parent's cap=2
    to halt the third descendant."""
    parent = store.create(agent_id="p", budget_usd=10.0, max_recursion_depth=2)
    mid = store.create(
        agent_id="m", budget_usd=10.0, parent_session_id=parent.session_id
    )
    # mid is depth 1; a grandchild of parent at depth 2 must fail.
    with pytest.raises(RecursionDepthExceeded):
        store.create(
            agent_id="grand",
            budget_usd=10.0,
            parent_session_id=mid.session_id,
        )


def test_recursion_depth_stale_parent_does_not_raise(
    store: SessionStore,
) -> None:
    """A parent_session_id that doesn't exist in the DB should not
    cause RecursionDepthExceeded — the walker treats missing
    ancestors as 'no enforcement available'. The child is created
    normally."""
    child = store.create(
        agent_id="orphan",
        budget_usd=10.0,
        parent_session_id="does-not-exist",
    )
    assert child.parent_session_id == "does-not-exist"


# ── 3. Schema migration ──────────────────────────────────────────────────────


def test_pre_t3_m3_schema_gets_migrated(tmp_path: Path) -> None:
    """A SessionStore opened against a DB that lacks max_iterations /
    max_recursion_depth columns must ALTER TABLE them in. After the
    migration, both old rows (NULL values) and new rows coexist."""
    db_path = tmp_path / "legacy.db"
    # Build the pre-T3-M3 schema by hand (no max_* columns).
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL,
            started_at REAL NOT NULL,
            completed_at REAL,
            parent_session_id TEXT,
            budget_cap_usd REAL NOT NULL,
            consumed_usd REAL NOT NULL DEFAULT 0.0,
            step_count INTEGER NOT NULL DEFAULT 0,
            state TEXT NOT NULL,
            framework TEXT
        )"""
    )
    # Insert a legacy row.
    conn.execute(
        "INSERT INTO sessions (session_id, agent_id, started_at, completed_at, "
        "parent_session_id, budget_cap_usd, consumed_usd, step_count, state, "
        "framework) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("legacy-1", "a", 1.0, None, None, 5.0, 0.0, 0, "active", None),
    )
    conn.commit()
    conn.close()

    # Open via SessionStore — migration must run.
    store = SessionStore(db_path=db_path)
    cols = {
        row[1]
        for row in store._conn.execute("PRAGMA table_info(sessions)").fetchall()
    }
    assert "max_iterations" in cols
    assert "max_recursion_depth" in cols

    # Legacy session still readable; new columns default to None.
    s = store.get("legacy-1")
    assert s.max_iterations is None
    assert s.max_recursion_depth is None


# ── 4. Backwards compat: callers that don't pass the caps ────────────────────


def test_existing_callers_unmodified(store: SessionStore) -> None:
    """Pre-T3-M3 calls to create() / record_step() must continue to
    work without passing the new params."""
    sess = store.create(agent_id="a", budget_usd=1.0)
    s = store.record_step(sess.session_id, 0.001)
    assert s.step_count == 1
    assert s.consumed_usd == pytest.approx(0.001)
    assert s.max_iterations is None
    assert s.max_recursion_depth is None
