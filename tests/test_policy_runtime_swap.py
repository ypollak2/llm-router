"""Iteration 9 acceptance — runtime effective-policy swap seam."""
from __future__ import annotations

import pytest

from llm_router.policy import OrgPolicy, load_org_policy
from llm_router.policy_runtime import (
    effective_policy_metadata,
    get_effective_org_policy,
    install_effective_org_policy,
    reset_effective_policy_for_tests,
)


@pytest.fixture(autouse=True)
def _reset():
    reset_effective_policy_for_tests()
    yield
    reset_effective_policy_for_tests()


def test_default_matches_local_load() -> None:
    eff = get_effective_org_policy()
    local = load_org_policy() or OrgPolicy()
    assert isinstance(eff, OrgPolicy)
    assert eff.block_models == local.block_models
    assert eff.block_providers == local.block_providers
    assert effective_policy_metadata()["source"] == "local"


def test_installed_policy_returned_atomically() -> None:
    pol = OrgPolicy(block_models=["ollama/qwen3:32b"], block_providers=["openai"], source="control_plane")
    install_effective_org_policy(pol, source="control_plane", version=3, digest="abc")
    got = get_effective_org_policy()
    assert got.block_models == ["ollama/qwen3:32b"]
    assert got.block_providers == ["openai"]
    meta = effective_policy_metadata()
    assert meta == {"source": "control_plane", "version": 3, "digest": "abc"}


def test_reset_restores_default() -> None:
    install_effective_org_policy(OrgPolicy(block_providers=["openai"]), source="control_plane", version=1, digest="d")
    reset_effective_policy_for_tests()
    assert effective_policy_metadata()["source"] == "local"
    assert get_effective_org_policy().block_providers == (load_org_policy() or OrgPolicy()).block_providers


def test_get_always_returns_complete_policy_under_swaps() -> None:
    # Repeated installs + reads never yield a partial/None policy.
    for i in range(50):
        install_effective_org_policy(
            OrgPolicy(block_models=[f"m{i}"]), source="control_plane", version=i, digest=str(i)
        )
        p = get_effective_org_policy()
        assert isinstance(p, OrgPolicy)
        assert p.block_models == [f"m{i}"]
