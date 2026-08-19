"""G-006-F2 / G-006-F3 runtime provider registry.

Sits between two consumers:

* ``src/llm_router/admin_api.py`` — admin endpoints mutate (disable /
  enable) and read (list disabled).
* ``src/llm_router/chain_builder.py`` — the routing path reads the
  disabled set when assembling a candidate chain; disabled
  providers' models drop out before the router sees them.

Two backends:

* **In-memory (default)** — ``db_path=None``. Pure-dict store, locked
  for thread-safety. Acceptable for single-process deployments and the
  test suite. Pre-G-006-F3 behaviour preserved when ``db_path`` is not
  supplied to ``__init__``.
* **SQLite-backed (G-006-F3)** — ``db_path=Path(...)``. Mutations
  write through to a small ``provider_state`` table and bump a
  ``registry_version`` counter atomically. Readers cache the last
  observed version; on every read the counter is cheaply re-checked
  and the in-memory cache is refreshed when it has advanced. This
  gives **cross-instance propagation by polling**: two admin-API
  instances pointing at the same SQLite file see each other's
  disable / enable within the next read on each side.

The polling design is deliberate. A true change feed (SQLite hook,
LISTEN/NOTIFY, file watcher) is overkill for the emergency-disable
use case where staleness budgets are seconds, not milliseconds, and
the read-path is hit on every routed turn anyway.

This module is intentionally tiny and dependency-free so the routing
core can import it without dragging in FastAPI / Pydantic.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


_SCHEMA = """
CREATE TABLE IF NOT EXISTS provider_state (
    provider TEXT PRIMARY KEY,
    disabled INTEGER NOT NULL,
    reason TEXT,
    disabled_at REAL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_provider_state_disabled
    ON provider_state(disabled);

-- Refinement #5 (G-006-F2 finisher): model-level disable. Provider
-- disable is the blunt instrument; model disable lets ops surgically
-- pull a single ``provider/model`` (e.g. when one model regresses
-- without taking down the rest of that provider's chain). Same
-- version-counter polling drives propagation; keys are full
-- ``provider/model`` strings.
CREATE TABLE IF NOT EXISTS model_state (
    model TEXT PRIMARY KEY,
    disabled INTEGER NOT NULL,
    reason TEXT,
    disabled_at REAL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_model_state_disabled
    ON model_state(disabled);

CREATE TABLE IF NOT EXISTS registry_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _read_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT value FROM registry_meta WHERE key = 'version'"
    ).fetchone()
    return int(row[0]) if row else 0


def _bump_version(conn: sqlite3.Connection) -> int:
    new_version = _read_version(conn) + 1
    conn.execute(
        "INSERT INTO registry_meta(key, value) VALUES ('version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(new_version),),
    )
    return new_version


class RuntimeProviderRegistry:
    """Registry of disabled providers.

    Holds the *intent* declared via the admin API; the routing path
    reads ``is_disabled`` at chain-build time and drops matching
    candidates. ``disable`` and ``enable`` are idempotent. Construct
    with ``db_path`` to persist + propagate across instances.
    """

    def __init__(
        self,
        db_path: Path | None = None,
        *,
        check_same_thread: bool = True,
    ) -> None:
        self._lock = threading.Lock()
        self._disabled: dict[str, dict[str, Any]] = {}
        # Refinement #5: separate map for disabled model ids
        # (``provider/model`` strings). Mirrors the provider-level
        # store exactly so the same version-counter polling drives
        # propagation for both.
        self._disabled_models: dict[str, dict[str, Any]] = {}
        self.db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._cached_version: int = 0
        if db_path is not None:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(
                str(db_path), check_same_thread=check_same_thread
            )
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
            self._reload_from_db()

    # ── Persistence-path helpers ────────────────────────────────────────

    def _reload_from_db(self) -> None:
        """Re-read the disabled set from SQLite into the in-memory cache.

        Caller must hold ``self._lock`` if invoking from a mutating path.
        Used both at construction and on every read when the version
        counter has advanced.
        """
        assert self._conn is not None
        self._disabled.clear()
        for row in self._conn.execute(
            "SELECT provider, reason, disabled_at FROM provider_state "
            "WHERE disabled = 1"
        ).fetchall():
            provider, reason, disabled_at = row
            self._disabled[provider] = {
                "provider": provider,
                "disabled": True,
                "reason": reason or "",
                "disabled_at": disabled_at,
            }
        # Refinement #5: mirror reload for the model-level table.
        self._disabled_models.clear()
        for row in self._conn.execute(
            "SELECT model, reason, disabled_at FROM model_state "
            "WHERE disabled = 1"
        ).fetchall():
            model, reason, disabled_at = row
            self._disabled_models[model] = {
                "model": model,
                "disabled": True,
                "reason": reason or "",
                "disabled_at": disabled_at,
            }
        self._cached_version = _read_version(self._conn)

    def _refresh_if_stale(self) -> None:
        """If another instance bumped the version since our last read,
        reload from SQLite. Called from every read-path."""
        if self._conn is None:
            return
        with self._lock:
            current = _read_version(self._conn)
            if current != self._cached_version:
                self._reload_from_db()

    # ── Public API ──────────────────────────────────────────────────────

    def disable(self, provider: str, *, reason: str) -> dict[str, Any]:
        """Mark ``provider`` disabled. Idempotent — re-disabling updates
        the reason + timestamp."""
        now = time.time()
        entry = {
            "provider": provider,
            "disabled": True,
            "reason": reason,
            "disabled_at": now,
        }
        with self._lock:
            self._disabled[provider] = entry
            if self._conn is not None:
                self._conn.execute(
                    "INSERT INTO provider_state "
                    "(provider, disabled, reason, disabled_at, updated_at) "
                    "VALUES (?, 1, ?, ?, ?) "
                    "ON CONFLICT(provider) DO UPDATE SET "
                    "disabled = 1, reason = excluded.reason, "
                    "disabled_at = excluded.disabled_at, "
                    "updated_at = excluded.updated_at",
                    (provider, reason, now, now),
                )
                self._cached_version = _bump_version(self._conn)
                self._conn.commit()
        return dict(entry)

    def enable(self, provider: str) -> dict[str, Any]:
        """Clear the disabled flag. Idempotent."""
        now = time.time()
        with self._lock:
            self._disabled.pop(provider, None)
            if self._conn is not None:
                self._conn.execute(
                    "INSERT INTO provider_state "
                    "(provider, disabled, reason, disabled_at, updated_at) "
                    "VALUES (?, 0, NULL, NULL, ?) "
                    "ON CONFLICT(provider) DO UPDATE SET "
                    "disabled = 0, reason = NULL, disabled_at = NULL, "
                    "updated_at = excluded.updated_at",
                    (provider, now),
                )
                self._cached_version = _bump_version(self._conn)
                self._conn.commit()
        return {"provider": provider, "disabled": False}

    def is_disabled(self, provider: str) -> bool:
        """Fast-path read for the routing layer.

        Hit on every routed turn; the SQLite version check is the only
        extra cost vs the pure-memory path. The check is a single
        indexed lookup so the overhead is microseconds, dwarfed by the
        rest of the routing pipeline.
        """
        self._refresh_if_stale()
        with self._lock:
            return provider in self._disabled

    def list_disabled(self) -> list[dict[str, Any]]:
        """Snapshot for the admin API + ops dashboards."""
        self._refresh_if_stale()
        with self._lock:
            return [dict(v) for v in self._disabled.values()]

    # ── Model-level disable (Refinement #5 / G-006-F2 finisher) ────────

    def disable_model(self, model: str, *, reason: str) -> dict[str, Any]:
        """Mark a single ``provider/model`` id disabled. Mirrors
        ``disable`` for providers; same persistence + propagation."""
        now = time.time()
        entry = {
            "model": model,
            "disabled": True,
            "reason": reason,
            "disabled_at": now,
        }
        with self._lock:
            self._disabled_models[model] = entry
            if self._conn is not None:
                self._conn.execute(
                    "INSERT INTO model_state "
                    "(model, disabled, reason, disabled_at, updated_at) "
                    "VALUES (?, 1, ?, ?, ?) "
                    "ON CONFLICT(model) DO UPDATE SET "
                    "disabled = 1, reason = excluded.reason, "
                    "disabled_at = excluded.disabled_at, "
                    "updated_at = excluded.updated_at",
                    (model, reason, now, now),
                )
                self._cached_version = _bump_version(self._conn)
                self._conn.commit()
        return dict(entry)

    def enable_model(self, model: str) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            self._disabled_models.pop(model, None)
            if self._conn is not None:
                self._conn.execute(
                    "INSERT INTO model_state "
                    "(model, disabled, reason, disabled_at, updated_at) "
                    "VALUES (?, 0, NULL, NULL, ?) "
                    "ON CONFLICT(model) DO UPDATE SET "
                    "disabled = 0, reason = NULL, disabled_at = NULL, "
                    "updated_at = excluded.updated_at",
                    (model, now),
                )
                self._cached_version = _bump_version(self._conn)
                self._conn.commit()
        return {"model": model, "disabled": False}

    def is_model_disabled(self, model: str) -> bool:
        """Fast-path read for ``chain_builder`` — does this exact
        ``provider/model`` id appear in the disabled set?"""
        self._refresh_if_stale()
        with self._lock:
            return model in self._disabled_models

    def list_disabled_models(self) -> list[dict[str, Any]]:
        self._refresh_if_stale()
        with self._lock:
            return [dict(v) for v in self._disabled_models.values()]

    def clear(self) -> None:
        """Drop all entries — used by tests; not exposed via the admin
        API. For the SQLite-backed mode, also truncates the table and
        bumps the version counter so peer instances see the reset."""
        with self._lock:
            self._disabled.clear()
            self._disabled_models.clear()
            if self._conn is not None:
                self._conn.execute("DELETE FROM provider_state")
                self._conn.execute("DELETE FROM model_state")
                self._cached_version = _bump_version(self._conn)
                self._conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


_global_registry: RuntimeProviderRegistry | None = None


def get_global_registry() -> RuntimeProviderRegistry:
    """Return the process-singleton registry, constructing on first call.

    G-006-F3: honour ``LLM_ROUTER_PROVIDER_REGISTRY_PATH`` for SQLite
    persistence. When unset, the registry stays pure-memory (backward
    compat). Both ``admin_api`` and ``chain_builder`` route through
    this accessor so they observe the same disabled set.

    Tests that need isolation can monkeypatch ``_global_registry`` to
    a fresh instance.
    """
    global _global_registry
    if _global_registry is None:
        raw_path = os.environ.get("LLM_ROUTER_PROVIDER_REGISTRY_PATH")
        db_path = Path(raw_path) if raw_path else None
        _global_registry = RuntimeProviderRegistry(db_path=db_path)
    return _global_registry


__all__ = ["RuntimeProviderRegistry", "get_global_registry"]
