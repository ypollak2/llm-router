"""Iteration 1 acceptance — control-plane SQLite store (no Postgres needed).

Covers: tenant creation idempotency, per-tenant version timelines, active
pointer swap, heartbeat upsert + append-only history, and tenant isolation.
"""
from __future__ import annotations

import pytest

from llm_router.control_plane import SqliteControlPlaneStore


@pytest.fixture()
def store():
    s = SqliteControlPlaneStore(":memory:")
    yield s
    s.close()


def test_ensure_tenant_idempotent(store) -> None:
    a = store.ensure_tenant("t1", org_id="o1")
    b = store.ensure_tenant("t1")  # second call must not error or duplicate
    assert a.tenant_id == b.tenant_id == "t1"
    assert a.org_id == "o1"
    # created_at preserved (not overwritten) on the idempotent call
    assert b.org_id == "o1"


def test_append_versions_autoincrement_per_tenant(store) -> None:
    v1 = store.append_policy_version("t1", yaml_text="a: 1", normalized_json='{"a":1}', actor="admin")
    v2 = store.append_policy_version("t1", yaml_text="a: 2", normalized_json='{"a":2}', actor="admin", note="second")
    assert (v1.version, v2.version) == (1, 2)
    # Versions are per-tenant: a different tenant restarts at 1.
    other = store.append_policy_version("t2", yaml_text="b: 1", normalized_json='{"b":1}', actor="admin")
    assert other.version == 1
    assert v2.note == "second"


def test_active_pointer_swap(store) -> None:
    store.append_policy_version("t1", yaml_text="a: 1", normalized_json='{"a":1}', actor="admin")
    store.append_policy_version("t1", yaml_text="a: 2", normalized_json='{"a":2}', actor="admin")
    assert store.get_current_policy("t1") is None  # nothing active yet
    store.set_active_policy("t1", 1)
    assert store.get_current_policy("t1").version == 1
    store.set_active_policy("t1", 2)  # swap
    assert store.get_current_policy("t1").version == 2


def test_set_active_rejects_missing_version(store) -> None:
    store.append_policy_version("t1", yaml_text="a: 1", normalized_json='{"a":1}', actor="admin")
    with pytest.raises(ValueError):
        store.set_active_policy("t1", 99)


def test_heartbeat_upsert_and_history(store) -> None:
    store.record_heartbeat(instance_id="i1", tenant_id="t1", effective_version=1,
                           effective_digest="d1", source="control_plane")
    store.record_heartbeat(instance_id="i1", tenant_id="t1", effective_version=2,
                           effective_digest="d2", source="last_known_good")
    instances = store.list_instances("t1")
    assert len(instances) == 1  # upsert by instance_id → one current row
    assert instances[0].effective_version == 2  # latest wins
    assert instances[0].source == "last_known_good"


def test_tenant_isolation(store) -> None:
    store.record_heartbeat(instance_id="i1", tenant_id="t1", effective_version=1,
                           effective_digest="d", source="control_plane")
    store.record_heartbeat(instance_id="i2", tenant_id="t2", effective_version=1,
                           effective_digest="d", source="control_plane")
    assert [i.instance_id for i in store.list_instances("t1")] == ["i1"]
    assert [i.instance_id for i in store.list_instances("t2")] == ["i2"]
