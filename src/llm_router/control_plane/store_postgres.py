from __future__ import annotations

import json
import os
import time
import typing

from llm_router.control_plane.schemas import (
    InstanceHeartbeatRecord,
    TenantPolicyVersionRecord,
    TenantRecord,
)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_router_cp_tenants (
    tenant_id TEXT PRIMARY KEY,
    org_id TEXT,
    created_at DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS llm_router_cp_policy_versions (
    tenant_id TEXT,
    version INTEGER,
    yaml_text TEXT,
    normalized_json TEXT,
    actor TEXT,
    note TEXT,
    created_at DOUBLE PRECISION,
    PRIMARY KEY(tenant_id, version)
);

CREATE TABLE IF NOT EXISTS llm_router_cp_tenant_active_policy (
    tenant_id TEXT PRIMARY KEY,
    version INTEGER
);

CREATE TABLE IF NOT EXISTS llm_router_cp_instances (
    instance_id TEXT PRIMARY KEY,
    tenant_id TEXT,
    effective_version INTEGER,
    effective_digest TEXT,
    source TEXT,
    sidecar_version TEXT,
    last_apply_latency_ms DOUBLE PRECISION,
    last_seen_at DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS llm_router_cp_heartbeats (
    id BIGSERIAL PRIMARY KEY,
    instance_id TEXT,
    tenant_id TEXT,
    effective_version INTEGER,
    effective_digest TEXT,
    source TEXT,
    sidecar_version TEXT,
    last_apply_latency_ms DOUBLE PRECISION,
    seen_at DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS llm_router_cp_policy_notifications (
    id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT,
    version INTEGER,
    digest TEXT,
    created_at DOUBLE PRECISION
);
"""


class PostgresControlPlaneStore:
    def __init__(self, dsn: str | None = None) -> None:
        try:
            import psycopg
        except ImportError as err:  # pragma: no cover - import guarded
            raise RuntimeError(
                "PostgresControlPlaneStore requires the 'postgres' extra: "
                "pip install 'llm_router[postgres]'"
            ) from err
        self._psycopg = psycopg
        self._dsn = dsn or os.environ.get("LLM_ROUTER_CP_POSTGRES_DSN", "")
        if not self._dsn:
            raise RuntimeError(
                "PostgresControlPlaneStore requires a DSN; set "
                "LLM_ROUTER_CP_POSTGRES_DSN or pass dsn=..."
            )
        self._conn = psycopg.connect(self._dsn)
        self.init_schema()

    def init_schema(self) -> None:
        with self._conn.cursor() as cur:
            cur.execute(_SCHEMA)
        self._conn.commit()

    def ensure_tenant(
        self, tenant_id: str, org_id: str | None = None
    ) -> TenantRecord:
        now = time.time()
        with self._conn.transaction():
            with self._conn.cursor() as cur:
                self._ensure_tenant(cur, tenant_id, org_id, now)
                row = self._fetch_tenant(cur, tenant_id)
        if row is None:
            raise ValueError(f"tenant missing after ensure_tenant: {tenant_id}")
        return self._tenant_from_row(row)

    def append_policy_version(
        self,
        tenant_id: str,
        *,
        yaml_text: str,
        normalized_json: str,
        actor: str,
        note: str = "",
    ) -> TenantPolicyVersionRecord:
        normalized_payload = self._normalize_json_text(normalized_json)
        created_at = time.time()
        with self._conn.transaction():
            with self._conn.cursor() as cur:
                self._ensure_tenant(cur, tenant_id, None, created_at)
                cur.execute(
                    "SELECT COALESCE(MAX(version), 0) "
                    "FROM llm_router_cp_policy_versions "
                    "WHERE tenant_id = %s",
                    (tenant_id,),
                )
                row = cur.fetchone()
                version = int(row[0]) + 1 if row is not None else 1
                cur.execute(
                    "INSERT INTO llm_router_cp_policy_versions ("
                    "tenant_id, version, yaml_text, normalized_json, actor, note, "
                    "created_at"
                    ") VALUES (%s, %s, %s, %s, %s, %s, %s)",
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
                record_row = self._fetch_policy_version(cur, tenant_id, version)
        if record_row is None:
            raise ValueError(
                f"policy version missing after append: {tenant_id}@{version}"
            )
        return self._tenant_policy_version_from_row(record_row)

    def set_active_policy(self, tenant_id: str, version: int) -> None:
        self.ensure_tenant(tenant_id)
        policy = self._get_policy_version(tenant_id, version)
        if policy is None:
            raise ValueError(
                f"policy version does not exist for tenant {tenant_id}: {version}"
            )
        with self._conn.transaction():
            with self._conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO llm_router_cp_tenant_active_policy "
                    "(tenant_id, version) VALUES (%s, %s) "
                    "ON CONFLICT(tenant_id) DO UPDATE SET "
                    "version = excluded.version",
                    (tenant_id, version),
                )

    def get_current_policy(
        self, tenant_id: str
    ) -> TenantPolicyVersionRecord | None:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT pv.tenant_id, pv.version, pv.yaml_text, pv.normalized_json, "
                "pv.actor, pv.note, pv.created_at "
                "FROM llm_router_cp_tenant_active_policy AS ap "
                "JOIN llm_router_cp_policy_versions AS pv "
                "ON pv.tenant_id = ap.tenant_id AND pv.version = ap.version "
                "WHERE ap.tenant_id = %s",
                (tenant_id,),
            )
            row = cur.fetchone()
        self._conn.rollback()
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
        now = time.time()
        with self._conn.transaction():
            with self._conn.cursor() as cur:
                self._ensure_tenant(cur, tenant_id, None, now)
                cur.execute(
                    "INSERT INTO llm_router_cp_instances ("
                    "instance_id, tenant_id, effective_version, effective_digest, "
                    "source, sidecar_version, last_apply_latency_ms, last_seen_at"
                    ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
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
                cur.execute(
                    "INSERT INTO llm_router_cp_heartbeats ("
                    "instance_id, tenant_id, effective_version, effective_digest, "
                    "source, sidecar_version, last_apply_latency_ms, seen_at"
                    ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
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
                row = self._fetch_instance(cur, instance_id)
        if row is None:
            raise ValueError(
                f"instance missing after record_heartbeat: {instance_id}"
            )
        return self._instance_heartbeat_from_row(row)

    def list_instances(self, tenant_id: str) -> list[InstanceHeartbeatRecord]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT instance_id, tenant_id, effective_version, effective_digest, "
                "source, sidecar_version, last_apply_latency_ms, last_seen_at "
                "FROM llm_router_cp_instances WHERE tenant_id = %s "
                "ORDER BY last_seen_at DESC",
                (tenant_id,),
            )
            rows = cur.fetchall()
        self._conn.rollback()
        return [self._instance_heartbeat_from_row(row) for row in rows]

    def close(self) -> None:
        self._conn.close()

    def _get_tenant(self, tenant_id: str) -> TenantRecord | None:
        with self._conn.cursor() as cur:
            row = self._fetch_tenant(cur, tenant_id)
        self._conn.rollback()
        if row is None:
            return None
        return self._tenant_from_row(row)

    def _get_policy_version(
        self, tenant_id: str, version: int
    ) -> TenantPolicyVersionRecord | None:
        with self._conn.cursor() as cur:
            row = self._fetch_policy_version(cur, tenant_id, version)
        self._conn.rollback()
        if row is None:
            return None
        return self._tenant_policy_version_from_row(row)

    def _get_instance(self, instance_id: str) -> InstanceHeartbeatRecord | None:
        with self._conn.cursor() as cur:
            row = self._fetch_instance(cur, instance_id)
        self._conn.rollback()
        if row is None:
            return None
        return self._instance_heartbeat_from_row(row)

    @staticmethod
    def _ensure_tenant(
        cur: typing.Any,
        tenant_id: str,
        org_id: str | None,
        created_at: float,
    ) -> None:
        cur.execute(
            "INSERT INTO llm_router_cp_tenants (tenant_id, org_id, created_at) "
            "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (tenant_id, org_id, created_at),
        )

    @staticmethod
    def _fetch_tenant(
        cur: typing.Any, tenant_id: str
    ) -> tuple[typing.Any, ...] | None:
        cur.execute(
            "SELECT tenant_id, org_id, created_at FROM llm_router_cp_tenants "
            "WHERE tenant_id = %s",
            (tenant_id,),
        )
        return cur.fetchone()

    @staticmethod
    def _fetch_policy_version(
        cur: typing.Any, tenant_id: str, version: int
    ) -> tuple[typing.Any, ...] | None:
        cur.execute(
            "SELECT tenant_id, version, yaml_text, normalized_json, actor, note, "
            "created_at FROM llm_router_cp_policy_versions "
            "WHERE tenant_id = %s AND version = %s",
            (tenant_id, version),
        )
        return cur.fetchone()

    @staticmethod
    def _fetch_instance(
        cur: typing.Any, instance_id: str
    ) -> tuple[typing.Any, ...] | None:
        cur.execute(
            "SELECT instance_id, tenant_id, effective_version, effective_digest, "
            "source, sidecar_version, last_apply_latency_ms, last_seen_at "
            "FROM llm_router_cp_instances WHERE instance_id = %s",
            (instance_id,),
        )
        return cur.fetchone()

    @staticmethod
    def _normalize_json_text(normalized_json: str) -> str:
        payload = json.loads(normalized_json)
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _tenant_from_row(row: typing.Sequence[typing.Any]) -> TenantRecord:
        return TenantRecord(
            tenant_id=str(row[0]),
            org_id=typing.cast(typing.Optional[str], row[1]),
            created_at=float(row[2]),
        )

    @staticmethod
    def _tenant_policy_version_from_row(
        row: typing.Sequence[typing.Any],
    ) -> TenantPolicyVersionRecord:
        return TenantPolicyVersionRecord(
            tenant_id=str(row[0]),
            version=int(row[1]),
            yaml_text=str(row[2]),
            normalized_json=str(row[3]),
            actor=str(row[4]),
            note=str(row[5] or ""),
            created_at=float(row[6]),
        )

    @staticmethod
    def _instance_heartbeat_from_row(
        row: typing.Sequence[typing.Any],
    ) -> InstanceHeartbeatRecord:
        return InstanceHeartbeatRecord(
            instance_id=str(row[0]),
            tenant_id=str(row[1]),
            effective_version=typing.cast(typing.Optional[int], row[2]),
            effective_digest=typing.cast(typing.Optional[str], row[3]),
            source=str(row[4]),
            sidecar_version=str(row[5] or ""),
            last_apply_latency_ms=typing.cast(
                typing.Optional[float], row[6]
            ),
            last_seen_at=float(row[7]),
        )


__all__ = ["PostgresControlPlaneStore"]
