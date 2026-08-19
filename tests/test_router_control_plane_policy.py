"""Iteration 9 — a control-plane-installed policy actually enforces at routing.

Proves the wiring end-to-end at the policy layer: installing a policy via the
runtime seam makes get_effective_org_policy() return it, and apply_policy (the
exact function the router calls at router.py:463) drops the blocked model.
"""
from __future__ import annotations

import pytest

from llm_router.policy import OrgPolicy, apply_policy
from llm_router.policy_runtime import (
    get_effective_org_policy,
    install_effective_org_policy,
    reset_effective_policy_for_tests,
)


@pytest.fixture(autouse=True)
def _reset():
    reset_effective_policy_for_tests()
    yield
    reset_effective_policy_for_tests()


def test_control_plane_policy_blocks_model_at_enforcement() -> None:
    candidates = ["ollama/qwen3:32b", "codex/gpt-5.5", "openai/gpt-4o"]

    # Before: nothing installed -> default policy blocks nothing of ours.
    eff = get_effective_org_policy()
    kept_before, _ = apply_policy(candidates, "code", eff)
    assert "openai/gpt-4o" in kept_before

    # Install a control-plane policy that blocks a provider + a model.
    install_effective_org_policy(
        OrgPolicy(block_providers=["openai"], block_models=["codex/gpt-5.5"], source="control_plane"),
        source="control_plane", version=5, digest="deadbeef",
    )

    # After: the router's enforcement seam now sees the control-plane policy.
    eff2 = get_effective_org_policy()
    kept_after, blocked = apply_policy(candidates, "code", eff2)
    assert "openai/gpt-4o" not in kept_after      # provider blocked
    assert "codex/gpt-5.5" not in kept_after       # model blocked
    assert "ollama/qwen3:32b" in kept_after        # allowed survives
    assert set(blocked) == {"openai/gpt-4o", "codex/gpt-5.5"}
