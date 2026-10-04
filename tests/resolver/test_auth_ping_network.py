"""The key-check ping against REAL local servers: it must never follow a redirect,
and never reach a proxy. Fake keys only; every server binds 127.0.0.1 on a free port.
"""

from __future__ import annotations

import http.server
import json
import os
import socket
import stat
import threading

import pytest

from llm_router.resolver import auth_ping

FAKE_KEY = "sk-test-FAKE-NOT-A-REAL-KEY-0000"


class _Server:
    """A throwaway HTTP server. ``routes`` maps a path to (status, headers) and every
    request (path + all headers) is recorded."""

    def __init__(self, routes):
        outer = self
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.routes = routes

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                outer.requests.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
                status, headers = outer.routes.get(self.path, (200, {}))
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def keys_seen(self):
        return [h for _p, h in self.requests if FAKE_KEY in "".join(h.values())]


@pytest.fixture
def servers():
    made = []

    def make(routes):
        srv = _Server(routes)
        made.append(srv)
        return srv

    yield make
    for s in made:
        s.close()


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_a_redirect_to_another_host_is_not_followed_and_the_second_host_gets_no_key(servers, code):
    b = servers({})
    a = servers({"/": (code, {"Location": f"{b.base}/steal"})})
    status = auth_ping._ping_url(a.base + "/", "bearer", FAKE_KEY)
    assert status == code                       # the 3xx comes back as the status, nothing more
    assert len(a.keys_seen()) == 1              # the real endpoint did get the key (premise)
    assert b.requests == []                     # the redirect target was never contacted
    assert b.keys_seen() == []


@pytest.mark.parametrize("style", ["bearer", "anthropic", "google"])
def test_no_header_style_reaches_the_redirect_target(servers, style):
    b = servers({})
    a = servers({"/": (302, {"Location": f"{b.base}/x"})})
    assert auth_ping._ping_url(a.base + "/", style, FAKE_KEY) == 302
    assert b.requests == []


def test_a_same_host_redirect_is_not_followed_either(servers):
    a = servers({"/start": (302, {"Location": "/final"})})
    assert auth_ping._ping_url(a.base + "/start", "bearer", FAKE_KEY) == 302
    assert [p for p, _ in a.requests] == ["/start"]            # /final was never requested


def test_a_redirect_is_reported_as_unexpected_never_as_verified(servers, tmp_path):
    a = servers({"/": (302, {"Location": "http://127.0.0.1:9/"})})
    verdict, status = auth_ping.verify_provider(
        "openai", FAKE_KEY, ping=lambda p, k: auth_ping._ping_url(a.base + "/", "bearer", k),
        now=1.0, cache=tmp_path / "c.json")
    assert (verdict, status) == (auth_ping.UNEXPECTED, 302)
    assert not (tmp_path / "c.json").exists()                  # not cached


def test_a_plain_200_and_a_401_pass_through(servers):
    ok = servers({"/": (200, {})})
    no = servers({"/": (401, {})})
    assert auth_ping._ping_url(ok.base + "/", "bearer", FAKE_KEY) == 200
    assert auth_ping._ping_url(no.base + "/", "bearer", FAKE_KEY) == 401


def test_a_closed_port_is_none_not_an_exception():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert auth_ping._ping_url(f"http://127.0.0.1:{port}/", "bearer", FAKE_KEY) is None


_PROXY_VARS = ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY")


def _point_proxies_at(monkeypatch, base):
    for var in _PROXY_VARS:
        monkeypatch.setenv(var, base)
    for var in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(var, raising=False)


def test_proxy_environment_variables_are_ignored(servers, monkeypatch):
    # A non-loopback, unresolvable host: urllib never bypasses the proxy for it, and a
    # direct connection cannot succeed, so the only way it reaches anything is via the proxy.
    proxy = servers({})
    _point_proxies_at(monkeypatch, proxy.base)
    assert auth_ping._ping_url("http://ping-target.invalid/v1/models", "bearer", FAKE_KEY) is None
    assert proxy.requests == []                                # the proxy saw nothing, not even the host


def test_the_premise_default_urllib_forwards_the_key_to_a_redirect_target_and_uses_a_proxy(servers, monkeypatch):
    """Guards the guards: with urllib's DEFAULT behaviour the key does reach the second
    host and a proxy does see the request, so the tests above can go red."""
    import urllib.request

    b = servers({})
    a = servers({"/": (302, {"Location": f"{b.base}/steal"})})
    # A fresh default opener: urlopen() caches one whose proxies were read at first use.
    urllib.request.build_opener().open(
        urllib.request.Request(a.base + "/", headers={"Authorization": f"Bearer {FAKE_KEY}"}), timeout=5).close()
    assert len(b.keys_seen()) == 1

    proxy = servers({})
    _point_proxies_at(monkeypatch, proxy.base)
    try:
        urllib.request.build_opener().open("http://ping-target.invalid/v1/models", timeout=5).close()
    except Exception:  # noqa: BLE001 - the fake proxy's reply is irrelevant
        pass
    assert proxy.requests != []


# --------------------------------------------------------------- endpoint table

DOCUMENTED_HOSTS = {
    "openai": "api.openai.com", "anthropic": "api.anthropic.com",
    "gemini": "generativelanguage.googleapis.com", "openrouter": "openrouter.ai",
    "deepseek": "api.deepseek.com", "groq": "api.groq.com", "xai": "api.x.ai",
    "mistral": "api.mistral.ai", "together": "api.together.ai",
    "moonshot": "api.moonshot.ai", "cohere": "api.cohere.com",
}


def test_the_endpoint_table_is_pinned():
    from urllib.parse import urlsplit

    assert set(auth_ping.ENDPOINTS) == set(DOCUMENTED_HOSTS)
    hosts = {}
    for provider, (url, style) in auth_ping.ENDPOINTS.items():
        parts = urlsplit(url)
        assert parts.scheme == "https", provider
        assert parts.hostname == DOCUMENTED_HOSTS[provider], provider
        assert parts.username is None and parts.password is None and parts.port is None, provider
        assert style in ("bearer", "anthropic", "google"), provider
        hosts.setdefault(parts.hostname, []).append(provider)
    assert all(len(v) == 1 for v in hosts.values()), hosts          # one host per provider, none shared
    assert auth_ping.ENDPOINTS["openrouter"][0] == "https://openrouter.ai/api/v1/key"
    assert auth_ping.ENDPOINTS["together"][0] == "https://api.together.ai/v1/models"


def test_the_table_comment_records_where_and_when_it_was_verified():
    import inspect

    src = inspect.getsource(auth_ping)
    assert "Verified 2026-10-04 by an UNAUTHENTICATED GET" in src and ".cn" in src


# ----------------------------------------------------------------- cache file mode

def test_the_cache_file_is_private_even_under_a_permissive_umask(tmp_path):
    old = os.umask(0o000)
    try:
        auth_ping.verify_provider("openai", FAKE_KEY, ping=lambda p, k: 200, now=1.0,
                                  cache=tmp_path / "c.json")
        assert stat.S_IMODE((tmp_path / "c.json").stat().st_mode) == 0o600
        auth_ping.verify_provider("groq", FAKE_KEY, ping=lambda p, k: 401, now=2.0,
                                  cache=tmp_path / "c.json")        # rewrite keeps it private
        assert stat.S_IMODE((tmp_path / "c.json").stat().st_mode) == 0o600
        assert set(json.loads((tmp_path / "c.json").read_text())) == {"openai", "groq"}
    finally:
        os.umask(old)
