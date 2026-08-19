"""Regression: CHZ-SEC-05 — dashboard served its auth token on the unauth index.

`auth_middleware` exempted "/" from auth, and the index injects the token into
the page (window.DASHBOARD_TOKEN). So any unauthenticated request to the port
received the token and could then call every API. The exemption is removed: an
unauthenticated GET / now returns 401 with no token; the tokenized URL works.

The server binds to localhost only; the test drives it on a random port in a
background thread.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

pytest.importorskip("aiohttp")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture()
def running_dashboard(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".llm-router").mkdir(parents=True, exist_ok=True)

    # Import after HOME is set so the token file lands in the temp home.
    import importlib

    from llm_router.dashboard import server as dash
    importlib.reload(dash)
    monkeypatch.setattr(dash, "_TOKEN_FILE", tmp_path / ".llm-router" / "dashboard.token")

    port = _free_port()
    token = dash._get_or_create_token()

    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def _serve():
        asyncio.set_event_loop(loop)
        loop.create_task(dash.run(port=port))
        loop.call_soon(ready.set)
        loop.run_forever()

    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    ready.wait(timeout=5)
    # give TCPSite a moment to bind
    for _ in range(50):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=0.2)
        except urllib.error.HTTPError:
            break  # server is up (401)
        except Exception:
            time.sleep(0.1)
    yield port, token
    loop.call_soon_threadsafe(loop.stop)


def test_unauth_index_returns_401_and_no_token(running_dashboard):
    port, token = running_dashboard
    try:
        resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=3)
        body = resp.read().decode()
        got_status = resp.status
    except urllib.error.HTTPError as e:
        got_status = e.code
        body = e.read().decode()
    assert got_status == 401, f"unauth GET / should be 401, got {got_status}"
    assert token not in body, "CHZ-SEC-05: token leaked in unauthenticated index response"


def test_tokenized_index_serves_page(running_dashboard):
    port, token = running_dashboard
    resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/?token={token}", timeout=3)
    assert resp.status == 200
    body = resp.read().decode()
    assert "DASHBOARD_TOKEN" in body, "authenticated index should serve the page"
