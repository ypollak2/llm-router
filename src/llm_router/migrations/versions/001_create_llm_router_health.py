"""First versioned migration — creates a ``llm_router_health`` table.

This migration is intentionally additive: it doesn't touch any existing
table, so applying or rolling it back can't lose data. It exists to
prove the framework end-to-end with a real schema change worth shipping.

``llm_router_health`` records one row per ``llm_router doctor`` run (or any
caller that wants to log a structured health-check result). Downstream
dashboards can plot the health timeline without re-parsing logs.

Columns:

* ``id`` — autoincrement PK
* ``checked_at`` — ISO-8601 UTC of the health check
* ``component`` — short label (``hooks``, ``providers``, ``classifier``…)
* ``status`` — one of ``ok | degraded | failed``
* ``detail`` — free-form message (truncated to 4 KB at the caller)
* ``duration_ms`` — how long the check took
"""

from __future__ import annotations

import sqlite3

version = "001"
name = "create-llm_router-health"


def up(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS llm_router_health (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            checked_at  TEXT NOT NULL,
            component   TEXT NOT NULL,
            status      TEXT NOT NULL CHECK (status IN ('ok','degraded','failed')),
            detail      TEXT,
            duration_ms INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_llm_router_health_checked_at "
        "ON llm_router_health(checked_at DESC)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_llm_router_health_component_status "
        "ON llm_router_health(component, status, checked_at DESC)"
    )


def down(connection: sqlite3.Connection) -> None:
    connection.execute("DROP INDEX IF EXISTS idx_llm_router_health_component_status")
    connection.execute("DROP INDEX IF EXISTS idx_llm_router_health_checked_at")
    connection.execute("DROP TABLE IF EXISTS llm_router_health")
