"""Day-one migration: seed a tenant's control-plane policy from its current
local policy (#44) so a single-instance deployment can adopt the sidecar model
with ZERO behaviour change (#59).

``bootstrap_tenant`` reads the instance's existing local policy YAML and records
it as version 1 in the control-plane store. It changes nothing at runtime — the
router keeps using the local policy until a sidecar is explicitly enabled
(``LLM_ROUTER_CP_SIDECAR_ENABLED`` etc.), at which point it fetches+verifies the same
policy back from the control plane.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from llm_router.control_plane.policy_bundle import make_payload, normalize_org_policy_yaml, policy_digest
from llm_router.control_plane.store import ControlPlaneStore

_DEFAULT_PERMISSIVE_YAML = "block_providers: []\nblock_models: []\nallow_models: []\ntask_caps: {}\n"


def _local_policy_yaml(policy_path: str | os.PathLike | None) -> str:
    """Return the instance's current local policy YAML text (permissive default
    when no file exists — matching load_org_policy's default-permissive stance)."""
    path = policy_path or os.environ.get("LLM_ROUTER_POLICY_PATH")
    p = Path(path) if path else Path.home() / ".llm-router" / "org-policy.yaml"
    if p.is_file():
        return p.read_text(encoding="utf-8")
    return _DEFAULT_PERMISSIVE_YAML


def bootstrap_tenant(
    store: ControlPlaneStore,
    *,
    tenant_id: str,
    policy_path: str | os.PathLike | None = None,
    force: bool = False,
) -> dict:
    """Record the local policy as the tenant's version 1 (idempotent).

    - No existing active policy -> append v1, activate.
    - Existing active policy with the SAME normalized content -> no-op.
    - Existing active policy with DIFFERENT content -> refuse unless ``force``
      (force appends a NEW version rather than overwriting history).
    Returns {"tenant_id", "version", "digest", "action"} where action is one of
    "created" | "noop" | "forced".
    """
    yaml_text = _local_policy_yaml(policy_path)
    normalized = normalize_org_policy_yaml(yaml_text)  # validates + rejects plaintext secrets
    normalized_json = json.dumps(normalized, sort_keys=True, separators=(",", ":"))

    store.ensure_tenant(tenant_id)
    existing = store.get_current_policy(tenant_id)

    if existing is not None:
        if existing.normalized_json == normalized_json:
            digest = policy_digest(make_payload(
                tenant_id=tenant_id, version=existing.version,
                issued_at=existing.created_at, yaml_text=existing.yaml_text,
            ))
            return {"tenant_id": tenant_id, "version": existing.version,
                    "digest": digest, "action": "noop"}
        if not force:
            raise ValueError(
                f"tenant {tenant_id} already has a different active policy "
                f"(v{existing.version}); pass force=True to append a new version"
            )
        action = "forced"
    else:
        action = "created"

    rec = store.append_policy_version(
        tenant_id, yaml_text=yaml_text, normalized_json=normalized_json,
        actor="migration", note="day-one bootstrap",
    )
    store.set_active_policy(tenant_id, rec.version)
    digest = policy_digest(make_payload(
        tenant_id=tenant_id, version=rec.version, issued_at=rec.created_at, yaml_text=yaml_text,
    ))
    return {"tenant_id": tenant_id, "version": rec.version, "digest": digest, "action": action}


__all__ = ["bootstrap_tenant"]
