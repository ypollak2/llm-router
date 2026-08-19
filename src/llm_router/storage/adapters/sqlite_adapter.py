"""SQLite adapter for audit log storage (~/.llm-router/audit.db).

Implements tamper-evident hash chain verification.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from llm_router.sqlite_wal import enable_wal



_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_events (
    id TEXT PRIMARY KEY,
    timestamp REAL NOT NULL,
    type TEXT NOT NULL,
    severity TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    actor_email TEXT NOT NULL,
    org_id TEXT NOT NULL,
    resource TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash_hex TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_events(actor_id);
CREATE INDEX IF NOT EXISTS idx_audit_type ON audit_events(type);
CREATE INDEX IF NOT EXISTS idx_audit_org ON audit_events(org_id);
"""


class SqliteAdapter:
    """Persist audit events in SQLite with tamper-evident hash chain."""

    def __init__(self, db_path: Path):
        """Initialize adapter for SQLite database.

        Args:
            db_path: Path to .db file (e.g., ~/.llm-router/audit.db)
        """
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), timeout=5.0)
        # RED5-01 (P0): third copy of the cold-start bug, and the worst-ordered
        # of the three. WAL was enabled AFTER executescript, so schema creation —
        # the statement most likely to contend on a cold start — ran with no WAL
        # and the 5s default timeout. enable_wal() raises the busy timeout first,
        # which is precisely why it must come first.
        enable_wal(self._conn, label="sqlite_adapter")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def read(self) -> list[dict] | None:
        """Read all audit events from database.

        Returns:
            List of event dicts, or None on error.
        """
        try:
            cursor = self._conn.execute(
                "SELECT id, timestamp, type, severity, actor_id, actor_email, "
                "org_id, resource, action, detail, prev_hash, hash_hex "
                "FROM audit_events ORDER BY timestamp ASC"
            )
            cols = [
                "id", "timestamp", "type", "severity", "actor_id", "actor_email",
                "org_id", "resource", "action", "detail", "prev_hash", "hash_hex"
            ]
            return [dict(zip(cols, row)) for row in cursor.fetchall()]
        except (sqlite3.Error, OSError):
            return None

    def write(self, data: dict, atomic: bool = True) -> None:
        """Not supported for SQLite; use append() instead."""
        raise NotImplementedError("SQLite adapter uses append() for events")

    def append(self, data: dict) -> None:
        """Append an audit event to the database.

        Args:
            data: AuditEvent as dict with hash fields

        Raises:
            sqlite3.Error: On database error
        """
        self._conn.execute(
            "INSERT INTO audit_events "
            "(id, timestamp, type, severity, actor_id, actor_email, org_id, resource, "
            "action, detail, prev_hash, hash_hex) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                data["id"],
                data["timestamp"],
                data["type"],
                data["severity"],
                data["actor_id"],
                data["actor_email"],
                data["org_id"],
                data["resource"],
                data["action"],
                json.dumps(data.get("detail", {})),
                data["prev_hash"],
                data["hash_hex"],
            ),
        )
        self._conn.commit()

    def verify_integrity(self) -> tuple[bool, str]:
        """Verify hash chain integrity (no tampering).

        Returns:
            (is_valid, explanation) tuple. If invalid, explanation contains
            the ID of the first corrupted event.
        """
        try:
            cursor = self._conn.execute(
                "SELECT id, timestamp, type, severity, actor_id, actor_email, "
                "org_id, resource, action, detail, prev_hash, hash_hex "
                "FROM audit_events ORDER BY timestamp ASC"
            )
            prev_hash = ""
            for row in cursor.fetchall():
                (
                    id_, ts, type_, severity, actor_id, actor_email, org_id,
                    resource, action, detail_json, prev_hash_stored, hash_hex
                ) = row

                # Reconstruct canonical payload
                detail = json.loads(detail_json) if detail_json else {}
                payload = json.dumps({
                    "id": id_,
                    "timestamp": ts,
                    "type": type_,
                    "severity": severity,
                    "actor_id": actor_id,
                    "actor_email": actor_email,
                    "org_id": org_id,
                    "resource": resource,
                    "action": action,
                    "detail": detail,
                }, sort_keys=True, separators=(",", ":"))

                # Verify prev_hash matches
                if prev_hash_stored != prev_hash:
                    return False, f"broken_chain_at_{id_}"

                # Verify hash_hex
                expected_hash = hashlib.sha256(
                    (prev_hash + payload).encode("utf-8")
                ).hexdigest()
                if hash_hex != expected_hash:
                    return False, f"tampered_at_{id_}"

                prev_hash = hash_hex

            return True, "intact"
        except Exception as e:
            return False, f"verification_error: {e}"

    def export(self, format: str) -> str:
        """Export audit log in specified format.

        Args:
            format: "json", "csv", or "cef"

        Returns:
            Formatted string
        """
        if format == "json":
            data = self.read()
            return json.dumps(data, indent=2)
        elif format == "csv":
            # Simple CSV export
            rows = self.read()
            if not rows:
                return ""
            import csv
            import io
            output = io.StringIO()
            writer = csv.DictWriter(output, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
            return output.getvalue()
        elif format == "cef":
            # CEF (Common Event Format) for SIEMs
            rows = self.read()
            lines = []
            for row in rows:
                # Basic CEF format
                cef = (
                    f"CEF:0|llm_router|audit|1.0|{row['type']}|{row['action']}|{row['severity']}|"
                    f"rt={int(row['timestamp']*1000)} "
                    f"src={row['actor_id']} "
                    f"suser={row['actor_email']} "
                    f"dvc={row['org_id']} "
                    f"fname={row['resource']}"
                )
                lines.append(cef)
            return "\n".join(lines)
        else:
            raise ValueError(f"Unknown format: {format}")

    def __del__(self):
        """Close database connection."""
        if hasattr(self, "_conn"):
            self._conn.close()
