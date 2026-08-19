"""G-007 versioned org policy storage.

The pre-G-007 ``POST /v1/admin/policy`` endpoint atomically wrote a
single YAML file to disk. No history, no rollback, no audit of "who
pushed what when". This module replaces that with an append-only
versioned store: every push creates a new ``policy_versions`` row,
the latest non-rolled-back row is the *active* version, and an
operator can roll back to any prior version by pointing the active
marker at it.

Design choices:

* **Append-only.** Old versions are never edited or deleted. Rollback
  is a new row that copies a prior YAML payload, not a mutation of
  the past. Audit + diffability come for free.
* **One-author timeline.** Versions form a strict monotonic sequence
  (1, 2, 3, ...). No branching. Multi-tenant policies are a separate
  problem; this slice models a single org's history.
* **Validation gate before persist.** The same plaintext-secret scan
  and YAML parse that the prior endpoint enforced still runs *before*
  the row is inserted. A bad payload never becomes part of history.
* **Provenance.** Every row carries ``actor_user_id`` + ``actor_email``
  pulled from the authenticated principal. Pairs with the admin-action
  log (G-006-F5) for cross-correlation.

Schema:

* ``policy_versions``:
  * ``version`` (int PK, monotonically assigned by SQLite max+1)
  * ``yaml_text`` (TEXT — exact bytes the operator pushed)
  * ``actor_user_id`` / ``actor_email``
  * ``note`` (free-form rationale, optional)
  * ``parent_version`` (for rollback: the version we copied from)
  * ``created_at``
* ``policy_active``: single row keyed on ``id = 'singleton'`` with
  ``version`` pointing at the currently-active row. Rollback rewrites
  this pointer; push appends and rewrites the pointer.

See: ``docs/audit/post-remediation/GAP_ANALYSIS.md`` G-007.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


_SCHEMA = """
CREATE TABLE IF NOT EXISTS policy_versions (
    version INTEGER PRIMARY KEY AUTOINCREMENT,
    yaml_text TEXT NOT NULL,
    actor_user_id TEXT NOT NULL,
    actor_email TEXT NOT NULL,
    note TEXT,
    parent_version INTEGER,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_policy_versions_created
    ON policy_versions(created_at);
CREATE INDEX IF NOT EXISTS idx_policy_versions_actor
    ON policy_versions(actor_user_id);

CREATE TABLE IF NOT EXISTS policy_active (
    id TEXT PRIMARY KEY,
    version INTEGER NOT NULL REFERENCES policy_versions(version)
);
"""


class PolicyVersionNotFound(KeyError):
    """Raised when a target version isn't in the store."""


class PolicyValidationError(ValueError):
    """Raised when the supplied YAML fails validation before persist."""


class PolicyVersionStore:
    """SQLite-backed versioned org-policy store."""

    def __init__(
        self,
        db_path: Path | None = None,
        *,
        check_same_thread: bool = True,
    ) -> None:
        self.db_path = db_path or Path(
            os.environ.get("LLM_ROUTER_POLICY_STORE_PATH")
            or (Path.home() / ".llm-router" / "policy_versions.db")
        )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.db_path), check_same_thread=check_same_thread
        )
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._lock = threading.Lock()

    # ── Validation gate ─────────────────────────────────────────────────

    @staticmethod
    def validate(yaml_text: str) -> None:
        """Run the same plaintext-secret + YAML-parse gate the legacy
        endpoint used. Raises ``PolicyValidationError`` on failure."""
        import tempfile
        from llm_router.org_policy import OrgPolicy, PlaintextSecretInPolicy

        # OrgPolicy.load reads from disk; write to a throwaway tempfile
        # so the scan + parse mirror exactly the prior contract.
        with tempfile.NamedTemporaryFile(
            mode="w", delete=False, suffix=".yaml", encoding="utf-8"
        ) as fh:
            fh.write(yaml_text)
            tmp_path = Path(fh.name)
        try:
            try:
                OrgPolicy.load(tmp_path)
            except PlaintextSecretInPolicy as exc:
                raise PolicyValidationError(
                    f"policy contains plaintext secrets: {exc}"
                )
            except Exception as exc:
                raise PolicyValidationError(
                    f"policy validation failed: {exc}"
                )
        finally:
            tmp_path.unlink(missing_ok=True)

    # ── Mutations ───────────────────────────────────────────────────────

    def push(
        self,
        *,
        yaml_text: str,
        actor_user_id: str,
        actor_email: str,
        note: str | None = None,
        parent_version: int | None = None,
    ) -> dict[str, Any]:
        """Validate + persist a new version, mark it active. Returns
        the persisted record."""
        self.validate(yaml_text)
        now = time.time()
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO policy_versions "
                "(yaml_text, actor_user_id, actor_email, note, "
                "parent_version, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (yaml_text, actor_user_id, actor_email, note,
                 parent_version, now),
            )
            version = cursor.lastrowid
            self._conn.execute(
                "INSERT INTO policy_active(id, version) "
                "VALUES ('singleton', ?) "
                "ON CONFLICT(id) DO UPDATE SET version = excluded.version",
                (version,),
            )
            self._conn.commit()
        return self._fetch_meta(int(version))

    def rollback(
        self,
        *,
        target_version: int,
        actor_user_id: str,
        actor_email: str,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Append a new version whose YAML matches ``target_version``
        and mark it active. The history shows: push v1, push v2, push
        v3, push v4-rolled-back-from-v2."""
        target = self.get(target_version)
        rolled = self.push(
            yaml_text=target["yaml_text"],
            actor_user_id=actor_user_id,
            actor_email=actor_email,
            note=note or f"rollback to version {target_version}",
            parent_version=target_version,
        )
        return rolled

    # ── Reads ───────────────────────────────────────────────────────────

    def active_version(self) -> int | None:
        row = self._conn.execute(
            "SELECT version FROM policy_active WHERE id = 'singleton'"
        ).fetchone()
        return int(row[0]) if row else None

    def get(self, version: int) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT version, yaml_text, actor_user_id, actor_email, "
            "note, parent_version, created_at FROM policy_versions "
            "WHERE version = ?",
            (version,),
        ).fetchone()
        if not row:
            raise PolicyVersionNotFound(f"version {version}")
        return {
            "version": int(row[0]),
            "yaml_text": row[1],
            "actor_user_id": row[2],
            "actor_email": row[3],
            "note": row[4],
            "parent_version": row[5],
            "created_at": row[6],
        }

    def get_active(self) -> dict[str, Any] | None:
        v = self.active_version()
        return self.get(v) if v is not None else None

    def list_versions(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Newest-first metadata listing. YAML body is *not* included
        — callers fetch a single version's body via ``get``."""
        return [
            self._fetch_meta(int(row[0]))
            for row in self._conn.execute(
                "SELECT version FROM policy_versions "
                "ORDER BY version DESC LIMIT ?",
                (limit,),
            ).fetchall()
        ]

    def _fetch_meta(self, version: int) -> dict[str, Any]:
        full = self.get(version)
        # Strip the heavy YAML body from list views.
        full_meta = {k: v for k, v in full.items() if k != "yaml_text"}
        full_meta["yaml_bytes"] = len(full["yaml_text"])
        full_meta["is_active"] = version == self.active_version()
        return full_meta

    def close(self) -> None:
        self._conn.close()


__all__ = [
    "PolicyValidationError",
    "PolicyVersionNotFound",
    "PolicyVersionStore",
]
