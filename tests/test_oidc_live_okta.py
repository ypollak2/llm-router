"""Gated LIVE OIDC integration test — Okta (or any real OIDC IdP).

Unlike test_oidc_validation.py (which injects a mock JWKS), this test
exercises the FULL production path against a REAL tenant: it builds the
validator from env, fetches the live JWKS over the network from the
issuer's discovery endpoint, and validates a REAL signed token the
operator obtained from the IdP.

It is SKIPPED unless both env vars are set, so it never runs in CI or on
a laptop without a tenant — it only runs where an operator has pointed it
at their Okta org:

    export LLM_ROUTER_OIDC_TEST_ISSUER="https://<your-org>.okta.com/oauth2/default"
    export LLM_ROUTER_OIDC_TEST_TOKEN="<a-real-access-or-id-token-from-that-issuer>"
    export LLM_ROUTER_OIDC_TEST_AUDIENCE="api://default"   # optional; the token's aud
    uv run pytest tests/test_oidc_live_okta.py -v

A green run here is the acceptance signal that closes #46 (oidc wired to
the real IdP). No secrets are committed — everything comes from the
operator's env.
"""
from __future__ import annotations

import os

import pytest

_ISSUER = os.environ.get("LLM_ROUTER_OIDC_TEST_ISSUER", "").strip()
_TOKEN = os.environ.get("LLM_ROUTER_OIDC_TEST_TOKEN", "").strip()

pytestmark = pytest.mark.skipif(
    not (_ISSUER and _TOKEN),
    reason="live OIDC test skipped — set LLM_ROUTER_OIDC_TEST_ISSUER + LLM_ROUTER_OIDC_TEST_TOKEN to run against a real tenant",
)


def test_live_oidc_token_validates_against_real_jwks(monkeypatch: pytest.MonkeyPatch) -> None:
    from llm_router.enterprise.oidc import OidcClaims, OidcConfig, OidcValidator

    # Point OidcConfig.from_env() at the operator's real tenant. JWKS is
    # auto-discovered as <issuer>/.well-known/jwks.json unless overridden.
    monkeypatch.setenv("LLM_ROUTER_OIDC_ISSUER", _ISSUER)
    aud = os.environ.get("LLM_ROUTER_OIDC_TEST_AUDIENCE", "").strip()
    if aud:
        monkeypatch.setenv("LLM_ROUTER_OIDC_AUDIENCE", aud)
    jwks_uri = os.environ.get("LLM_ROUTER_OIDC_TEST_JWKS_URI", "").strip()
    if jwks_uri:
        monkeypatch.setenv("LLM_ROUTER_OIDC_JWKS_URI", jwks_uri)

    config = OidcConfig.from_env()
    assert config is not None, "OidcConfig.from_env() returned None — check LLM_ROUTER_OIDC_* env"

    validator = OidcValidator(config)  # production mode → fetches live JWKS
    claims = validator.validate_sync(_TOKEN)

    assert isinstance(claims, OidcClaims)
    # A real token must carry a subject; email/groups depend on scopes/claims.
    assert getattr(claims, "sub", None) or getattr(claims, "email", None), (
        "validated claims had neither sub nor email — check token scopes / claim mapping"
    )
