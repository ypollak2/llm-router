"""E3: `llm_router serve` argument parsing."""
from __future__ import annotations

import pytest

from llm_router.commands.serve import parse_serve_args


def test_defaults_to_sse_localhost():
    o = parse_serve_args([])
    assert o.host == "127.0.0.1"
    assert o.port == 17891
    assert o.admin is False


def test_admin_uses_admin_default_port():
    o = parse_serve_args(["--admin"])
    assert o.admin is True
    assert o.port == 8080


def test_explicit_host_and_port():
    o = parse_serve_args(["--host", "0.0.0.0", "--port", "9000"])
    assert o.host == "0.0.0.0"
    assert o.port == 9000


def test_admin_with_explicit_port():
    o = parse_serve_args(["--admin", "--port", "8443"])
    assert o.admin is True and o.port == 8443


def test_bad_port_rejected():
    with pytest.raises(SystemExit):
        parse_serve_args(["--port", "not-a-number"])
