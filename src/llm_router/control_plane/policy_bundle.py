from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import yaml

from llm_router.org_policy import _scan_for_plaintext_secrets
from llm_router.policy import OrgPolicy as RuntimeOrgPolicy


@dataclass(frozen=True)
class PolicyVersionMeta:
    tenant_id: str
    version: int
    issued_at: float


@dataclass(frozen=True)
class PolicyBundlePayload:
    meta: PolicyVersionMeta
    policy: dict


@dataclass(frozen=True)
class PolicyBundleSignature:
    algorithm: str
    public_key_b64: str
    signature_b64: str


@dataclass(frozen=True)
class PolicyBundle:
    payload: PolicyBundlePayload
    signature: PolicyBundleSignature | None


def _normalized_str_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        values = [value]
    else:
        try:
            values = list(value)  # type: ignore[arg-type]
        except TypeError:
            values = [value]
    return sorted(str(item) for item in values)


def _normalized_task_caps(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {str(key): int(value[key]) for key in sorted(value, key=str)}


def normalize_org_policy_yaml(yaml_text: str) -> dict:
    """Validate + normalize a policy YAML into a deterministic runtime-policy dict.

    Reuses the secure loader's ``_scan_for_plaintext_secrets`` so a bundle can
    never carry a plaintext credential, then extracts ONLY the four runtime
    enforcement fields (llm_router.policy.OrgPolicy) with sorted lists so the same
    semantic policy always yields the same canonical dict + digest.
    """
    _scan_for_plaintext_secrets(yaml_text, source="<policy-bundle>")

    data = yaml.safe_load(yaml_text) or {}
    if not isinstance(data, dict):
        data = {}

    return {
        "block_providers": _normalized_str_list(data.get("block_providers", [])),
        "block_models": _normalized_str_list(data.get("block_models", [])),
        "allow_models": _normalized_str_list(data.get("allow_models", [])),
        "task_caps": _normalized_task_caps(data.get("task_caps", {})),
    }


def bundle_payload_bytes(payload: PolicyBundlePayload) -> bytes:
    canonical = {
        "meta": {
            "tenant_id": payload.meta.tenant_id,
            "version": payload.meta.version,
            "issued_at": payload.meta.issued_at,
        },
        "policy": payload.policy,
    }
    return json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")


def policy_digest(payload: PolicyBundlePayload) -> str:
    return hashlib.sha256(bundle_payload_bytes(payload)).hexdigest()


def runtime_policy_from_payload(payload: PolicyBundlePayload) -> RuntimeOrgPolicy:
    policy = payload.policy
    return RuntimeOrgPolicy(
        block_providers=list(policy.get("block_providers", [])),
        block_models=list(policy.get("block_models", [])),
        allow_models=list(policy.get("allow_models", [])),
        task_caps=dict(policy.get("task_caps", {})),
        source="control_plane",
    )


def make_payload(
    *,
    tenant_id: str,
    version: int,
    issued_at: float,
    yaml_text: str,
) -> PolicyBundlePayload:
    return PolicyBundlePayload(
        meta=PolicyVersionMeta(tenant_id=tenant_id, version=version, issued_at=issued_at),
        policy=normalize_org_policy_yaml(yaml_text),
    )


__all__ = [
    "PolicyVersionMeta",
    "PolicyBundlePayload",
    "PolicyBundleSignature",
    "PolicyBundle",
    "normalize_org_policy_yaml",
    "bundle_payload_bytes",
    "policy_digest",
    "runtime_policy_from_payload",
    "make_payload",
]
