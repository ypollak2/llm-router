"""P0-4 — SCIM 2.0 is mounted on the SERVED admin app.

SCIM was fully built + tested but only ever wired into a standalone app no
process ran, so no deployed LLM Router exposed ``/scim/v2`` and IdP-driven
deprovisioning was unreachable. ``create_app()`` now mounts the SCIM router
when ``LLM_ROUTER_SCIM_ENABLED`` + ``LLM_ROUTER_SCIM_TOKEN`` are set.
"""
from __future__ import annotations

from fastapi.testclient import TestClient


def _client(tmp_path) -> TestClient:
    from llm_router.admin_api import create_app

    return TestClient(create_app())


def test_scim_routable_on_admin_app_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_IDENTITY_PATH", str(tmp_path / "identity.db"))
    monkeypatch.setenv("LLM_ROUTER_SCIM_ENABLED", "true")
    monkeypatch.setenv("LLM_ROUTER_SCIM_TOKEN", "scim-secret-xyz")
    client = _client(tmp_path)

    # Routable (not 404) and auth-gated.
    unauth = client.get("/scim/v2/Users")
    assert unauth.status_code != 404, "SCIM /scim/v2/Users not mounted on the served app"
    assert unauth.status_code in (401, 403), unauth.status_code

    # With the bearer secret → reachable.
    ok = client.get(
        "/scim/v2/Users", headers={"Authorization": "Bearer scim-secret-xyz"}
    )
    assert ok.status_code == 200, ok.text


def test_scim_not_mounted_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_IDENTITY_PATH", str(tmp_path / "identity.db"))
    monkeypatch.delenv("LLM_ROUTER_SCIM_ENABLED", raising=False)
    monkeypatch.delenv("LLM_ROUTER_SCIM_TOKEN", raising=False)
    client = _client(tmp_path)
    assert client.get("/scim/v2/Users").status_code == 404


def test_scim_provision_and_deprovision_e2e(tmp_path, monkeypatch):
    """The headline SCIM flow now works against the served app: provision a
    user (201) then deprovision via PATCH active=false."""
    monkeypatch.setenv("LLM_ROUTER_IDENTITY_PATH", str(tmp_path / "identity.db"))
    monkeypatch.setenv("LLM_ROUTER_SCIM_ENABLED", "1")
    monkeypatch.setenv("LLM_ROUTER_SCIM_TOKEN", "scim-secret-xyz")
    client = _client(tmp_path)
    h = {"Authorization": "Bearer scim-secret-xyz"}

    created = client.post(
        "/scim/v2/Users",
        headers=h,
        json={"userName": "alice@corp.io", "displayName": "Alice", "active": True},
    )
    assert created.status_code == 201, created.text
    uid = created.json()["id"]

    deprov = client.patch(
        f"/scim/v2/Users/{uid}",
        headers=h,
        json={
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": "replace", "value": {"active": False}}],
        },
    )
    assert deprov.status_code == 200, deprov.text
