from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Callable

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel

from llm_router.control_plane import audit as cpa
from llm_router.control_plane import signing
from llm_router.control_plane.events import get_event_bus
from llm_router.control_plane.policy_bundle import (
    bundle_payload_bytes,
    make_payload,
    normalize_org_policy_yaml,
    policy_digest,
)
from llm_router.control_plane.store import ControlPlaneStore, SqliteControlPlaneStore


class PolicyPushRequest(BaseModel):
    yaml_text: str
    note: str = ""


class PolicyPushResponse(BaseModel):
    tenant_id: str
    version: int
    digest: str


class CurrentPolicyResponse(BaseModel):
    tenant_id: str
    version: int
    yaml_text: str
    normalized_json: str
    digest: str
    created_at: float
    # Ed25519 signature over the canonical bundle payload (Iter 7). The sidecar
    # reconstructs the payload from (tenant_id, version, created_at, yaml_text)
    # and verifies with public_key_b64 before applying — never holds the secret.
    signature_algorithm: str = "ed25519"
    signature_b64: str = ""
    public_key_b64: str = ""


class PublicKeyResponse(BaseModel):
    algorithm: str = "ed25519"
    public_key_b64: str


class HeartbeatRequest(BaseModel):
    instance_id: str
    effective_version: int | None = None
    effective_digest: str | None = None
    source: str
    sidecar_version: str = ""
    last_apply_latency_ms: float | None = None


class HeartbeatResponse(BaseModel):
    ok: bool
    instance_id: str


class InstanceStatus(BaseModel):
    instance_id: str
    effective_version: int | None
    effective_digest: str | None
    source: str
    last_seen_at: float


class TenantAuditStatus(BaseModel):
    tenant_id: str
    active_version: int | None
    instances: list[InstanceStatus]


_store_singleton: ControlPlaneStore | None = None
_store_lock = threading.Lock()


def get_cp_store() -> ControlPlaneStore:
    global _store_singleton
    with _store_lock:
        if _store_singleton is None:
            db_path = Path(
                os.environ.get(
                    "LLM_ROUTER_CP_STORE_PATH",
                    str(Path.home() / ".llm-router" / "cp_store.db"),
                )
            )
            db_path.parent.mkdir(parents=True, exist_ok=True)
            _store_singleton = SqliteControlPlaneStore(db_path)
        return _store_singleton


def authenticate_sidecar(
    authorization: str | None = Header(default=None),
) -> str:
    expected_token = os.environ.get("LLM_ROUTER_CP_SIDECAR_TOKEN")
    presented_token = ""
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and token:
            presented_token = token

    if expected_token is None:
        return presented_token
    if presented_token != expected_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid sidecar bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return presented_token


def _require_manage_policy() -> Callable[..., Any]:
    from llm_router.admin_api import require_perm
    from llm_router.enterprise.rbac import Permission

    return require_perm(Permission.MANAGE_POLICY)


_signing_key_singleton = None


def get_signing_key():
    """Return the control plane's Ed25519 private key (process singleton).

    Loaded from ``LLM_ROUTER_CP_ED25519_PRIVATE_KEY`` via signing.load_signing_key.
    Tests override this dependency with a generated key.
    """
    global _signing_key_singleton
    if _signing_key_singleton is None:
        _signing_key_singleton = signing.load_signing_key()
    return _signing_key_singleton


def publish_policy_change(tenant_id: str, version: int, digest: str) -> int:
    """Notify subscribed sidecars that a new policy version is active.

    Returns the number of subscribers notified (best-effort).
    """
    return get_event_bus().publish(
        tenant_id,
        {"type": "policy_change", "tenant_id": tenant_id, "version": version, "digest": digest},
    )


def create_control_plane_app() -> FastAPI:
    app = FastAPI(title="llm_router control plane")

    @app.get("/cp/v1/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/cp/v1/tenants/{tenant_id}/policy", response_model=PolicyPushResponse)
    def push_policy(
        tenant_id: str,
        req: PolicyPushRequest,
        store: ControlPlaneStore = Depends(get_cp_store),
        identity=Depends(_require_manage_policy()),
    ) -> PolicyPushResponse:
        normalized = normalize_org_policy_yaml(req.yaml_text)
        normalized_json = json.dumps(
            normalized,
            sort_keys=True,
            separators=(",", ":"),
        )
        actor = getattr(getattr(identity, "user", None), "email", "system")
        rec = store.append_policy_version(
            tenant_id,
            yaml_text=req.yaml_text,
            normalized_json=normalized_json,
            actor=actor,
            note=req.note,
        )
        store.set_active_policy(tenant_id, rec.version)
        # issued_at MUST be the stored created_at (not time.time()) so the
        # digest is reproducible: the sidecar re-derives the same digest from
        # the persisted record via /policy/current.
        payload = make_payload(
            tenant_id=tenant_id,
            version=rec.version,
            issued_at=rec.created_at,
            yaml_text=req.yaml_text,
        )
        digest = policy_digest(payload)
        cpa.audit_policy_created(
            tenant_id=tenant_id,
            version=rec.version,
            digest=digest,
        )
        cpa.audit_policy_activated(
            tenant_id=tenant_id,
            version=rec.version,
            digest=digest,
        )
        # Fast-path notify: subscribed sidecars pull immediately (5s SLO).
        publish_policy_change(tenant_id, rec.version, digest)
        return PolicyPushResponse(
            tenant_id=tenant_id,
            version=rec.version,
            digest=digest,
        )

    @app.get(
        "/cp/v1/tenants/{tenant_id}/policy/current",
        response_model=CurrentPolicyResponse,
    )
    def get_current_policy(
        tenant_id: str,
        store: ControlPlaneStore = Depends(get_cp_store),
        key=Depends(get_signing_key),
        _token: str = Depends(authenticate_sidecar),
    ) -> CurrentPolicyResponse:
        rec = store.get_current_policy(tenant_id)
        if rec is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No active policy for tenant {tenant_id}",
            )
        payload = make_payload(
            tenant_id=tenant_id,
            version=rec.version,
            issued_at=rec.created_at,
            yaml_text=rec.yaml_text,
        )
        digest = policy_digest(payload)
        signature_b64 = signing.sign_payload(key, bundle_payload_bytes(payload))
        cpa.audit_bundle_served(tenant_id=tenant_id, version=rec.version, digest=digest)
        return CurrentPolicyResponse(
            tenant_id=rec.tenant_id,
            version=rec.version,
            yaml_text=rec.yaml_text,
            normalized_json=rec.normalized_json,
            digest=digest,
            created_at=rec.created_at,
            signature_algorithm="ed25519",
            signature_b64=signature_b64,
            public_key_b64=signing.public_key_b64(key),
        )

    @app.get("/cp/v1/public-key", response_model=PublicKeyResponse)
    def public_key(key=Depends(get_signing_key)) -> PublicKeyResponse:
        return PublicKeyResponse(algorithm="ed25519", public_key_b64=signing.public_key_b64(key))

    @app.get("/cp/v1/tenants/{tenant_id}/policy/events")
    async def policy_events(tenant_id: str, _token: str = Depends(authenticate_sidecar)):
        """SSE stream of policy-change events for a tenant's sidecars."""
        from sse_starlette.sse import EventSourceResponse

        bus = get_event_bus()
        queue = bus.subscribe(tenant_id)

        async def _gen():
            try:
                while True:
                    event = await queue.get()
                    yield {"event": "policy_change", "data": json.dumps(event)}
            finally:
                bus.unsubscribe(tenant_id, queue)

        return EventSourceResponse(_gen())

    @app.post(
        "/cp/v1/tenants/{tenant_id}/heartbeat",
        response_model=HeartbeatResponse,
    )
    def heartbeat(
        tenant_id: str,
        req: HeartbeatRequest,
        store: ControlPlaneStore = Depends(get_cp_store),
        _token: str = Depends(authenticate_sidecar),
    ) -> HeartbeatResponse:
        # Detect a version/source TRANSITION before the upsert overwrites the
        # stored row, so the tamper-evident audit records transitions only —
        # not every routine heartbeat (which would flood the chain).
        prior = next(
            (i for i in store.list_instances(tenant_id) if i.instance_id == req.instance_id),
            None,
        )
        is_transition = (
            prior is None
            or prior.effective_version != req.effective_version
            or prior.source != req.source
        )
        store.record_heartbeat(
            instance_id=req.instance_id,
            tenant_id=tenant_id,
            effective_version=req.effective_version,
            effective_digest=req.effective_digest,
            source=req.source,
            sidecar_version=req.sidecar_version,
            last_apply_latency_ms=req.last_apply_latency_ms,
        )
        if is_transition:
            cpa.audit_heartbeat(
                tenant_id=tenant_id,
                instance_id=req.instance_id,
                effective_version=req.effective_version,
                effective_digest=req.effective_digest,
                source=req.source,
            )
        return HeartbeatResponse(ok=True, instance_id=req.instance_id)

    def _build_tenant_audit_status(
        tenant_id: str,
        store: ControlPlaneStore,
    ) -> TenantAuditStatus:
        current = store.get_current_policy(tenant_id)
        instances = [
            InstanceStatus(
                instance_id=rec.instance_id,
                effective_version=rec.effective_version,
                effective_digest=rec.effective_digest,
                source=rec.source,
                last_seen_at=rec.last_seen_at,
            )
            for rec in store.list_instances(tenant_id)
        ]
        return TenantAuditStatus(
            tenant_id=tenant_id,
            active_version=None if current is None else current.version,
            instances=instances,
        )

    @app.get("/cp/v1/tenants/{tenant_id}/instances", response_model=TenantAuditStatus)
    def list_instances(
        tenant_id: str,
        store: ControlPlaneStore = Depends(get_cp_store),
        _identity=Depends(_require_manage_policy()),
    ) -> TenantAuditStatus:
        return _build_tenant_audit_status(tenant_id, store)

    @app.get(
        "/cp/v1/tenants/{tenant_id}/audit/effective-policy",
        response_model=TenantAuditStatus,
    )
    def get_effective_policy_audit(
        tenant_id: str,
        store: ControlPlaneStore = Depends(get_cp_store),
        _identity=Depends(_require_manage_policy()),
    ) -> TenantAuditStatus:
        return _build_tenant_audit_status(tenant_id, store)

    @app.get("/cp/v1/tenants/{tenant_id}/reconciliation")
    def get_reconciliation(
        tenant_id: str,
        store: ControlPlaneStore = Depends(get_cp_store),
        _identity=Depends(_require_manage_policy()),
    ) -> dict:
        from llm_router.control_plane.reconciliation import reconcile_tenant_effective_policy

        return reconcile_tenant_effective_policy(store, tenant_id)

    return app


__all__ = [
    "create_control_plane_app",
    "get_cp_store",
    "authenticate_sidecar",
    "PolicyPushRequest",
    "PolicyPushResponse",
    "CurrentPolicyResponse",
    "HeartbeatRequest",
    "HeartbeatResponse",
    "InstanceStatus",
    "TenantAuditStatus",
]
