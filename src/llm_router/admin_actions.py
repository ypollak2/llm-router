"""G-006-F5 admin-action audit log.

Separate from the routing audit (``enterprise.audit.AuditLog``) on
purpose: the routing audit captures one row per LLM call (read-heavy,
high-volume); the admin-action log captures one row per platform-team
mutation (write-light, accountability-critical).

Schema is intentionally narrow:

* ``actor_email`` — the principal who made the change.
* ``actor_user_id`` — id of the principal.
* ``action`` — verb in the form ``"resource_kind:operation"``
  (e.g. ``"user:create"``, ``"provider:disable"``, ``"policy:push"``).
* ``resource_id`` — what was changed (a user id, provider name,
  policy path, …).
* ``detail_json`` — small JSON blob with the rest of the context.
* ``timestamp`` — Unix seconds; the only natural sort key.

Deliberate non-features for this slice:

* No hash-chain (the routing audit already has one for tamper
  evidence; the admin log lives next to it and is short enough that
  operators can spot anomalies by reading the table directly).
* No SIEM push — that's G-010 and lands by tailing the same SQLite
  file.
* No per-action permission gate beyond what the endpoint itself
  already enforces (we trust the caller; rows are only ever written
  *after* the endpoint's ``require_perm`` passed).

See: ``docs/audit/post-remediation/GAP_ANALYSIS.md`` G-006-F5.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any


_SCHEMA = """
CREATE TABLE IF NOT EXISTS admin_actions (
    id TEXT PRIMARY KEY,
    timestamp REAL NOT NULL,
    actor_user_id TEXT NOT NULL,
    actor_email TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_admin_actions_ts ON admin_actions(timestamp);
CREATE INDEX IF NOT EXISTS idx_admin_actions_actor ON admin_actions(actor_user_id);
CREATE INDEX IF NOT EXISTS idx_admin_actions_action ON admin_actions(action);
"""


class AdminActionLog:
    """SQLite-backed append-only log of admin-API mutations."""

    def __init__(
        self,
        db_path: Path | None = None,
        *,
        check_same_thread: bool = True,
    ) -> None:
        self.db_path = db_path or Path(
            os.environ.get("LLM_ROUTER_ADMIN_ACTIONS_PATH")
            or (Path.home() / ".llm-router" / "admin_actions.db")
        )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.db_path), check_same_thread=check_same_thread
        )
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._lock = threading.Lock()

    def append(
        self,
        *,
        actor_user_id: str,
        actor_email: str,
        action: str,
        resource_id: str,
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist one admin-action row. Returns the inserted record."""
        row = {
            "id": str(uuid.uuid4()),
            "timestamp": time.time(),
            "actor_user_id": actor_user_id,
            "actor_email": actor_email,
            "action": action,
            "resource_id": resource_id,
            "detail_json": json.dumps(detail or {}),
        }
        with self._lock:
            self._conn.execute(
                "INSERT INTO admin_actions "
                "(id, timestamp, actor_user_id, actor_email, action, "
                "resource_id, detail_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    row["id"], row["timestamp"], row["actor_user_id"],
                    row["actor_email"], row["action"], row["resource_id"],
                    row["detail_json"],
                ),
            )
            self._conn.commit()
        return row

    def recent(
        self,
        limit: int = 100,
        *,
        action: str | None = None,
        actor_user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Newest-first listing with optional filters. Detail JSON is
        parsed back into a dict before returning."""
        clauses = []
        params: list[Any] = []
        if action:
            clauses.append("action = ?")
            params.append(action)
        if actor_user_id:
            clauses.append("actor_user_id = ?")
            params.append(actor_user_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        cursor = self._conn.execute(
            f"SELECT id, timestamp, actor_user_id, actor_email, action, "
            f"resource_id, detail_json FROM admin_actions {where} "
            f"ORDER BY timestamp DESC LIMIT ?",
            params,
        )
        result: list[dict[str, Any]] = []
        for row in cursor.fetchall():
            result.append({
                "id": row[0],
                "timestamp": row[1],
                "actor_user_id": row[2],
                "actor_email": row[3],
                "action": row[4],
                "resource_id": row[5],
                "detail": json.loads(row[6]),
            })
        return result

    def count(self) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM admin_actions"
        ).fetchone()[0]

    def prune(
        self,
        *,
        older_than_seconds: float,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Refinement #7 — drop admin-action rows older than the
        threshold. The retention guard for long-lived admin DBs.

        ``older_than_seconds`` is the **age** beyond which a row is
        eligible for pruning, not the cutoff timestamp itself. So
        ``older_than_seconds=86400`` keeps the last 24 hours.

        Returns ``{cutoff_ts, would_delete, deleted}``. Under
        ``dry_run=True`` the SELECT is executed but nothing is
        deleted (``deleted`` stays 0); the count is reported as
        ``would_delete``.

        Negative or zero ``older_than_seconds`` is a usage error
        (would delete everything including the row about to be
        written by the caller), so it raises ``ValueError``.
        """
        if older_than_seconds <= 0:
            raise ValueError(
                "older_than_seconds must be positive — "
                "refusing to prune the entire table"
            )
        cutoff = time.time() - older_than_seconds
        with self._lock:
            (would,) = self._conn.execute(
                "SELECT COUNT(*) FROM admin_actions WHERE timestamp < ?",
                (cutoff,),
            ).fetchone()
            deleted = 0
            if not dry_run and would:
                cur = self._conn.execute(
                    "DELETE FROM admin_actions WHERE timestamp < ?",
                    (cutoff,),
                )
                deleted = cur.rowcount or 0
                self._conn.commit()
        return {
            "cutoff_ts": cutoff,
            "would_delete": int(would),
            "deleted": int(deleted),
            "dry_run": dry_run,
        }

    def close(self) -> None:
        self._conn.close()


__all__ = ["AdminActionLog"]
