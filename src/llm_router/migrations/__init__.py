"""Versioned schema migrations for LLM Router's SQLite stores.

Background
----------
Pre-0.1, schema changes landed via :func:`llm_router.cost._safe_migrate` —
a list of ``ALTER TABLE`` statements applied idempotently on every
connection. That works for additive columns but breaks down for:

* Tracking which migrations have actually applied (currently
  unknowable — we re-run every ALTER on every connect)
* Reversing a change (no down-path means a bad migration is permanent)
* Destructive changes (renames, type changes, foreign-key adjustments)

This module adds an Alembic-style framework that coexists with the
legacy ``_safe_migrate`` list. New schema work should land as a
versioned migration here; the old list stays in place for compatibility
and gets folded into the new system gradually.

API surface
-----------
* :class:`Migration` — protocol implemented by each
  ``versions/NNN_*.py`` module.
* :func:`discover` — walks the versions/ directory and returns the
  ordered migration list.
* :func:`status` — returns which migrations are pending vs applied for
  the database at ``db_path``.
* :func:`apply` — applies all pending migrations in order, tracking
  each in the ``llm_router_migrations`` ledger.
* :func:`rollback` — reverses the last-applied migration (or down to a
  target version) by calling each migration's ``down`` function.

CLI is in :mod:`llm_router.commands.migrate` — ``llm_router migrate status`` /
``up`` / ``down``.
"""

from __future__ import annotations

import hashlib
import importlib.util
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "Migration",
    "MigrationRecord",
    "discover",
    "status",
    "apply",
    "rollback",
    "ensure_ledger",
    "compute_checksum",
]


# ── Protocol ──────────────────────────────────────────────────────────────


@runtime_checkable
class Migration(Protocol):
    """A versioned schema migration.

    Each ``versions/NNN_*.py`` file must export module-level names that
    match this protocol. The ``up`` and ``down`` functions receive an
    open ``sqlite3.Connection`` and may execute any number of statements;
    the framework commits the transaction after a successful ``up``.

    ``down`` is optional but strongly recommended — without it a
    rollback for this migration will raise. We still apply such
    migrations, but flag them in ``status`` so authors see the gap.
    """

    version: str  # zero-padded, e.g. "001", "002", "010"
    name: str     # short kebab-case label, e.g. "add-correlation-id"

    def up(self, connection: sqlite3.Connection) -> None: ...

    # ``down`` is intentionally not part of the Protocol so missing
    # implementations are caught at rollback time, not import time.


# ── Records ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MigrationRecord:
    """One row in the ``llm_router_migrations`` ledger.

    ``checksum`` is the SHA-256 of the up function's source so a
    silent edit to an already-applied migration is detectable. Drift
    here is a strong signal that someone rewrote history rather than
    issuing a follow-up migration.
    """

    version: str
    name: str
    applied_at: str           # ISO-8601 UTC
    checksum: str             # 64-char hex SHA-256
    duration_ms: int


# ── Ledger ───────────────────────────────────────────────────────────────


_LEDGER_TABLE = "llm_router_migrations"


def ensure_ledger(connection: sqlite3.Connection) -> None:
    """Idempotently create the ``llm_router_migrations`` ledger table."""
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_LEDGER_TABLE} (
            version     TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            applied_at  TEXT NOT NULL,
            checksum    TEXT NOT NULL,
            duration_ms INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    connection.commit()


def _read_ledger(connection: sqlite3.Connection) -> dict[str, MigrationRecord]:
    ensure_ledger(connection)
    rows = connection.execute(
        f"SELECT version, name, applied_at, checksum, duration_ms "
        f"FROM {_LEDGER_TABLE} ORDER BY version"
    ).fetchall()
    return {
        v: MigrationRecord(version=v, name=n, applied_at=a, checksum=c, duration_ms=d)
        for v, n, a, c, d in rows
    }


def _insert_record(connection: sqlite3.Connection, record: MigrationRecord) -> None:
    connection.execute(
        f"INSERT INTO {_LEDGER_TABLE} "
        f"(version, name, applied_at, checksum, duration_ms) "
        f"VALUES (?, ?, ?, ?, ?)",
        (record.version, record.name, record.applied_at,
         record.checksum, record.duration_ms),
    )


def _delete_record(connection: sqlite3.Connection, version: str) -> None:
    connection.execute(
        f"DELETE FROM {_LEDGER_TABLE} WHERE version = ?", (version,)
    )


# ── Discovery ────────────────────────────────────────────────────────────


_VERSIONS_DIR = Path(__file__).resolve().parent / "versions"


def discover(versions_dir: Path | None = None) -> list[Any]:
    """Return migration modules from ``versions/`` ordered by version.

    Each ``*.py`` file is loaded by absolute path so the framework
    doesn't require the migrations dir to be a Python package
    (avoids ``__init__.py`` clutter and accidental cross-imports).
    Modules without both ``version`` and ``name`` attributes are
    skipped with a warning — keeps the directory tolerant of
    fixtures and scratch files.
    """
    root = versions_dir or _VERSIONS_DIR
    if not root.is_dir():
        return []

    discovered: list[Any] = []
    for path in sorted(root.glob("*.py")):
        if path.name.startswith("_"):
            continue
        spec = importlib.util.spec_from_file_location(
            f"llm_router_migration_{path.stem}", path
        )
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if not (hasattr(module, "version") and hasattr(module, "name")
                and hasattr(module, "up")):
            continue
        discovered.append(module)

    discovered.sort(key=lambda m: str(m.version))
    return discovered


def compute_checksum(migration: Any) -> str:
    """SHA-256 of the migration's source file — used to detect drift.

    Hashing the file (not just the ``up`` callable) catches changes
    to module-level helpers that ``up`` calls into. False positives
    on whitespace-only edits are acceptable — those are also drift.
    """
    import inspect
    src = inspect.getsource(inspect.getmodule(migration))
    return hashlib.sha256(src.encode("utf-8")).hexdigest()


# ── Status / apply / rollback ────────────────────────────────────────────


@dataclass(frozen=True)
class StatusReport:
    """Summary of the migration ledger versus the discovered scripts."""

    applied: list[MigrationRecord] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)       # versions only
    missing_down: list[str] = field(default_factory=list)  # applied w/o down
    drifted: list[str] = field(default_factory=list)        # checksum mismatch


def status(connection: sqlite3.Connection,
           versions_dir: Path | None = None) -> StatusReport:
    """Compare what the ledger says with what's on disk.

    Surfaces three pathological states:

    * **pending** — migrations whose script exists but the ledger
      hasn't recorded them yet. Run ``apply`` to land them.
    * **missing_down** — applied migrations whose script doesn't
      expose a ``down`` function. Rollback past these will fail.
    * **drifted** — applied migration's source has changed since it
      ran. Indicates someone edited history; usually wrong.
    """
    discovered = discover(versions_dir)
    ledger = _read_ledger(connection)

    applied = list(ledger.values())
    pending = [str(m.version) for m in discovered if str(m.version) not in ledger]

    missing_down: list[str] = []
    drifted: list[str] = []
    by_version = {str(m.version): m for m in discovered}
    for version, record in ledger.items():
        mod = by_version.get(version)
        if mod is None:
            # Applied migration whose file vanished; treat as drift.
            drifted.append(version)
            continue
        if not hasattr(mod, "down"):
            missing_down.append(version)
        if compute_checksum(mod) != record.checksum:
            drifted.append(version)

    return StatusReport(
        applied=applied,
        pending=pending,
        missing_down=missing_down,
        drifted=drifted,
    )


def apply(connection: sqlite3.Connection,
          versions_dir: Path | None = None,
          *,
          target: str | None = None) -> list[MigrationRecord]:
    """Apply all pending migrations up to ``target`` (inclusive).

    Each migration runs inside a single transaction; failure rolls back
    the partial change and leaves the ledger untouched, so retry is
    safe. Returns the records that were freshly inserted.

    ``target`` lets callers pin to a specific version (useful for
    bisecting a bad migration in CI).
    """
    ensure_ledger(connection)
    discovered = discover(versions_dir)
    ledger = _read_ledger(connection)

    newly_applied: list[MigrationRecord] = []
    for module in discovered:
        version = str(module.version)
        if version in ledger:
            continue
        if target is not None and version > target:
            break
        started = time.time()
        try:
            module.up(connection)
        except Exception:
            connection.rollback()
            raise
        connection.commit()
        record = MigrationRecord(
            version=version,
            name=str(module.name),
            applied_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            checksum=compute_checksum(module),
            duration_ms=int((time.time() - started) * 1000),
        )
        _insert_record(connection, record)
        connection.commit()
        newly_applied.append(record)

    return newly_applied


def rollback(connection: sqlite3.Connection,
             versions_dir: Path | None = None,
             *,
             target: str | None = None,
             steps: int | None = 1) -> list[str]:
    """Roll back applied migrations.

    By default rolls back ``steps=1`` migrations (the most recent).
    Pass ``target="000"`` to roll back to the empty schema, or
    ``target="003"`` to bring the database down to having migrations
    001–003 applied (i.e. roll back everything strictly newer).

    Returns the version strings rolled back, in the order they were
    reversed (newest first).

    Raises ``RuntimeError`` if any in-scope migration lacks a ``down``
    function — better to fail loudly than to leave the database in an
    inconsistent state.
    """
    ensure_ledger(connection)
    discovered = discover(versions_dir)
    ledger = _read_ledger(connection)

    discovered_by_version = {str(m.version): m for m in discovered}

    applied_versions = sorted(ledger.keys(), reverse=True)  # newest first
    to_reverse: list[str] = []
    for version in applied_versions:
        if target is not None and version <= target:
            break
        to_reverse.append(version)
        if target is None and steps is not None and len(to_reverse) >= steps:
            break

    # Validate downs exist before doing anything destructive.
    for version in to_reverse:
        mod = discovered_by_version.get(version)
        if mod is None:
            raise RuntimeError(
                f"migration {version} is applied but its script is missing — "
                f"cannot rollback safely"
            )
        if not hasattr(mod, "down"):
            raise RuntimeError(
                f"migration {version} ({ledger[version].name}) has no down() — "
                f"refusing to rollback"
            )

    reversed_versions: list[str] = []
    for version in to_reverse:
        mod = discovered_by_version[version]
        try:
            mod.down(connection)
        except Exception:
            connection.rollback()
            raise
        connection.commit()
        _delete_record(connection, version)
        connection.commit()
        reversed_versions.append(version)

    return reversed_versions
