"""Tests for dashboard token authentication."""

from __future__ import annotations

import os
import secrets
from unittest.mock import patch


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
