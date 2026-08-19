"""SQLite-backed session store at ~/.llm-router/sessions.db.

One row per agent session. The session table tracks the lifecycle state
machine; per-step lineage lives in `~/.llm-router/lineage.db` linked by
session_id.

Session state transitions:
    create() → ACTIVE
    record_step() → ACTIVE (or BUDGET_EXCEEDED if cap reached)
    complete() → COMPLETED
    error()    → ERRORED
    Terminal states refuse further mutations.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from types import MappingProxyType

from llm_router.agents.base import AgentRoutingPolicy, AgentSession, SessionState
from llm_router.agents.budget import (
    BudgetEnvelope,
    BudgetExceeded,
    IterationsExceeded,
    RecursionDepthExceeded,
)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    started_at REAL NOT NULL,
    completed_at REAL,
    parent_session_id TEXT,
    budget_cap_usd REAL NOT NULL,
    consumed_usd REAL NOT NULL DEFAULT 0.0,
    step_count INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL,
    framework TEXT,
    max_iterations INTEGER,
    max_recursion_depth INTEGER,
    routing_policy_json TEXT,
    tool_call_count INTEGER NOT NULL DEFAULT 0,
    max_tool_calls INTEGER,
    max_children_concurrent INTEGER,
    last_activity_at REAL
);
CREATE INDEX IF NOT EXISTS idx_sessions_agent ON sessions(agent_id);
CREATE INDEX IF NOT EXISTS idx_sessions_parent ON sessions(parent_session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_state ON sessions(state);
"""

# Refinement #4: composite index serving both the state-only ledger
# query and the (state, last_activity_at) "stuck" filter via the
# leftmost-prefix rule. Created AFTER the column migration so it lands
# on legacy DBs whose last_activity_at column is added at open time.
_INDEX_STATE_ACTIVITY = (
    "CREATE INDEX IF NOT EXISTS idx_sessions_state_activity "
    "ON sessions(state, last_activity_at)"
)

# T3-M3 + T3-XL1 + G-029: idempotent ALTER TABLE ADD COLUMN migration for
# sessions DBs that pre-date the runaway-guard, routing-policy, or ledger
# columns. PRAGMA table_info introspects the live schema; we add only the
# missing columns. SQLite accepts multiple statements separated by ';'.
_MIGRATIONS_V2 = (
    ("max_iterations", "ALTER TABLE sessions ADD COLUMN max_iterations INTEGER"),
    ("max_recursion_depth", "ALTER TABLE sessions ADD COLUMN max_recursion_depth INTEGER"),
    ("routing_policy_json", "ALTER TABLE sessions ADD COLUMN routing_policy_json TEXT"),
    ("tool_call_count", "ALTER TABLE sessions ADD COLUMN tool_call_count INTEGER NOT NULL DEFAULT 0"),
    ("max_tool_calls", "ALTER TABLE sessions ADD COLUMN max_tool_calls INTEGER"),
    ("max_children_concurrent", "ALTER TABLE sessions ADD COLUMN max_children_concurrent INTEGER"),
    ("last_activity_at", "ALTER TABLE sessions ADD COLUMN last_activity_at REAL"),
)

# Full column list, in storage order. Shared by get() and recent() so the
# positional unpacking in _row_to_session stays in lockstep with the SELECT.
_SELECT_COLS = (
    "session_id, agent_id, started_at, completed_at, parent_session_id, "
    "budget_cap_usd, consumed_usd, step_count, state, framework, "
    "max_iterations, max_recursion_depth, routing_policy_json, "
    "tool_call_count, max_tool_calls, max_children_concurrent, last_activity_at"
)

_INSERT = """
INSERT INTO sessions (
    session_id, agent_id, started_at, completed_at, parent_session_id,
    budget_cap_usd, consumed_usd, step_count, state, framework,
    max_iterations, max_recursion_depth, routing_policy_json,
    tool_call_count, max_tool_calls, max_children_concurrent, last_activity_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

# CHZ-AUD-004: the WHERE guard makes the step write conditional on the row
# still being ACTIVE. If a concurrent cancel()/complete()/error() transitioned
# the session to a terminal state between our read and this write, rowcount==0
# and record_step refuses rather than resurrecting the row to 'active'.
_UPDATE_STEP = """
UPDATE sessions SET consumed_usd = ?, step_count = ?, state = ?,
    last_activity_at = ? WHERE session_id = ? AND state = 'active'
"""

# Terminal transition that also stamps activity (cancel / cascade).
_UPDATE_CANCEL = """
UPDATE sessions SET state = ?, completed_at = ?, last_activity_at = ?
    WHERE session_id = ?
"""

_UPDATE_TERMINAL = """
UPDATE sessions SET state = ?, completed_at = ? WHERE session_id = ?
"""


class SessionNotFound(KeyError):
    """Raised when a session_id isn't in the store."""


class TerminalStateViolation(RuntimeError):
    """Raised when a mutation is attempted on a terminal-state session."""


def _policy_to_json(policy: AgentRoutingPolicy | None) -> str | None:
    """Serialise a policy to JSON for the routing_policy_json column.

    inherits_from is NOT serialised — at persistence time we record only
    the leaf policy. Cross-session inheritance lives in the session graph
    (via parent_session_id), not in the stored policy.
    """
    if policy is None:
        return None
    return json.dumps(
        {
            "preferred_providers": list(policy.preferred_providers),
            "preferred_models_by_classification": {
                k: list(v) for k, v in policy.preferred_models_by_classification.items()
            },
            "max_cost_per_turn_usd": policy.max_cost_per_turn_usd,
            "max_temperature": policy.max_temperature,
        }
    )


def _policy_from_json(raw: str | None) -> AgentRoutingPolicy | None:
    if raw is None:
        return None
    data = json.loads(raw)
    by_class = {
        k: tuple(v)
        for k, v in (data.get("preferred_models_by_classification") or {}).items()
    }
    return AgentRoutingPolicy(
        preferred_providers=tuple(data.get("preferred_providers") or ()),
        preferred_models_by_classification=MappingProxyType(by_class),
        max_cost_per_turn_usd=data.get("max_cost_per_turn_usd"),
        max_temperature=data.get("max_temperature"),
    )


def _row_to_session(row: tuple) -> AgentSession:
    # row may carry the T3-M3 max_iterations / max_recursion_depth
    # columns (positions 10–11) and the T3-XL1 routing_policy_json
    # column (position 12). Older rows have fewer fields because the
    # migration ran AFTER the SELECT statement was built — fall back
    # to None for backwards compat.
    max_iter = row[10] if len(row) > 10 else None
    max_depth = row[11] if len(row) > 11 else None
    policy_json = row[12] if len(row) > 12 else None
    # G-029 ledger columns (positions 13–16). Short rows from the
    # children()/by_agent() projections fall back to defaults.
    tool_call_count = row[13] if len(row) > 13 and row[13] is not None else 0
    max_tool_calls = row[14] if len(row) > 14 else None
    max_children_concurrent = row[15] if len(row) > 15 else None
    last_activity_at = row[16] if len(row) > 16 else None
    return AgentSession(
        session_id=row[0],
        agent_id=row[1],
        started_at=row[2],
        completed_at=row[3],
        parent_session_id=row[4],
        budget_cap_usd=row[5],
        consumed_usd=row[6],
        step_count=row[7],
        state=SessionState(row[8]),
        framework=row[9],
        max_iterations=max_iter,
        max_recursion_depth=max_depth,
        routing_policy=_policy_from_json(policy_json),
        tool_call_count=tool_call_count,
        max_tool_calls=max_tool_calls,
        max_children_concurrent=max_children_concurrent,
        last_activity_at=last_activity_at,
    )


class SessionStore:
    """SQLite-backed store. One row per agent session; mutations are
    immediate writes. Caller is responsible for serializing concurrent
    access to the same session_id."""

    def __init__(
        self,
        db_path: Path | None = None,
        *,
        check_same_thread: bool = True,
    ) -> None:
        self.db_path = db_path or Path(
            os.environ.get("LLM_ROUTER_SESSIONS_PATH")
            or (Path.home() / ".llm-router" / "sessions.db")
        )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False lets the admin API share one SessionStore
        # across FastAPI worker threads (matches IdentityStore / QuotaTracker).
        self._conn = sqlite3.connect(
            str(self.db_path), check_same_thread=check_same_thread,
        )
        # CHZ-AUD-003: serialize the read-modify-write in record_step (and other
        # mutators) so concurrent callers on a shared store don't lose updates.
        # In-process guard; WAL + busy_timeout below cover the cross-process case.
        self._lock = threading.RLock()
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)
        # T3-M3: idempotent ALTER TABLE migration for pre-T3-M3 DBs.
        # Introspect existing columns; add only the missing ones.
        existing_cols = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(sessions)").fetchall()
        }
        for col, ddl in _MIGRATIONS_V2:
            if col not in existing_cols:
                self._conn.execute(ddl)
        # Refinement #4: the composite (state, last_activity_at) index is
        # created here — after the column migration above — so legacy DBs
        # that just gained last_activity_at pick it up on open.
        self._conn.execute(_INDEX_STATE_ACTIVITY)
        self._conn.commit()

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def create(
        self,
        *,
        agent_id: str,
        budget_usd: float,
        parent_session_id: str | None = None,
        framework: str | None = None,
        max_iterations: int | None = None,
        max_recursion_depth: int | None = None,
        routing_policy: AgentRoutingPolicy | None = None,
        max_tool_calls: int | None = None,
        max_children_concurrent: int | None = None,
    ) -> AgentSession:
        """Open a new active session with the given budget cap.

        T3-M3 runaway guards (both optional; ``None`` = no cap):

        * ``max_iterations`` — hard cap on ``step_count``. Once
          ``record_step`` would push ``step_count`` past this value,
          ``IterationsExceeded`` is raised and the session
          transitions to ``BUDGET_EXCEEDED`` (terminal).
        * ``max_recursion_depth`` — hard cap on the parent chain.
          Checked at create time by walking
          ``parent_session_id`` recursively; if any ancestor has a
          ``max_recursion_depth`` cap and the walked depth would
          equal-or-exceed it, ``RecursionDepthExceeded`` is raised
          before any row is inserted.

        ``budget_usd`` must be positive; ``max_iterations`` and
        ``max_recursion_depth``, when provided, must be positive.
        """
        if budget_usd <= 0:
            raise ValueError("budget_usd must be positive")
        if max_iterations is not None and max_iterations <= 0:
            raise ValueError("max_iterations must be positive when set")
        if max_recursion_depth is not None and max_recursion_depth <= 0:
            raise ValueError("max_recursion_depth must be positive when set")

        if max_tool_calls is not None and max_tool_calls <= 0:
            raise ValueError("max_tool_calls must be positive when set")
        if max_children_concurrent is not None and max_children_concurrent <= 0:
            raise ValueError("max_children_concurrent must be positive when set")

        # CHZ-AUD-011: hold the same lock as cancel() so a child insert can't
        # interleave with an in-flight cascade cancel. This makes the
        # create-vs-cancel ordering atomic on a shared store: the insert either
        # lands before cancel() takes the lock (and gets swept by its BFS) or
        # after cancel() commits (and is born CANCELLED via the ancestor check
        # below). Without this, a child created inside cancel()'s
        # snapshot-to-commit window escaped as 'active'.
        with self._lock:
            # T3-M3: walk the parent chain and refuse if any ancestor has a
            # max_recursion_depth that this child would breach. Depth is
            # measured against the deepest cap in the chain so a child of a
            # tightly-capped parent inherits the cap.
            if parent_session_id is not None:
                self._enforce_recursion_depth(parent_session_id)

            # CHZ-AUD-011: if any ancestor is already CANCELLED, the emergency
            # stop has fired for this subtree — the child is born CANCELLED so
            # it can never spend, rather than escaping as a fresh 'active' row.
            born_cancelled = (
                parent_session_id is not None
                and self._has_cancelled_ancestor(parent_session_id)
            )
            initial_state = (
                SessionState.CANCELLED if born_cancelled else SessionState.ACTIVE
            )

            session_id = str(uuid.uuid4())
            now = time.time()
            session = AgentSession(
                session_id=session_id,
                agent_id=agent_id,
                started_at=now,
                completed_at=now if born_cancelled else None,
                parent_session_id=parent_session_id,
                budget_cap_usd=budget_usd,
                consumed_usd=0.0,
                step_count=0,
                state=initial_state,
                framework=framework,
                max_iterations=max_iterations,
                max_recursion_depth=max_recursion_depth,
                routing_policy=routing_policy,
                tool_call_count=0,
                max_tool_calls=max_tool_calls,
                max_children_concurrent=max_children_concurrent,
                # Seed activity to creation time so a just-created session is
                # never immediately flagged "stuck".
                last_activity_at=now,
            )
            self._conn.execute(
                _INSERT,
                (
                    session.session_id,
                    session.agent_id,
                    session.started_at,
                    session.completed_at,
                    session.parent_session_id,
                    session.budget_cap_usd,
                    session.consumed_usd,
                    session.step_count,
                    session.state.value,
                    session.framework,
                    session.max_iterations,
                    session.max_recursion_depth,
                    _policy_to_json(session.routing_policy),
                    session.tool_call_count,
                    session.max_tool_calls,
                    session.max_children_concurrent,
                    session.last_activity_at,
                ),
            )
            self._conn.commit()
            return session

    def _has_cancelled_ancestor(self, parent_session_id: str) -> bool:
        """True if the parent chain contains a CANCELLED session.

        Walks ``parent_session_id`` upward (cycle-guarded at 1024 hops,
        parity with ``_enforce_recursion_depth``). A CANCELLED ancestor
        means an emergency stop has cascaded to this subtree, so a
        newly-created child must be born terminal rather than active.
        """
        current_id: str | None = parent_session_id
        for _ in range(1024):
            if current_id is None:
                return False
            try:
                ancestor = self.get(current_id)
            except SessionNotFound:
                return False
            if ancestor.state == SessionState.CANCELLED:
                return True
            current_id = ancestor.parent_session_id
        return False

    def get(self, session_id: str) -> AgentSession:
        cursor = self._conn.execute(
            f"SELECT {_SELECT_COLS} FROM sessions WHERE session_id = ?",
            (session_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise SessionNotFound(session_id)
        return _row_to_session(row)

    def effective_policy(self, session_id: str) -> AgentRoutingPolicy | None:
        """Walk parent chain root→leaf and merge policies child-over-parent.

        Cross-session inheritance for T3-XL1: when a session has a
        ``parent_session_id`` set, the parent's policy fills in any field
        the child leaves unset. Walks recursively to the root, merges
        outward so the leaf wins on conflicts. Returns ``None`` if no
        session in the chain has a policy.

        Cycle guard: bails after 1024 hops (parity with
        ``_enforce_recursion_depth``).
        """
        chain: list[AgentRoutingPolicy] = []  # root first, leaf last
        current_id: str | None = session_id
        for _ in range(1024):
            if current_id is None:
                break
            try:
                s = self.get(current_id)
            except SessionNotFound:
                break
            if s.routing_policy is not None:
                chain.append(s.routing_policy.resolved())
            current_id = s.parent_session_id
        if not chain:
            return None
        # chain[0] is the leaf (the session we started from); walk
        # outward so each merge applies the next ancestor's defaults.
        effective = chain[0]
        for ancestor in chain[1:]:
            effective = effective.merged_with(ancestor)
        return effective

    def _enforce_recursion_depth(self, parent_session_id: str) -> None:
        """Walk the parent chain; raise RecursionDepthExceeded if any
        ancestor's ``max_recursion_depth`` cap would be breached by the
        new child being created at depth = chain_length + 1.

        The walk includes the immediate parent at depth 1; each
        ancestor's cap is evaluated against the depth at which THE
        NEW CHILD would land. Cycle guard: bail after 1024 hops to
        avoid pathological corrupt data sending us into an infinite
        loop.
        """
        depth = 1  # the new child sits 1 level below its parent
        current_id: str | None = parent_session_id
        for _ in range(1024):
            if current_id is None:
                return
            try:
                ancestor = self.get(current_id)
            except SessionNotFound:
                # Stale parent reference — treat as no enforcement.
                return
            if (
                ancestor.max_recursion_depth is not None
                and depth >= ancestor.max_recursion_depth
            ):
                raise RecursionDepthExceeded(
                    parent_session_id=parent_session_id,
                    max_recursion_depth=ancestor.max_recursion_depth,
                    current_depth=depth,
                )
            current_id = ancestor.parent_session_id
            depth += 1

    def envelope(self, session_id: str) -> BudgetEnvelope:
        """Construct a BudgetEnvelope reflecting current consumed/cap."""
        s = self.get(session_id)
        return BudgetEnvelope(cap_usd=s.budget_cap_usd, consumed_usd=s.consumed_usd)

    def check_budget(
        self, session_id: str, prospective_cost_usd: float
    ) -> bool:
        """True if charging prospective_cost would NOT breach the cap.

        Returns False (instead of raising) so callers can render a useful
        error or downshift before refusing.
        """
        env = self.envelope(session_id)
        return not env.would_exceed(prospective_cost_usd)

    def record_step(self, session_id: str, cost_usd: float) -> AgentSession:
        """Record an actual call's cost. Increments step_count and consumed.

        Three terminal-transition conditions, checked in order:

        1. **T3-M3 iterations cap.** If ``max_iterations`` is set and
           the new step count would equal or exceed it, the session
           is transitioned to ``BUDGET_EXCEEDED`` and
           ``IterationsExceeded`` is raised before any cost is
           charged. This precedes the budget check so a runaway loop
           is stopped at the cheapest point.
        2. **Budget cap.** If charging ``cost_usd`` would breach
           ``budget_cap_usd``, the session is transitioned to
           ``BUDGET_EXCEEDED`` and ``BudgetExceeded`` is raised.
        3. **Otherwise.** The step is recorded and the session stays
           ``ACTIVE``.

        Calls on terminal-state sessions raise
        ``TerminalStateViolation`` unchanged.
        """
        # CHZ-AUD-003/004: the whole read-modify-write must be atomic. The
        # lock serializes concurrent record_step/cancel on a shared store (no
        # lost updates); the WHERE state='active' guard + rowcount check below
        # reject a step that races a concurrent terminal transition.
        with self._lock:
            s = self.get(session_id)
            if s.state.is_terminal:
                raise TerminalStateViolation(
                    f"session {session_id} is {s.state.value}; cannot record_step"
                )

            # T3-M3: iterations cap. Checked before budget so a runaway
            # loop halts at the cheapest possible point (no cost charged).
            if (
                s.max_iterations is not None
                and s.step_count + 1 > s.max_iterations
            ):
                # Persist the terminal state before raising so subsequent
                # callers see the transition (parity with budget-breach).
                self._conn.execute(
                    _UPDATE_TERMINAL,
                    (SessionState.BUDGET_EXCEEDED.value, time.time(), session_id),
                )
                self._conn.commit()
                raise IterationsExceeded(
                    session_id=session_id,
                    max_iterations=s.max_iterations,
                    current_step_count=s.step_count,
                )

            env = BudgetEnvelope(cap_usd=s.budget_cap_usd, consumed_usd=s.consumed_usd)
            breached = env.would_exceed(cost_usd)
            new_consumed = s.consumed_usd + cost_usd
            new_step = s.step_count + 1
            new_state = SessionState.BUDGET_EXCEEDED if breached else SessionState.ACTIVE
            now_activity = time.time()
            completed_at = now_activity if breached else None
            cur = self._conn.execute(
                _UPDATE_STEP,
                (new_consumed, new_step, new_state.value, now_activity, session_id),
            )
            if cur.rowcount != 1:
                # A concurrent cancel()/complete()/error() moved the row to a
                # terminal state after our read — do not resurrect it.
                self._conn.rollback()
                fresh = self.get(session_id)
                raise TerminalStateViolation(
                    f"session {session_id} became {fresh.state.value} concurrently; "
                    "record_step rejected"
                )
            if breached:
                self._conn.execute(
                    _UPDATE_TERMINAL,
                    (new_state.value, completed_at, session_id),
                )
            self._conn.commit()
            if breached:
                raise BudgetExceeded(
                    session_id=session_id,
                    cap_usd=s.budget_cap_usd,
                    consumed_usd=s.consumed_usd,
                    proposed_usd=cost_usd,
                )
            return self.get(session_id)

    def complete(self, session_id: str) -> AgentSession:
        """Mark the session COMPLETED. Idempotent — re-completing a
        completed session is a no-op (returns the same row)."""
        s = self.get(session_id)
        if s.state == SessionState.COMPLETED:
            return s
        if s.state.is_terminal:
            raise TerminalStateViolation(
                f"session {session_id} is {s.state.value}; cannot complete"
            )
        now = time.time()
        self._conn.execute(
            _UPDATE_TERMINAL,
            (SessionState.COMPLETED.value, now, session_id),
        )
        self._conn.commit()
        return self.get(session_id)

    def error(self, session_id: str) -> AgentSession:
        """Mark the session ERRORED. Used when the caller hits an
        unrecoverable failure mid-run."""
        s = self.get(session_id)
        if s.state.is_terminal:
            return s  # idempotent — already terminal
        now = time.time()
        self._conn.execute(
            _UPDATE_TERMINAL,
            (SessionState.ERRORED.value, now, session_id),
        )
        self._conn.commit()
        return self.get(session_id)

    def record_tool_call(self, session_id: str, tool_name: str) -> AgentSession:
        """Increment ``tool_call_count`` and bump ``last_activity_at``.

        ``tool_name`` is accepted for call-site clarity and future
        per-tool telemetry; the ledger currently tracks only the count.
        Terminal-state sessions refuse the mutation (parity with
        ``record_step``) so a cancelled/completed run can't keep ticking.
        """
        s = self.get(session_id)
        if s.state.is_terminal:
            raise TerminalStateViolation(
                f"session {session_id} is {s.state.value}; cannot record_tool_call"
            )
        self._conn.execute(
            "UPDATE sessions SET tool_call_count = tool_call_count + 1, "
            "last_activity_at = ? WHERE session_id = ?",
            (time.time(), session_id),
        )
        self._conn.commit()
        return self.get(session_id)

    def cancel(
        self,
        session_id: str,
        reason: str | None = None,
        *,
        cascade: bool = True,
    ) -> AgentSession:
        """Operator emergency stop (G-026/G-030).

        Marks the session ``CANCELLED``. When ``cascade`` (default), walks
        the descendant tree and marks every *non-terminal* child
        ``CANCELLED`` in the same transaction — already-terminal branches
        keep their real outcome. Idempotent: cancelling an already-terminal
        session is a no-op that returns it unchanged.

        ``reason`` is accepted for caller symmetry with the admin endpoint
        (which records it in the admin-action audit); there is no reason
        column on the session row itself.
        """
        # CHZ-AUD-004: hold the same lock as record_step so a concurrent step
        # can't interleave (and so record_step's rollback on the shared
        # connection can't discard an in-flight cancel).
        with self._lock:
            s = self.get(session_id)  # raises SessionNotFound on unknown id
            if s.state.is_terminal:
                return s  # idempotent no-op — never rewrite a terminal outcome

            now = time.time()
            if cascade:
                # Mark the parent CANCELLED FIRST, inside the same lock+txn as
                # the descendant sweep. Combined with create() taking self._lock
                # and checking _has_cancelled_ancestor, this closes the
                # CHZ-AUD-011 race: any child whose insert lands after this
                # point sees a CANCELLED ancestor and is born terminal; any
                # child that landed before is caught by the BFS below.
                self._conn.execute(
                    _UPDATE_CANCEL,
                    (SessionState.CANCELLED.value, now, now, session_id),
                )
                # BFS over descendants; a visited set guards against cyclic
                # parent links in corrupt data (parity with the 1024-hop guard
                # used elsewhere, but exact rather than bounded). Re-scanning
                # children() each level (rather than a one-shot snapshot) also
                # picks up any child that committed just before we took the
                # lock.
                visited: set[str] = {session_id}
                frontier = self.children(session_id)
                while frontier:
                    next_frontier: list[AgentSession] = []
                    for child in frontier:
                        if child.session_id in visited:
                            continue
                        visited.add(child.session_id)
                        if not child.state.is_terminal:
                            self._conn.execute(
                                _UPDATE_CANCEL,
                                (SessionState.CANCELLED.value, now, now,
                                 child.session_id),
                            )
                        next_frontier.extend(self.children(child.session_id))
                    frontier = next_frontier
            else:
                self._conn.execute(
                    _UPDATE_CANCEL,
                    (SessionState.CANCELLED.value, now, now, session_id),
                )

            self._conn.commit()
            return self.get(session_id)

    # ── Queries ───────────────────────────────────────────────────────────

    def children(self, parent_session_id: str) -> list[AgentSession]:
        """All sessions spawned by parent_session_id (for nested agent rollups)."""
        cursor = self._conn.execute(
            "SELECT session_id, agent_id, started_at, completed_at, "
            "parent_session_id, budget_cap_usd, consumed_usd, step_count, "
            "state, framework FROM sessions WHERE parent_session_id = ? "
            "ORDER BY started_at ASC",
            (parent_session_id,),
        )
        return [_row_to_session(row) for row in cursor.fetchall()]

    def by_agent(self, agent_id: str, limit: int = 50) -> list[AgentSession]:
        cursor = self._conn.execute(
            "SELECT session_id, agent_id, started_at, completed_at, "
            "parent_session_id, budget_cap_usd, consumed_usd, step_count, "
            "state, framework FROM sessions WHERE agent_id = ? "
            "ORDER BY started_at DESC LIMIT ?",
            (agent_id, limit),
        )
        return [_row_to_session(row) for row in cursor.fetchall()]

    def recent(
        self,
        limit: int = 100,
        state: str | None = None,
        before: tuple[float, str] | None = None,
    ) -> list[AgentSession]:
        """Newest-first session ledger with optional state filter and
        keyset pagination.

        Ordering is ``(started_at DESC, session_id DESC)`` — a strict total
        order so identical timestamps can't skip or duplicate a row across
        pages. ``before`` is the ``(started_at, session_id)`` of the last
        row on the previous page; rows strictly *older* than it in that
        order are returned. ``state`` matches the lifecycle value exactly
        (e.g. ``"active"``, ``"cancelled"``).
        """
        clauses: list[str] = []
        params: list[object] = []
        if state is not None:
            clauses.append("state = ?")
            params.append(state)
        if before is not None:
            before_ts, before_sid = before
            clauses.append(
                "(started_at < ? OR (started_at = ? AND session_id < ?))"
            )
            params.extend([before_ts, before_ts, before_sid])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        cursor = self._conn.execute(
            f"SELECT {_SELECT_COLS} FROM sessions {where} "
            "ORDER BY started_at DESC, session_id DESC LIMIT ?",
            params,
        )
        return [_row_to_session(row) for row in cursor.fetchall()]

    def rollup(self, session_id: str) -> dict:
        """Total cost across this session + all its descendants.

        Used for nested agent reporting: agent A spawned agent B, B spawned
        C — rollup(A) sums A + B + C.
        """
        s = self.get(session_id)
        total_cost = s.consumed_usd
        total_steps = s.step_count
        descendant_count = 0
        # BFS through children
        frontier = self.children(session_id)
        while frontier:
            next_frontier: list[AgentSession] = []
            for child in frontier:
                total_cost += child.consumed_usd
                total_steps += child.step_count
                descendant_count += 1
                next_frontier.extend(self.children(child.session_id))
            frontier = next_frontier
        return {
            "session_id": session_id,
            "agent_id": s.agent_id,
            "total_cost_usd": total_cost,
            "total_steps": total_steps,
            "descendant_session_count": descendant_count,
            "state": s.state.value,
        }

    def close(self) -> None:
        self._conn.close()
