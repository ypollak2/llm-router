from __future__ import annotations

import json
import sqlite3
import time
import typing
from contextlib import closing
from pathlib import Path

from llm_router.control_plane.schemas import (
    InstanceHeartbeatRecord,
    TenantPolicyVersionRecord,
    TenantRecord,
)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS cp_tenants (
    tenant_id TEXT PRIMARY KEY,
    org_id TEXT,
    created_at REAL
);

CREATE TABLE IF NOT EXISTS cp_policy_versions (
    tenant_id TEXT,
    version INTEGER,
    yaml_text TEXT,
    normalized_json TEXT,
    actor TEXT,
    note TEXT,
    created_at REAL,
    PRIMARY KEY(tenant_id, version)
);

CREATE TABLE IF NOT EXISTS cp_tenant_active_policy (
    tenant_id TEXT PRIMARY KEY,
    version INTEGER
);

CREATE TABLE IF NOT EXISTS cp_instances (
    instance_id TEXT PRIMARY KEY,
    tenant_id TEXT,
    effective_version INTEGER,
    effective_digest TEXT,
    source TEXT,
    sidecar_version TEXT,
    last_apply_latency_ms REAL,
    last_seen_at REAL
);

CREATE TABLE IF NOT EXISTS cp_heartbeats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id TEXT,
    tenant_id TEXT,
    effective_version INTEGER,
    effective_digest TEXT,
    source TEXT,
    sidecar_version TEXT,
    last_apply_latency_ms REAL,
    seen_at REAL
);

CREATE TABLE IF NOT EXISTS cp_policy_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT,
    version INTEGER,
    digest TEXT,
    created_at REAL
);
"""


class ControlPlaneStore(typing.Protocol):
    def init_schema(self) -> None:
        ...

    def ensure_tenant(
        self, tenant_id: str, org_id: str | None = None
    ) -> TenantRecord:
        ...

    def append_policy_version(
        self,
        tenant_id: str,
        *,
        yaml_text: str,
        normalized_json: str,
        actor: str,
        note: str = "",
    ) -> TenantPolicyVersionRecord:
        ...

    def set_active_policy(self, tenant_id: str, version: int) -> None:
        ...

    def get_current_policy(
        self, tenant_id: str
    ) -> TenantPolicyVersionRecord | None:
        ...

    def record_heartbeat(
        self,
        *,
        instance_id: str,
        tenant_id: str,
        effective_version: int | None,
        effective_digest: str | None,
        source: str,
        sidecar_version: str = "",
        last_apply_latency_ms: float | None = None,
    ) -> InstanceHeartbeatRecord:
        ...

    def list_instances(self, tenant_id: str) -> list[InstanceHeartbeatRecord]:
        ...


class SqliteControlPlaneStore:
    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self._db_path = db_path
        self._conn = sqlite3.connect(
            str(db_path),
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self.init_schema()

    def init_schema(self) -> None:
        with self._conn:
            self._conn.executescript(_SCHEMA)

    def ensure_tenant(
        self, tenant_id: str, org_id: str | None = None
    ) -> TenantRecord:
        now = time.time()
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO cp_tenants (tenant_id, org_id, created_at) "
                "VALUES (?, ?, ?)",
                (tenant_id, org_id, now),
            )
        record = self._get_tenant(tenant_id)
        if record is None:
            raise ValueError(f"tenant missing after ensure_tenant: {tenant_id}")
        return record

    def append_policy_version(
        self,
        tenant_id: str,
        *,
        yaml_text: str,
        normalized_json: str,
        actor: str,
        note: str = "",
    ) -> TenantPolicyVersionRecord:
        self.ensure_tenant(tenant_id)
        normalized_payload = self._normalize_json_text(normalized_json)
        created_at = time.time()
        with self._conn:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS max_version "
                "FROM cp_policy_versions WHERE tenant_id = ?",
                (tenant_id,),
            ).fetchone()
            version = int(row["max_version"]) + 1
            self._conn.execute(
                "INSERT INTO cp_policy_versions ("
                "tenant_id, version, yaml_text, normalized_json, actor, note, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    tenant_id,
                    version,
                    yaml_text,
                    normalized_payload,
                    actor,
                    note,
                    created_at,
                ),
            )
        record = self._get_policy_version(tenant_id, version)
        if record is None:
            raise ValueError(
                f"policy version missing after append: {tenant_id}@{version}"
            )
        return record

    def set_active_policy(self, tenant_id: str, version: int) -> None:
        self.ensure_tenant(tenant_id)
        policy = self._get_policy_version(tenant_id, version)
        if policy is None:
            raise ValueError(
                f"policy version does not exist for tenant {tenant_id}: {version}"
            )
        with self._conn:
            self._conn.execute(
                "INSERT INTO cp_tenant_active_policy (tenant_id, version) "
                "VALUES (?, ?) "
                "ON CONFLICT(tenant_id) DO UPDATE SET version = excluded.version",
                (tenant_id, version),
            )

    def get_current_policy(
        self, tenant_id: str
    ) -> TenantPolicyVersionRecord | None:
        with closing(
            self._conn.execute(
                "SELECT pv.tenant_id, pv.version, pv.yaml_text, pv.normalized_json, "
                "pv.actor, pv.note, pv.created_at "
                "FROM cp_tenant_active_policy AS ap "
                "JOIN cp_policy_versions AS pv "
                "ON pv.tenant_id = ap.tenant_id AND pv.version = ap.version "
                "WHERE ap.tenant_id = ?",
                (tenant_id,),
            )
        ) as cursor:
            row = cursor.fetchone()
        if row is None:
            return None
        return self._tenant_policy_version_from_row(row)

    def record_heartbeat(
        self,
        *,
        instance_id: str,
        tenant_id: str,
        effective_version: int | None,
        effective_digest: str | None,
        source: str,
        sidecar_version: str = "",
        last_apply_latency_ms: float | None = None,
    ) -> InstanceHeartbeatRecord:
        self.ensure_tenant(tenant_id)
        now = time.time()
        with self._conn:
            self._conn.execute(
                "INSERT INTO cp_instances ("
                "instance_id, tenant_id, effective_version, effective_digest, source, "
                "sidecar_version, last_apply_latency_ms, last_seen_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(instance_id) DO UPDATE SET "
                "tenant_id = excluded.tenant_id, "
                "effective_version = excluded.effective_version, "
                "effective_digest = excluded.effective_digest, "
                "source = excluded.source, "
                "sidecar_version = excluded.sidecar_version, "
                "last_apply_latency_ms = excluded.last_apply_latency_ms, "
                "last_seen_at = excluded.last_seen_at",
                (
                    instance_id,
                    tenant_id,
                    effective_version,
                    effective_digest,
                    source,
                    sidecar_version,
                    last_apply_latency_ms,
                    now,
                ),
            )
            self._conn.execute(
                "INSERT INTO cp_heartbeats ("
                "instance_id, tenant_id, effective_version, effective_digest, source, "
                "sidecar_version, last_apply_latency_ms, seen_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    instance_id,
                    tenant_id,
                    effective_version,
                    effective_digest,
                    source,
                    sidecar_version,
                    last_apply_latency_ms,
                    now,
                ),
            )
        record = self._get_instance(instance_id)
        if record is None:
            raise ValueError(
                f"instance missing after record_heartbeat: {instance_id}"
            )
        return record

    def list_instances(self, tenant_id: str) -> list[InstanceHeartbeatRecord]:
        with closing(
            self._conn.execute(
                "SELECT instance_id, tenant_id, effective_version, effective_digest, "
                "source, sidecar_version, last_apply_latency_ms, last_seen_at "
                "FROM cp_instances WHERE tenant_id = ? "
                "ORDER BY last_seen_at DESC",
                (tenant_id,),
            )
        ) as cursor:
            rows = cursor.fetchall()
        return [self._instance_heartbeat_from_row(row) for row in rows]

    def close(self) -> None:
        self._conn.close()

    def _get_tenant(self, tenant_id: str) -> TenantRecord | None:
        with closing(
            self._conn.execute(
                "SELECT tenant_id, org_id, created_at FROM cp_tenants "
                "WHERE tenant_id = ?",
                (tenant_id,),
            )
        ) as cursor:
            row = cursor.fetchone()
        if row is None:
            return None
        return self._tenant_from_row(row)

    def _get_policy_version(
        self, tenant_id: str, version: int
    ) -> TenantPolicyVersionRecord | None:
        with closing(
            self._conn.execute(
                "SELECT tenant_id, version, yaml_text, normalized_json, actor, note, "
                "created_at FROM cp_policy_versions "
                "WHERE tenant_id = ? AND version = ?",
                (tenant_id, version),
            )
        ) as cursor:
            row = cursor.fetchone()
        if row is None:
            return None
        return self._tenant_policy_version_from_row(row)

    def _get_instance(self, instance_id: str) -> InstanceHeartbeatRecord | None:
        with closing(
            self._conn.execute(
                "SELECT instance_id, tenant_id, effective_version, effective_digest, "
                "source, sidecar_version, last_apply_latency_ms, last_seen_at "
                "FROM cp_instances WHERE instance_id = ?",
                (instance_id,),
            )
        ) as cursor:
            row = cursor.fetchone()
        if row is None:
            return None
        return self._instance_heartbeat_from_row(row)

    @staticmethod
    def _normalize_json_text(normalized_json: str) -> str:
        payload = json.loads(normalized_json)
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _tenant_from_row(row: sqlite3.Row) -> TenantRecord:
        return TenantRecord(
            tenant_id=str(row["tenant_id"]),
            org_id=typing.cast(typing.Optional[str], row["org_id"]),
            created_at=float(row["created_at"]),
        )

    @staticmethod
    def _tenant_policy_version_from_row(
        row: sqlite3.Row,
    ) -> TenantPolicyVersionRecord:
        return TenantPolicyVersionRecord(
            tenant_id=str(row["tenant_id"]),
            version=int(row["version"]),
            yaml_text=str(row["yaml_text"]),
            normalized_json=str(row["normalized_json"]),
            actor=str(row["actor"]),
            note=str(row["note"] or ""),
            created_at=float(row["created_at"]),
        )

    @staticmethod
    def _instance_heartbeat_from_row(
        row: sqlite3.Row,
    ) -> InstanceHeartbeatRecord:
        return InstanceHeartbeatRecord(
            instance_id=str(row["instance_id"]),
            tenant_id=str(row["tenant_id"]),
            effective_version=typing.cast(
                typing.Optional[int], row["effective_version"]
            ),
            effective_digest=typing.cast(
                typing.Optional[str], row["effective_digest"]
            ),
            source=str(row["source"]),
            sidecar_version=str(row["sidecar_version"] or ""),
            last_apply_latency_ms=typing.cast(
                typing.Optional[float], row["last_apply_latency_ms"]
            ),
            last_seen_at=float(row["last_seen_at"]),
        )


__all__ = ["ControlPlaneStore", "SqliteControlPlaneStore"]
