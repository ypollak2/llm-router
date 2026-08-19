"""Iteration 12 acceptance — day-one migration (#44) + pilot bootstrap (#59)."""
from __future__ import annotations

import pytest

from llm_router.control_plane.migration import bootstrap_tenant
from llm_router.control_plane.store import SqliteControlPlaneStore
from llm_router.policy import OrgPolicy
from llm_router.policy_runtime import get_effective_org_policy, reset_effective_policy_for_tests


@pytest.fixture()
def store():
    s = SqliteControlPlaneStore(":memory:")
    yield s
    s.close()


@pytest.fixture()
def policy_file(tmp_path):
    p = tmp_path / "org-policy.yaml"
    p.write_text("block_providers: [openai]\nblock_models: []\ntask_caps: {code: 5000}\n")
    return p


def test_local_policy_becomes_v1(store, policy_file) -> None:
    r = bootstrap_tenant(store, tenant_id="t1", policy_path=policy_file)
    assert r["action"] == "created" and r["version"] == 1
    cur = store.get_current_policy("t1")
    assert cur is not None and cur.version == 1
    assert "openai" in cur.yaml_text


def test_idempotent_rerun_is_noop(store, policy_file) -> None:
    bootstrap_tenant(store, tenant_id="t1", policy_path=policy_file)
    r2 = bootstrap_tenant(store, tenant_id="t1", policy_path=policy_file)
    assert r2["action"] == "noop" and r2["version"] == 1


def test_different_policy_refused_without_force(store, policy_file, tmp_path) -> None:
    bootstrap_tenant(store, tenant_id="t1", policy_path=policy_file)
    other = tmp_path / "other.yaml"
    other.write_text("block_providers: [anthropic]\n")
    with pytest.raises(ValueError):
        bootstrap_tenant(store, tenant_id="t1", policy_path=other)
    # force appends a NEW version (history preserved).
    r = bootstrap_tenant(store, tenant_id="t1", policy_path=other, force=True)
    assert r["action"] == "forced" and r["version"] == 2


def test_missing_local_file_uses_permissive_default(store, tmp_path) -> None:
    r = bootstrap_tenant(store, tenant_id="t1", policy_path=tmp_path / "does-not-exist.yaml")
    assert r["action"] == "created" and r["version"] == 1


def test_zero_behavior_change_when_sidecar_disabled(store, policy_file) -> None:
    reset_effective_policy_for_tests()
    before = get_effective_org_policy()
    bootstrap_tenant(store, tenant_id="t1", policy_path=policy_file)
    after = get_effective_org_policy()
    # Bootstrap only seeds the control-plane store; the router's effective policy
    # is unchanged (still local) because no sidecar installed anything.
    assert isinstance(after, OrgPolicy)
    assert after.block_providers == before.block_providers
    reset_effective_policy_for_tests()


def test_cli_no_subcommand_prints_help() -> None:
    from llm_router.commands.cp import cmd_cp
    assert cmd_cp([]) == 2  # no subcommand -> prints help, returns 2


def test_cli_bootstrap_end_to_end(tmp_path, policy_file) -> None:
    from llm_router.commands.cp import cmd_cp
    db = tmp_path / "cp_store.db"
    rc = cmd_cp(["bootstrap-tenant", "--tenant-id", "t1",
                 "--policy-path", str(policy_file), "--store-path", str(db)])
    assert rc == 0
    # Re-run is idempotent (still exit 0).
    assert cmd_cp(["bootstrap-tenant", "--tenant-id", "t1",
                   "--policy-path", str(policy_file), "--store-path", str(db)]) == 0
