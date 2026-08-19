from __future__ import annotations

import httpx


class CPClient:
    def __init__(
        self,
        cp_url: str,
        *,
        sidecar_token: str | None = None,
        timeout: float = 5.0,
        transport=None,
    ):
        cp_url = cp_url.rstrip("/")
        self._sidecar_token = sidecar_token
        self._client = httpx.Client(
            base_url=cp_url,
            timeout=timeout,
            transport=transport,
        )

    def _headers(self) -> dict:
        if self._sidecar_token is not None:
            return {"Authorization": f"Bearer {self._sidecar_token}"}
        return {}

    def get_current_policy(self, tenant_id: str) -> dict:
        resp = self._client.get(
            f"/cp/v1/tenants/{tenant_id}/policy/current",
            headers=self._headers(),
        )
        resp.raise_for_status()
        return resp.json()

    def post_heartbeat(self, tenant_id: str, payload: dict) -> dict:
        resp = self._client.post(
            f"/cp/v1/tenants/{tenant_id}/heartbeat",
            json=payload,
            headers=self._headers(),
        )
        resp.raise_for_status()
        return resp.json()

    def close(self):
        self._client.close()


__all__ = ["CPClient"]
