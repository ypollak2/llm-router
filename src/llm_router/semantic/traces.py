"""What ran, what it saw, and what was actually done about it.

The arms exist to answer "did this help", and that question needs three things
the layer was throwing away at the end of every call: which treatment ran, what
evidence it saw, and which state it ran against. A review of this branch named
the gap directly — traces are computed in memory and discarded, which is fine
for an MVP and fatal for the evaluation, because replaying an outcome is only
valid for rows generated under the evidence conditions being compared.

You can always re-run an arm. Without a trace you cannot say the arm is why the
number moved.

TWO TRACES, BECAUSE TWO DIFFERENT THINGS ARE BEING CLAIMED

`RetrievalTrace` is what the system OFFERED: the arm, both snapshots, the exact
evidence with the hashes it was read at, the lessons, the conflicts that were
still unresolved at the time, and everything dropped with its reason.

`InterventionTrace` is what was then DONE. It is the more important of the two
and the easier to fake. "The agent was shown the lesson" is not "the lesson
prevented the bug", and a log that cannot distinguish "the check ran and passed"
from "the check was never run" will report prevention coverage it never had.
Hence `unobserved_steps`, and hence `prevention_coverage` returning
`unavailable` rather than anything cheerier when nothing ran.

REPLAY KEYS

`replay_key()` is the identity of a retrieval condition: scope, both snapshots,
the query, the arm, and the evidence actually selected. Two runs that share it
saw the same thing, so a difference in outcome is the model or the luck — not
retrieval drift. Two runs that do not share it are not comparable, however
similar the numbers look.

That is why the memory snapshot is in the key alongside the code snapshot.
Somebody filing a lesson changes what the system offers while every line of
code stands still, and an outcome attributed to the commit would be attributed
to the wrong treatment entirely.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY, value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS retrieval_trace (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at         TEXT NOT NULL,
    scope_id            TEXT NOT NULL,
    snapshot_id         TEXT NOT NULL,
    memory_snapshot_id  TEXT NOT NULL,
    arm                 TEXT NOT NULL,
    query               TEXT NOT NULL,
    retriever_version   INTEGER NOT NULL,
    retrieval_status    TEXT NOT NULL,
    budget_tokens       INTEGER NOT NULL,
    retrieved_tokens    INTEGER NOT NULL,
    selected_evidence   TEXT NOT NULL,
    applicable_lessons  TEXT NOT NULL,
    decision_constraints TEXT NOT NULL,
    unresolved_conflicts TEXT NOT NULL,
    suggested_checks    TEXT NOT NULL,
    missing_requirements TEXT NOT NULL,
    omissions           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS retrieval_by_scope ON retrieval_trace(scope_id);
CREATE INDEX IF NOT EXISTS retrieval_by_arm ON retrieval_trace(arm);

CREATE TABLE IF NOT EXISTS intervention_trace (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at      TEXT NOT NULL,
    retrieval_id     INTEGER NOT NULL,
    task_id          TEXT NOT NULL,
    action_taken     TEXT NOT NULL,
    checks_run       TEXT NOT NULL,
    outcomes         TEXT NOT NULL,
    unobserved_steps TEXT NOT NULL,
    FOREIGN KEY (retrieval_id) REFERENCES retrieval_trace(id)
);
CREATE INDEX IF NOT EXISTS intervention_by_retrieval
    ON intervention_trace(retrieval_id);
"""

# Bumped when retrieval's SELECTION BEHAVIOUR changes. Two traces with
# different retriever versions are not comparable even if everything else
# matches, because the thing being measured moved underneath them.
RETRIEVER_VERSION = 1

# An outcome string that means "nobody looked". Kept identical in spirit to
# library-harvest's `_outcome`, which learned the same lesson the hard way.
UNKNOWN = "unknown"


@dataclass(frozen=True)
class RetrievalTrace:
    id: int
    recorded_at: str
    scope_id: str
    snapshot_id: str
    memory_snapshot_id: str
    arm: str
    query: str
    retriever_version: int
    retrieval_status: str
    budget_tokens: int
    retrieved_tokens: int
    selected_evidence: list[dict[str, Any]] = field(default_factory=list)
    applicable_lessons: list[str] = field(default_factory=list)
    decision_constraints: list[str] = field(default_factory=list)
    unresolved_conflicts: list[dict] = field(default_factory=list)
    suggested_checks: list[str] = field(default_factory=list)
    missing_requirements: list[str] = field(default_factory=list)
    omissions: list[str] = field(default_factory=list)

    def replay_key(self) -> str:
        """The identity of a retrieval condition.

        Two traces sharing this saw the same evidence at the same state, so a
        difference in outcome is attributable to something else. Two traces not
        sharing it are not comparable however close their numbers look.

        Deliberately includes the memory snapshot: filing a lesson changes what
        the system offers while the code stands still, and crediting that to
        the commit is crediting the wrong treatment.
        """
        payload = json.dumps({
            "scope": self.scope_id,
            "code": self.snapshot_id,
            "memory": self.memory_snapshot_id,
            "arm": self.arm,
            "query": self.query,
            "retriever": self.retriever_version,
            "evidence": sorted(
                (e.get("path", ""), e.get("symbol", ""), e.get("source_hash", ""))
                for e in self.selected_evidence
            ),
            "lessons": sorted(self.applicable_lessons),
        }, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True)
class InterventionTrace:
    id: int
    recorded_at: str
    retrieval_id: int
    task_id: str
    action_taken: str
    checks_run: list[str] = field(default_factory=list)
    outcomes: dict[str, str] = field(default_factory=dict)
    unobserved_steps: list[str] = field(default_factory=list)

    def prevention_coverage(self, suggested_checks: list[str]) -> str:
        """`observed`, `partial`, `unavailable` — never a number.

        The question is not "how many checks ran" but "can this task's outcome
        be used as evidence that a known failure was prevented". Three honest
        answers:

            observed      every suggested check ran AND reported a result
            partial       some ran, or some result was never observed
            unavailable   none ran, so this task says nothing about prevention

        A check with an `unknown` outcome counts as not observed. Logs saying
        "the agent followed the lesson" do not establish prevention, and a
        counterfactual cannot be read out of an intervention log at all — this
        only reports what was actually watched.
        """
        wanted = [c for c in suggested_checks if c]
        if not wanted:
            return "unavailable"
        confirmed = [
            c for c in wanted
            if c in self.checks_run
            and self.outcomes.get(c, UNKNOWN) not in (UNKNOWN, "", None)
        ]
        if not confirmed:
            return "unavailable"
        if len(confirmed) == len(wanted) and not self.unobserved_steps:
            return "observed"
        return "partial"


class TraceStore:
    """Traces on disk, scoped by project like everything else here."""

    def __init__(self, root: Path | str):
        self.root = Path(root)

    def _connect(self) -> sqlite3.Connection:
        self.root.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.root / "traces.sqlite"))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),))
        conn.commit()
        return conn

    # -- write ----------------------------------------------------------------

    def record_retrieval(self, pack, query: str, arm: str = "") -> int:
        """Persist what a pack offered. Returns the trace id."""
        from llm_router.semantic.experience import record_id

        evidence = [
            {"id": e.get("id", ""), "path": e.get("path", ""),
             "symbol": e.get("symbol", ""), "source_hash": e.get("source_hash", ""),
             "span": e.get("span", {})}
            for e in pack.evidence
        ]
        conn = self._connect()
        try:
            with conn:
                cur = conn.execute(
                    "INSERT INTO retrieval_trace(recorded_at, scope_id, snapshot_id, "
                    "memory_snapshot_id, arm, query, retriever_version, "
                    "retrieval_status, budget_tokens, retrieved_tokens, "
                    "selected_evidence, applicable_lessons, decision_constraints, "
                    "unresolved_conflicts, suggested_checks, missing_requirements, "
                    "omissions) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        datetime.now(timezone.utc).isoformat(),
                        pack.scope_id, pack.snapshot_id, pack.memory_snapshot_id,
                        arm, query, RETRIEVER_VERSION, pack.retrieval_status,
                        pack.budget_tokens, pack.retrieved_tokens,
                        json.dumps(evidence),
                        json.dumps([record_id(le.record)
                                    for le in pack.applicable_lessons]),
                        json.dumps([record_id(le.record)
                                    for le in pack.decision_constraints]),
                        json.dumps(pack.unresolved_conflicts),
                        json.dumps(pack.suggested_checks),
                        json.dumps(pack.missing_requirements),
                        json.dumps(pack.omissions),
                    ),
                )
                return int(cur.lastrowid)
        finally:
            conn.close()

    def record_intervention(
        self,
        retrieval_id: int,
        task_id: str,
        action_taken: str,
        checks_run: list[str] | None = None,
        outcomes: dict[str, str] | None = None,
        unobserved_steps: list[str] | None = None,
    ) -> int:
        conn = self._connect()
        try:
            with conn:
                cur = conn.execute(
                    "INSERT INTO intervention_trace(recorded_at, retrieval_id, "
                    "task_id, action_taken, checks_run, outcomes, unobserved_steps) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (datetime.now(timezone.utc).isoformat(), retrieval_id, task_id,
                     action_taken, json.dumps(checks_run or []),
                     json.dumps(outcomes or {}), json.dumps(unobserved_steps or [])),
                )
                return int(cur.lastrowid)
        finally:
            conn.close()

    # -- read -----------------------------------------------------------------

    def _to_retrieval(self, row: sqlite3.Row) -> RetrievalTrace:
        return RetrievalTrace(
            id=row["id"], recorded_at=row["recorded_at"],
            scope_id=row["scope_id"], snapshot_id=row["snapshot_id"],
            memory_snapshot_id=row["memory_snapshot_id"], arm=row["arm"],
            query=row["query"], retriever_version=row["retriever_version"],
            retrieval_status=row["retrieval_status"],
            budget_tokens=row["budget_tokens"],
            retrieved_tokens=row["retrieved_tokens"],
            selected_evidence=json.loads(row["selected_evidence"]),
            applicable_lessons=json.loads(row["applicable_lessons"]),
            decision_constraints=json.loads(row["decision_constraints"]),
            unresolved_conflicts=json.loads(row["unresolved_conflicts"]),
            suggested_checks=json.loads(row["suggested_checks"]),
            missing_requirements=json.loads(row["missing_requirements"]),
            omissions=json.loads(row["omissions"]),
        )

    def get_retrieval(self, trace_id: int) -> RetrievalTrace | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM retrieval_trace WHERE id = ?", (trace_id,)
            ).fetchone()
            return self._to_retrieval(row) if row else None
        finally:
            conn.close()

    def get_intervention(self, trace_id: int) -> InterventionTrace | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM intervention_trace WHERE id = ?", (trace_id,)
            ).fetchone()
            if not row:
                return None
            return InterventionTrace(
                id=row["id"], recorded_at=row["recorded_at"],
                retrieval_id=row["retrieval_id"], task_id=row["task_id"],
                action_taken=row["action_taken"],
                checks_run=json.loads(row["checks_run"]),
                outcomes=json.loads(row["outcomes"]),
                unobserved_steps=json.loads(row["unobserved_steps"]),
            )
        finally:
            conn.close()

    def for_scope(self, scope_id: str) -> list[RetrievalTrace]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM retrieval_trace WHERE scope_id = ? ORDER BY id",
                (scope_id,),
            ).fetchall()
            return [self._to_retrieval(r) for r in rows]
        finally:
            conn.close()

    def for_arm(self, arm: str) -> list[RetrievalTrace]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM retrieval_trace WHERE arm = ? ORDER BY id", (arm,)
            ).fetchall()
            return [self._to_retrieval(r) for r in rows]
        finally:
            conn.close()
