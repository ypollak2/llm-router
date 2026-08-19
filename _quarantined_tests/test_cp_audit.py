"""Iteration 5 acceptance — control plane's own tamper-evident audit log (#42)."""
from __future__ import annotations

import json
import sqlite3

import pytest

from llm_router.control_plane import audit as cpa


@pytest.fixture()
def cp_audit(tmp_path, monkeypatch):
    db = tmp_path / "cp_audit.db"
    monkeypatch.setenv("LLM_ROUTER_CP_AUDIT_PATH", str(db))
    cpa.reset_cp_audit_log_for_tests()
    yield db
    cpa.reset_cp_audit_log_for_tests()


def test_events_append_to_independent_db(cp_audit) -> None:
    cpa.audit_policy_created(tenant_id="t1", version=1, digest="d1")
    cpa.audit_policy_activated(tenant_id="t1", version=1, digest="d1")
    cpa.audit_bundle_served(tenant_id="t1", version=1, digest="d1")
    rows = cpa.get_cp_audit_log().recent(limit=10)
    actions = {r["action"] for r in rows}
    assert cpa.ACTION_POLICY_VERSION_CREATED in actions
    assert cpa.ACTION_POLICY_ACTIVATED in actions
    assert cpa.ACTION_BUNDLE_SERVED in actions
    # Written to the isolated CP DB, not the default runtime audit DB.
    assert cp_audit.exists()


def test_verify_chain_passes_for_untampered(cp_audit) -> None:
    cpa.audit_policy_created(tenant_id="t1", version=1, digest="d1")
    cpa.audit_heartbeat(tenant_id="t1", instance_id="i1", effective_version=1,
                        effective_digest="d1", source="control_plane")
    cpa.verify_cp_audit_chain()  # must not raise


def test_direct_tamper_breaks_chain(cp_audit) -> None:
    cpa.audit_policy_created(tenant_id="t1", version=1, digest="d1")
    cpa.audit_policy_activated(tenant_id="t1", version=1, digest="d1")
    cpa.reset_cp_audit_log_for_tests()  # drop the cached handle before raw edit
    # Tamper directly in SQL — mutate a row's detail without fixing the hash.
    conn = sqlite3.connect(str(cp_audit))
    conn.execute("UPDATE audit_events SET detail = ? WHERE seq = (SELECT MIN(seq) FROM audit_events)",
                 (json.dumps({"version": 999, "digest": "forged"}),))
    conn.commit()
    conn.close()
    cpa.reset_cp_audit_log_for_tests()
    with pytest.raises(cpa.TamperDetected):
        cpa.verify_cp_audit_chain()


def test_heartbeat_detail_carries_tenant_and_version(cp_audit) -> None:
    cpa.audit_heartbeat(tenant_id="t1", instance_id="i9", effective_version=7,
                        effective_digest="dd", source="last_known_good")
    row = cpa.get_cp_audit_log().recent(limit=1)[0]
    detail = row["detail"] if isinstance(row["detail"], dict) else json.loads(row["detail"])
    assert detail["tenant_id"] == "t1"
    assert detail["instance_id"] == "i9"
    assert detail["effective_version"] == 7
    assert detail["source"] == "last_known_good"


def test_signature_failed_is_critical(cp_audit) -> None:
    cpa.audit_signature_failed(tenant_id="t1", instance_id="i1", detail={"reason": "bad sig"})
    row = cpa.get_cp_audit_log().recent(limit=1)[0]
    assert row["action"] == cpa.ACTION_SIGNATURE_FAILED
    assert row["severity"] == "critical"
