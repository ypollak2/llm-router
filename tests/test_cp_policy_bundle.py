"""Iteration 3 acceptance — control-plane policy bundle normalization + digest."""
from __future__ import annotations

import pytest

from llm_router.control_plane.policy_bundle import (
    make_payload,
    normalize_org_policy_yaml,
    policy_digest,
    runtime_policy_from_payload,
)
from llm_router.policy import OrgPolicy as RuntimeOrgPolicy


def test_normalize_extracts_only_runtime_fields() -> None:
    y = """
block_providers: [openai]
block_models: ["openai/gpt-4o"]
allow_models: []
task_caps: {code: 5000}
enforce: advise
some_other_key: ignored
"""
    norm = normalize_org_policy_yaml(y)
    assert set(norm) == {"block_providers", "block_models", "allow_models", "task_caps"}
    assert norm["block_providers"] == ["openai"]
    assert norm["task_caps"] == {"code": 5000}


def test_reordered_yaml_same_semantic_same_digest() -> None:
    y1 = "block_providers: [openai, anthropic]\nblock_models: []\n"
    y2 = "block_models: []\nblock_providers: [anthropic, openai]\n"  # reordered list + keys
    p1 = make_payload(tenant_id="t1", version=1, issued_at=1.0, yaml_text=y1)
    p2 = make_payload(tenant_id="t1", version=1, issued_at=1.0, yaml_text=y2)
    assert policy_digest(p1) == policy_digest(p2)  # sorting makes it deterministic


def test_different_policy_different_digest() -> None:
    a = make_payload(tenant_id="t1", version=1, issued_at=1.0, yaml_text="block_providers: [openai]\n")
    b = make_payload(tenant_id="t1", version=1, issued_at=1.0, yaml_text="block_providers: [anthropic]\n")
    assert policy_digest(a) != policy_digest(b)


def test_plaintext_secret_rejected() -> None:
    # A plaintext-looking API key must be rejected by the reused secure scanner.
    bad = "block_providers: []\nnote: 'sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'\n"
    with pytest.raises(Exception):
        normalize_org_policy_yaml(bad)


def test_payload_maps_to_runtime_orgpolicy() -> None:
    y = "block_providers: [openai]\nblock_models: [\"openai/gpt-4o\"]\ntask_caps: {code: 100}\n"
    payload = make_payload(tenant_id="t1", version=2, issued_at=9.0, yaml_text=y)
    pol = runtime_policy_from_payload(payload)
    assert isinstance(pol, RuntimeOrgPolicy)
    assert pol.block_providers == ["openai"]
    assert pol.block_models == ["openai/gpt-4o"]
    assert pol.task_caps == {"code": 100}
    assert pol.source == "control_plane"


def test_digest_is_stable_hex() -> None:
    p = make_payload(tenant_id="t1", version=1, issued_at=1.0, yaml_text="block_providers: []\n")
    d = policy_digest(p)
    assert isinstance(d, str) and len(d) == 64  # sha256 hexdigest
