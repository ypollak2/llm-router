"""Dual-write lineage storage — JSONL for real-time, SQLite for analytics."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

from llm_router.sqlite_wal import enable_wal

if TYPE_CHECKING:
    from llm_router.lineage.decision_logger import RoutingDecision


# Concurrent writers (the agentic router fans out across threads, and each
# LineageStore opens its own short-lived connection) all contend on one file.
# SQLite serialises writers under any journal mode, so the thing that decides
# whether a writer queues or raises "database is locked" is the busy timeout —
# and Python's default is only 5s. Measured here at 60 threads x 40 writes:
#
#   rollback journal : slowest writer 4.41s  -> 1.1x headroom under 5s
#   WAL              : slowest writer 1.50s  ->  20x headroom under 30s
#
# 4.41s against a 5s budget is why a slower CI runner tipped over into
# sqlite3.OperationalError. Both levers matter: the timeout is what prevents
# the error, WAL is what keeps the wait short (and lets readers run alongside
# the writer). Same pattern as budget_backend.py and result_cache.py.
_BUSY_TIMEOUT_MS = 30_000


def _connect(db_file: Path | str) -> sqlite3.Connection:
    """Open the lineage DB with concurrency-safe pragmas.

    ``journal_mode`` is persisted in the database header, so it only has to win
    once; ``busy_timeout`` is per-connection and must be set on every connect.
    """
    conn = sqlite3.connect(str(db_file))
    # RED5-01 (P0): the WAL switch takes an exclusive lock, and on a cold start
    # several hooks race for it. Unguarded, the loser's PRAGMA raised straight
    # out of this constructor — 4 of 12 concurrent LineageStore constructions
    # died that way. enable_wal() also inspects the RETURNED mode, because the
    # non-raising failure is reported by answering "delete".
    enable_wal(conn, busy_timeout_ms=_BUSY_TIMEOUT_MS, label="lineage_store")
    return conn


class LineageStore:
    """Dual-write storage backend for routing decisions.

    - JSONL file for real-time access and log rotation
    - SQLite for efficient queries and analytics
    """

    def __init__(
        self,
        router_dir: Path | str | None = None,
        *,
        db_path: Path | str | None = None,
    ) -> None:
        """Initialize lineage store.

        Two construction modes:

        * ``router_dir`` (or no args): the store owns a *directory* and
          places ``routing_lineage.jsonl`` and ``routing_lineage.db``
          inside it. This is the production shape — every src/ caller
          uses ``LineageStore()`` with no args, defaulting to
          ``~/.llm-router/``.

        * ``db_path``: the caller names a specific SQLite file. The JSONL
          sidecar is placed next to it, sharing the file stem
          (``my_lineage.db`` → ``my_lineage.jsonl``). Tests use this mode
          to direct each test's writes at a fresh ``tmp_path`` file
          without collisions.

        Args:
            router_dir: Override default ``~/.llm-router`` directory.
            db_path: Direct path to the SQLite file; JSONL sidecar is
                placed next to it. Keyword-only.

        Raises:
            ValueError: if both ``router_dir`` and ``db_path`` are given.
                The combination is ambiguous — the caller should pick
                exactly one construction mode.
        """
        if router_dir is not None and db_path is not None:
            raise ValueError(
                "Provide either router_dir or db_path, not both — they "
                "describe overlapping locations and the combination is "
                "ambiguous."
            )

        if db_path is not None:
            db_path = Path(db_path)
            db_path.parent.mkdir(parents=True, exist_ok=True)
            self.db_file = db_path
            # JSONL sidecar shares the stem so a caller that says
            # ``db_path=tmp/foo.db`` gets ``tmp/foo.jsonl`` alongside it.
            self.jsonl_file = db_path.with_suffix(".jsonl")
        else:
            if router_dir is None:
                router_dir = Path.home() / ".llm-router"
            else:
                router_dir = Path(router_dir)
            router_dir.mkdir(parents=True, exist_ok=True)
            self.jsonl_file = router_dir / "routing_lineage.jsonl"
            self.db_file = router_dir / "routing_lineage.db"

        # Initialize SQLite schema
        self._init_db()

    def _init_db(self) -> None:
        """Initialize SQLite schema if not present.

        Two parallel tables coexist:

        * ``routing_decisions`` — the production write path, populated by
          ``LineageStore.append(RoutingDecision)``. Schema is shaped
          around the routing-decision audit trail.
        * ``lineage`` — the planned-API write path, populated by
          ``LineageStore.record(LineageRecord)`` and consumed by the
          inversion / summary / session-step queries. Schema mirrors
          ``LineageRecord.to_row()``.

        Both share the same SQLite file; the constructor's ``router_dir``
        vs ``db_path`` mode just decides where that file lives.
        """
        conn = _connect(self.db_file)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS routing_decisions (
                    decision_id TEXT PRIMARY KEY,
                    operation TEXT NOT NULL,
                    classification TEXT NOT NULL,
                    selected_model TEXT NOT NULL,
                    selection_reason TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    total_tokens INTEGER,
                    cost_usd REAL,
                    latency_ms REAL,
                    routing_overhead_ms REAL,
                    fallback_chain TEXT,  -- JSON array as string
                    fallback_reason TEXT,
                    request_id TEXT,
                    parent_decision_id TEXT,
                    metadata TEXT,  -- JSON object as string
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS lineage (
                    id TEXT PRIMARY KEY,
                    timestamp REAL NOT NULL,
                    host TEXT NOT NULL,
                    prompt_fingerprint TEXT,
                    task_type TEXT,
                    complexity TEXT,
                    classifier_method TEXT,
                    signal_scores TEXT,    -- JSON object
                    fired_decisions TEXT,  -- JSON array
                    chain_attempted TEXT,  -- JSON array
                    model_chosen TEXT,
                    model_tier TEXT,
                    inversion TEXT,
                    outcome TEXT,
                    latency_ms INTEGER,
                    cost_usd REAL,
                    notes TEXT,
                    agent_id TEXT,
                    session_id TEXT,
                    step_index INTEGER,
                    parent_session_id TEXT,
                    framework TEXT
                )
            """)
            # Indexes on both tables.
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_operation ON routing_decisions(operation)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_model ON routing_decisions(selected_model)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_classification ON routing_decisions(classification)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON routing_decisions(timestamp)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_lr_timestamp ON lineage(timestamp)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_lr_inversion ON lineage(inversion)"
            )
            # ── Migration: add v0.0.2 agent-session columns to pre-v0.0.2 dbs.
            # Must precede the agent_session index — SQLite refuses to index a
            # column that doesn't exist on the table yet.
            # PRAGMA table_info is the canonical introspection; ALTER TABLE
            # ADD COLUMN is idempotent only via a missing-column check.
            existing_cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(lineage)").fetchall()
            }
            for col, ddl in (
                ("agent_id", "agent_id TEXT"),
                ("session_id", "session_id TEXT"),
                ("step_index", "step_index INTEGER"),
                ("parent_session_id", "parent_session_id TEXT"),
                ("framework", "framework TEXT"),
            ):
                if col not in existing_cols:
                    conn.execute(f"ALTER TABLE lineage ADD COLUMN {ddl}")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_lr_agent_session ON lineage(agent_id, session_id)"
            )
            conn.commit()
        finally:
            conn.close()

    def append(self, decision: RoutingDecision) -> None:
        """Append a routing decision to both JSONL and SQLite.

        Args:
            decision: RoutingDecision to log
        """
        # Write to JSONL (append-only)
        with open(self.jsonl_file, "a") as f:
            f.write(json.dumps(decision.to_dict()) + "\n")

        # Write to SQLite
        conn = _connect(self.db_file)
        try:
            conn.execute(
                """
                INSERT INTO routing_decisions (
                    decision_id, operation, classification, selected_model,
                    selection_reason, timestamp, input_tokens, output_tokens,
                    total_tokens, cost_usd, latency_ms, routing_overhead_ms,
                    fallback_chain, fallback_reason, request_id, parent_decision_id,
                    metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision.decision_id,
                    decision.operation,
                    decision.classification,
                    decision.selected_model,
                    decision.selection_reason,
                    decision.timestamp,
                    decision.input_tokens,
                    decision.output_tokens,
                    decision.total_tokens,
                    decision.cost_usd,
                    decision.latency_ms,
                    decision.routing_overhead_ms,
                    json.dumps(decision.fallback_chain),
                    decision.fallback_reason,
                    decision.request_id,
                    decision.parent_decision_id,
                    json.dumps(decision.metadata),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def query_jsonl(self, limit: int = 100) -> list[dict]:
        """Read recent decisions from JSONL file.

        Args:
            limit: Max number of recent records to return

        Returns:
            List of decision records (most recent first)
        """
        if not self.jsonl_file.exists():
            return []

        records = []
        with open(self.jsonl_file) as f:
            for line in f:
                records.append(json.loads(line))

        return records[-limit:]  # Last N records

    def recent(self, limit: int = 50) -> list[dict]:
        """Return the N most recent routing decisions as a list of dicts.

        Backward-compatible shim for callers written against the pre-v0.1.x
        flat-module ``LineageStore.recent(limit)`` API (notably
        ``llm_router.observability.summary``).

        Two-source merge:
          1. **New canonical store** — ``routing_lineage.jsonl``. Populated
             via ``LineageStore.append()``.
          2. **Legacy store** — ``model_tracking.jsonl``. Populated by
             ``llm_router.model_tracking.log_routing_decision``, which is the
             code path the production auto-route hook actually uses today.

        The v0.1.x rewrite added the new store but never migrated the
        production write path. Until that migration lands, the legacy file
        carries the real data — so we merge both and remap legacy field
        names (``selected_model`` → ``model_chosen``, ``cost_usd_estimate``
        → ``cost_usd``) plus derive ``model_tier`` and ``inversion`` via
        ``tier_for_model``/``detect_inversion`` so the dashboard renders
        meaningful tier breakdowns and inversion counts.
        """
        rows = list(self.query_jsonl(limit=limit))
        if rows:
            return rows

        legacy = self.jsonl_file.parent / "model_tracking.jsonl"
        if not legacy.exists():
            return rows

        # Lazy import to avoid a circular dependency at module load.
        from llm_router.lineage.types import detect_inversion, tier_for_model

        adapted: list[dict] = []
        with open(legacy) as f:
            for line in f:
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    continue
                model_chosen = raw.get("selected_model", "")
                provider = raw.get("provider", "unknown")
                task_type = raw.get("task_type", "unknown")
                complexity = raw.get("complexity", "unknown")

                # Meta-task relabel: production model_tracking logger
                # writes selected_model="unknown" for routing decisions
                # that didn't pick a model (coordination/introspect tasks
                # handled by llm_router or Claude internally). Surface those
                # as tier="meta" / model="llm_router-internal" so the
                # dashboard shows them in an honest bucket instead of
                # mixing with genuine routing failures.
                if model_chosen == "unknown" or task_type in ("coordination", "introspect"):
                    model_chosen = "llm_router-internal"
                    provider = "meta"
                    tier_value = "meta"
                    inversion_value = "none"
                else:
                    tier = tier_for_model(model_chosen)
                    tier_value = tier.value
                    inversion_value = detect_inversion(complexity, tier).value

                adapted.append({
                    "timestamp": raw.get("timestamp", 0),
                    "task_type": task_type,
                    "complexity": complexity,
                    "classifier_method": raw.get("classification_method", "unknown"),
                    "model_chosen": model_chosen,
                    "model_tier": tier_value,
                    "inversion": inversion_value,
                    "outcome": "success",
                    "success": True,
                    "latency_ms": 0,
                    "cost_usd": raw.get("cost_usd_estimate") or 0.0,
                    "host": provider,
                    "notes": raw.get("notes") or "",
                    "framework": None,
                })

        adapted.sort(key=lambda r: r["timestamp"], reverse=True)
        return adapted[:limit]

    # ── Planned-API: LineageRecord write + query surface ─────────────────
    # Tests across qa/, scenarios/, and test_lineage.py write
    # ``LineageRecord`` instances built via ``make_record`` and expect
    # the queries below. These methods write to a SECOND SQLite table
    # (``lineage``) so the production ``append(RoutingDecision)``
    # write path is unaffected.

    def record(self, rec) -> None:  # rec: LineageRecord (avoid runtime import cycle)
        """Persist a ``LineageRecord`` to JSONL and SQLite.

        Args:
            rec: A ``LineageRecord`` (see ``llm_router.lineage.types``). The
                record is appended to ``self.jsonl_file`` (one JSON object
                per line) and inserted into the ``lineage`` table.

        Behaviour notes:
          * Writes are best-effort sequentially — the JSONL write happens
            first so a crash mid-call leaves a recoverable line.
          * The SQLite insert uses ``rec.to_row()`` directly; the schema
            and the row tuple must stay aligned (covered by the
            roundtrip tests in ``tests/test_lineage.py``).
        """
        from llm_router.lineage.types import LineageRecord  # local import: avoid cycle

        if not isinstance(rec, LineageRecord):
            raise TypeError(
                f"LineageStore.record requires a LineageRecord, got {type(rec).__name__}"
            )

        # 1) JSONL append — store the same shape the reader returns.
        payload = self._lineage_record_to_dict(rec)
        with open(self.jsonl_file, "a") as f:
            f.write(json.dumps(payload) + "\n")

        # 2) SQLite insert.
        conn = _connect(self.db_file)
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO lineage (
                    id, timestamp, host, prompt_fingerprint, task_type,
                    complexity, classifier_method, signal_scores,
                    fired_decisions, chain_attempted, model_chosen,
                    model_tier, inversion, outcome, latency_ms, cost_usd,
                    notes, agent_id, session_id, step_index,
                    parent_session_id, framework
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rec.to_row(),
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _lineage_record_to_dict(rec) -> dict:  # rec: LineageRecord
        """Map a ``LineageRecord`` to the dict shape ``recent()`` returns.

        ``signal_scores`` / ``fired_decisions`` / ``chain_attempted`` are
        stored as JSON-encoded strings so the shape matches what the
        SQLite-backed ``inversions``/``session_steps`` queries return —
        callers can ``json.loads`` whichever query path they used and
        get the same answer.
        """
        return {
            "id": rec.id,
            "timestamp": rec.timestamp,
            "host": rec.host,
            "prompt_fingerprint": rec.prompt_fingerprint,
            "task_type": rec.task_type,
            "complexity": rec.complexity,
            "classifier_method": rec.classifier_method,
            "signal_scores": json.dumps(dict(rec.signal_scores)),
            "fired_decisions": json.dumps(list(rec.fired_decisions)),
            "chain_attempted": json.dumps(list(rec.chain_attempted)),
            "model_chosen": rec.model_chosen,
            "model_tier": rec.model_tier.value,
            "inversion": rec.inversion.value,
            "outcome": rec.outcome,
            "latency_ms": rec.latency_ms,
            "cost_usd": rec.cost_usd,
            "notes": rec.notes,
            "agent_id": rec.agent_id,
            "session_id": rec.session_id,
            "step_index": rec.step_index,
            "parent_session_id": rec.parent_session_id,
            "framework": rec.framework,
        }

    def _row_to_lineage_dict(self, row: dict) -> dict:
        """Materialise a ``lineage`` SQLite row.

        JSON-encoded columns (``signal_scores`` / ``fired_decisions`` /
        ``chain_attempted``) stay as their JSON-string form, matching
        ``_lineage_record_to_dict``. Callers that want the parsed form
        ``json.loads`` whichever path they used.
        """
        return dict(row)

    def inversions(self, kind=None) -> list[dict]:
        """Return rows with an inversion verdict.

        Args:
            kind: Optional ``Inversion`` enum (UP / DOWN). When provided,
                only rows whose ``inversion`` column matches are
                returned. When omitted, every up- and down-inversion
                row is returned (anything except NONE).
        """
        from llm_router.lineage.types import Inversion  # local import: avoid cycle

        if kind is None:
            sql = (
                "SELECT * FROM lineage "
                "WHERE inversion != ? ORDER BY timestamp DESC"
            )
            params = (Inversion.NONE.value,)
        else:
            sql = (
                "SELECT * FROM lineage "
                "WHERE inversion = ? ORDER BY timestamp DESC"
            )
            params = (kind.value,)

        rows = self.query_db(sql, params)
        return [self._row_to_lineage_dict(r) for r in rows]

    def summary(self) -> dict:
        """Aggregate inversion counts and rate across the lineage table.

        The denominator for ``inversion_rate`` is the count of *decided*
        rows — every row whose inversion is non-NONE. With four rows of
        which two are UP, one DOWN, one NONE: rate = 3/4 = 0.75.
        """
        from llm_router.lineage.types import Inversion

        rows = self.query_db(
            "SELECT inversion FROM lineage"
        )
        total = len(rows)
        up = sum(1 for r in rows if r["inversion"] == Inversion.UP.value)
        down = sum(1 for r in rows if r["inversion"] == Inversion.DOWN.value)
        none = sum(1 for r in rows if r["inversion"] == Inversion.NONE.value)
        decided = total - none
        return {
            "total_decisions": total,
            "up_inversions": up,
            "down_inversions": down,
            "no_inversion": none,
            "inversion_rate": (decided / total) if total else 0.0,
        }

    def by_session(self, session_id: str, agent_id: str | None = None) -> list[dict]:
        """Return every lineage row for ``session_id`` in step-index order.

        Args:
            session_id: The session id to filter by.
            agent_id: Optional secondary filter. When provided, only rows
                whose ``agent_id`` matches are returned — useful when one
                session id is shared across agent identities (rare).
        """
        if agent_id is None:
            rows = self.query_db(
                "SELECT * FROM lineage "
                "WHERE session_id = ? "
                "ORDER BY step_index ASC, timestamp ASC",
                (session_id,),
            )
        else:
            rows = self.query_db(
                "SELECT * FROM lineage "
                "WHERE session_id = ? AND agent_id = ? "
                "ORDER BY step_index ASC, timestamp ASC",
                (session_id, agent_id),
            )
        return [self._row_to_lineage_dict(r) for r in rows]

    def by_framework(self, framework: str) -> list[dict]:
        """Return every lineage row whose ``framework`` column matches.

        Used by the framework-attribution scenarios to confirm that a
        framework slug round-trips from ``make_record(framework=...)``
        through the SQLite store and back out.
        """
        rows = self.query_db(
            "SELECT * FROM lineage WHERE framework = ? ORDER BY timestamp DESC",
            (framework,),
        )
        return [self._row_to_lineage_dict(r) for r in rows]

    def close(self) -> None:
        """No-op connection close.

        The store opens a fresh SQLite connection per call (see ``record``
        / ``append`` / ``query_db``) so there is no long-lived handle to
        close. Provided for API symmetry with callers that expect a
        ``close()`` method (notably ``tests/qa/test_integrity.py``).
        """
        return None

    def query_db(self, sql: str, params: tuple = ()) -> list[dict]:
        """Execute SQL query against SQLite backend.

        Args:
            sql: SQL query
            params: Query parameters

        Returns:
            List of result rows as dicts
        """
        conn = _connect(self.db_file)
        conn.row_factory = sqlite3.Row  # Return rows as dicts
        try:
            cursor = conn.execute(sql, params)
            return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()
