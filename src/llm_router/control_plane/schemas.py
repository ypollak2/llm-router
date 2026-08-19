from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TenantRecord:
    tenant_id: str
    org_id: str | None
    created_at: float


@dataclass(frozen=True)
class TenantPolicyVersionRecord:
    tenant_id: str
    version: int
    yaml_text: str
    normalized_json: str
    actor: str
    note: str
    created_at: float


@dataclass(frozen=True)
class InstanceHeartbeatRecord:
    instance_id: str
    tenant_id: str
    effective_version: int | None
    effective_digest: str | None
    source: str
    sidecar_version: str
    last_apply_latency_ms: float | None
    last_seen_at: float


@dataclass(frozen=True)
class PolicyChangeRecord:
    tenant_id: str
    version: int
    digest: str
    created_at: float


__all__ = [
    "TenantRecord",
    "TenantPolicyVersionRecord",
    "InstanceHeartbeatRecord",
    "PolicyChangeRecord",
]
