"""Tests for dashboard token authentication."""

from __future__ import annotations

import os
import secrets
from unittest.mock import patch

import pytest


def test_get_or_create_token_creates_file(tmp_path):
    token_file = tmp_path / "dashboard.token"
    with patch("llm_router.dashboard.server._TOKEN_FILE", token_file):
        from llm_router.dashboard.server import _get_or_create_token

        token = _get_or_create_token()
        assert token_file.exists()
        assert token_file.read_text().strip() == token
        assert len(token) >= 32


def test_get_or_create_token_returns_existing(tmp_path):
    token_file = tmp_path / "dashboard.token"
    expected = "existing-token-abc123"
    token_file.write_text(expected)
    with patch("llm_router.dashboard.server._TOKEN_FILE", token_file):
        from llm_router.dashboard.server import _get_or_create_token

        assert _get_or_create_token() == expected


def test_get_or_create_token_sets_permissions(tmp_path):
    token_file = tmp_path / "dashboard.token"
    with patch("llm_router.dashboard.server._TOKEN_FILE", token_file):
        from llm_router.dashboard.server import _get_or_create_token

        _get_or_create_token()
        mode = oct(os.stat(token_file).st_mode)[-3:]
        assert mode == "600"


def test_html_injects_token():
    from llm_router.dashboard.server import _html

    token = secrets.token_urlsafe(16)
    html = _html(token)
    assert f"window.DASHBOARD_TOKEN = {repr(token)}" in html or f'"{token}"' in html


def test_html_default_empty_token():
    """_html() with no token injects an empty string — server always passes real token."""
    from llm_router.dashboard.server import _html

    html = _html()
    assert "window.DASHBOARD_TOKEN" in html


# ──────────────────────────────────────────────────────────────────────────────
# Middleware integration tests
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_auth_middleware_allows_index_without_token():
    """GET / should be accessible without token (displays dashboard UI)."""
    from aiohttp import web
    from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop
    from llm_router.dashboard.server import _get_or_create_token, _html

    class DashboardTestApp(AioHTTPTestCase):
        async def get_application(self):
            token = _get_or_create_token()

            @web.middleware
            async def auth_middleware(request, handler):
                if request.path == "/":
                    return await handler(request)
                provided = request.headers.get("X-Dashboard-Token") or request.rel_url.query.get("token")
                if provided != token:
                    raise web.HTTPUnauthorized(text="Unauthorized")
                return await handler(request)

            async def handle_index(request):
                return web.Response(text=_html(token), content_type="text/html")

            app = web.Application(middlewares=[auth_middleware])
            app.router.add_get("/", handle_index)
            return app

        @unittest_run_loop
        async def test_index_accessible(self):
            resp = await self.client.request("GET", "/")
            assert resp.status == 200
            text = await resp.text()
            assert "LLM Router" in text


@pytest.mark.asyncio
async def test_auth_middleware_blocks_api_without_token():
    """POST endpoints should be blocked without valid token."""
    from aiohttp import web
    from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop

    class DashboardTestApp(AioHTTPTestCase):
        async def get_application(self):
            token = "test-secret-token"

            @web.middleware
            async def auth_middleware(request, handler):
                if request.path == "/":
                    return await handler(request)
                provided = request.headers.get("X-Dashboard-Token") or request.rel_url.query.get("token")
                if provided != token:
                    raise web.HTTPUnauthorized(text="Unauthorized")
                return await handler(request)

            async def handle_budget_set(request):
                return web.Response(text='{"ok": true}', content_type="application/json")

            app = web.Application(middlewares=[auth_middleware])
            app.router.add_post("/api/budget/set", handle_budget_set)
            return app

        @unittest_run_loop
        async def test_api_blocked_without_token(self):
            resp = await self.client.post("/api/budget/set")
            assert resp.status == 401


@pytest.mark.asyncio
async def test_auth_middleware_allows_api_with_valid_token():
    """POST endpoints should work with valid token in header."""
    from aiohttp import web
    from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop

    class DashboardTestApp(AioHTTPTestCase):
        async def get_application(self):
            self.token = "test-secret-token"

            @web.middleware
            async def auth_middleware(request, handler):
                if request.path == "/":
                    return await handler(request)
                provided = request.headers.get("X-Dashboard-Token") or request.rel_url.query.get("token")
                if provided != self.token:
                    raise web.HTTPUnauthorized(text="Unauthorized")
                return await handler(request)

            async def handle_budget_set(request):
                return web.Response(text='{"ok": true}', content_type="application/json")

            app = web.Application(middlewares=[auth_middleware])
            app.router.add_post("/api/budget/set", handle_budget_set)
            return app

        @unittest_run_loop
        async def test_api_allowed_with_token(self):
            resp = await self.client.post(
                "/api/budget/set",
                headers={"X-Dashboard-Token": self.token},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True
