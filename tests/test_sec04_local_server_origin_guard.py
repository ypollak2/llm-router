"""Regression: CHZ-SEC-04 — loopback model-call servers had no CSRF/rebinding guard.

route_server (:7338) and gateway (:17900) bind loopback but every POST triggers
a real (possibly paid) model call. A malicious web page could CSRF them from the
user's browser, or use DNS-rebinding (attacker.com -> 127.0.0.1). Both mechanisms
carry a non-loopback Host and/or a cross-site Origin/Referer; legitimate CLI/SDK
clients (OPENAI_BASE_URL) send a loopback Host and no browser Origin.

The shared guard `is_forbidden_cross_origin` gates both servers.
"""

from __future__ import annotations

import pytest

from llm_router.route_server import is_forbidden_cross_origin


class _H(dict):
    """Case-insensitive header mapping like http.client.HTTPMessage."""
    def get(self, k, default=None):  # noqa: A003
        for kk, vv in self.items():
            if kk.lower() == k.lower():
                return vv
        return default


# ── shared guard logic ────────────────────────────────────────────────────────

def test_loopback_clients_allowed():
    assert is_forbidden_cross_origin(_H({"Host": "127.0.0.1:7338"})) is False
    assert is_forbidden_cross_origin(_H({"Host": "localhost:17900"})) is False
    assert is_forbidden_cross_origin(_H({})) is False  # curl may omit? still allow
    # legit SDK: loopback Host, no Origin
    assert is_forbidden_cross_origin(_H({"Host": "127.0.0.1", "User-Agent": "openai-python"})) is False


def test_dns_rebinding_host_rejected():
    assert is_forbidden_cross_origin(_H({"Host": "attacker.com"})) is True
    assert is_forbidden_cross_origin(_H({"Host": "evil.example:7338"})) is True


def test_cross_site_browser_origin_rejected():
    assert is_forbidden_cross_origin(_H({"Host": "127.0.0.1", "Origin": "http://evil.com"})) is True
    assert is_forbidden_cross_origin(_H({"Host": "127.0.0.1", "Referer": "https://evil.com/x"})) is True
    # same-origin browser request (localhost) is fine
    assert is_forbidden_cross_origin(_H({"Host": "localhost", "Origin": "http://localhost:7338"})) is False


# ── gateway FastAPI middleware, end-to-end via TestClient ─────────────────────

@pytest.fixture()
def gateway_client():
    starlette_testclient = pytest.importorskip("starlette.testclient")
    from llm_router.gateway import app
    # base_url gives a loopback Host by default so legit requests pass the guard.
    return starlette_testclient.TestClient(app, base_url="http://127.0.0.1")


def test_gateway_allows_loopback(gateway_client):
    # A GET to /healthz does not trigger a model call; the guard must NOT 403 it.
    r = gateway_client.get("/healthz")
    assert r.status_code != 403, "loopback request wrongly rejected"


def test_gateway_blocks_rebinding_host(gateway_client):
    r = gateway_client.get("/healthz", headers={"Host": "attacker.com"})
    assert r.status_code == 403


def test_gateway_blocks_cross_site_origin(gateway_client):
    r = gateway_client.get("/healthz", headers={"Origin": "http://evil.com"})
    assert r.status_code == 403
