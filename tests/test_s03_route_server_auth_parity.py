"""`route_server` must honour the same bearer token as the gateway — S-03.

The gateway grew an opt-in `Authorization: Bearer` check. `route_server`
exposes the SAME router over HTTP — the same routing, the same paid provider
calls — and had no auth check at all. The file's own comment in `main()`
acknowledged the gap while only refusing a public bind.

It shares the gateway's token deliberately. An operator who sets
`LLM_ROUTER_GATEWAY_TOKEN` is protecting "the router over HTTP"; asking them to
configure that twice is how one of the two ends up unset.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from llm_router import route_server as rs

TOKEN = "s03-test-token-not-a-real-secret"


@pytest.fixture
def server(monkeypatch, tmp_path):
    """A real loopback server, so the assertions are about HTTP, not a function."""
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))

    def _fake_route_payload(payload):
        return {"text": "ok", "model": "m", "provider": "p",
                "input_tokens": 1, "output_tokens": 1, "cost_usd": 0.0}

    monkeypatch.setattr(rs, "route_payload", _fake_route_payload)

    import http.server
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), rs.make_handler())
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def _post(base, path="/route", token=None, body=None):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(body or {"prompt": "hi"}).encode("utf-8"),
        headers={"Content-Type": "application/json", "Host": "127.0.0.1"},
        method="POST",
    )
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


# ── the gate ─────────────────────────────────────────────────────────────────

def test_an_unauthenticated_request_is_refused_when_a_token_is_configured(
    server, monkeypatch
):
    monkeypatch.setenv("LLM_ROUTER_GATEWAY_TOKEN", TOKEN)
    status, body = _post(server)
    assert status == 401, f"route_server served an unauthenticated request: {body}"
    assert "Bearer" in body.get("error", "")


def test_a_wrong_token_is_refused(server, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_GATEWAY_TOKEN", TOKEN)
    status, body = _post(server, token="not-the-token")
    assert status == 401
    assert "invalid" in body.get("error", "").lower()


def test_the_correct_token_is_accepted(server, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_GATEWAY_TOKEN", TOKEN)
    status, body = _post(server, token=TOKEN)
    assert status == 200, f"a correctly authenticated request was refused: {body}"
    assert body["text"] == "ok"


def test_the_feedback_endpoint_is_protected_too(server, monkeypatch):
    """Both POST paths, not just the expensive one.

    `/feedback` writes routing-quality records that the bandit reads. An
    unauthenticated writer there poisons model selection without spending a
    cent, which is cheaper for an attacker than abusing `/route`.
    """
    monkeypatch.setenv("LLM_ROUTER_GATEWAY_TOKEN", TOKEN)
    status, _ = _post(server, path="/feedback", body={"verdict": "good"})
    assert status == 401


# ── opt-in: an upgrade must not break working traffic ────────────────────────

def test_no_token_configured_means_no_check(server, monkeypatch):
    """Anti-vacuity, and the compatibility promise.

    If this fails the other way — 401 with no token set — every existing
    operator's install stops working on upgrade, which is a worse outcome than
    the gap being closed.
    """
    monkeypatch.delenv("LLM_ROUTER_GATEWAY_TOKEN", raising=False)
    status, body = _post(server)
    assert status == 200, f"auth fired with no token configured: {body}"


def test_health_stays_open(server, monkeypatch):
    """A liveness probe that needs a secret is not a liveness probe."""
    monkeypatch.setenv("LLM_ROUTER_GATEWAY_TOKEN", TOKEN)
    with urllib.request.urlopen(server + "/health", timeout=10) as r:
        assert r.status == 200


# ── one token, not two ───────────────────────────────────────────────────────

def test_it_uses_the_gateway_token_not_a_second_one():
    """Two token settings means one of them ends up unset.

    Was two `"phrase" in inspect.getsource(rs)` substring checks. A comment
    reproducing either phrase — the import line, or the forbidden env var
    name — would satisfy or defeat them without the real import existing.
    The import half is now an `ast.ImportFrom` check (module
    `llm_router.gateway`, alias `gateway_token`); the "no second token"
    half uses `assert_not_in_strings`, which only looks at STRING CONSTANTS
    the module actually holds (e.g. as an `os.environ` key), not comments —
    a second, separate token would need to appear as a real string the code
    reads, not prose about not adding one.
    """
    import ast
    import inspect
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _ast_assert import assert_not_in_strings

    tree = ast.parse(inspect.getsource(rs))
    imports_gateway_token = any(
        isinstance(n, ast.ImportFrom)
        and n.module == "llm_router.gateway"
        and any(alias.name == "gateway_token" for alias in n.names)
        for n in ast.walk(tree)
    )
    assert imports_gateway_token, (
        "route_server no longer imports gateway_token from llm_router.gateway"
    )
    assert_not_in_strings(
        rs, "LLM_ROUTER_ROUTE_SERVER_TOKEN",
        msg="route_server introduced a second, separate token",
    )


def test_the_stale_no_auth_comment_is_gone():
    """The file said "no auth checks at all". That must not survive the fix.

    NOT converted to an AST assertion. The property under test is the
    CONTENT OF A DOCSTRING — the misleading sentence the file used to carry
    about itself — and `tests/_ast_assert.py` deliberately EXCLUDES
    docstrings from `string_constants` (its own docstring: asserting on a
    docstring "is the same mistake one layer in"). Routing this through the
    AST helper would make the check vacuously pass regardless of whether the
    stale sentence is still there, which is strictly WEAKER than the
    text check it would replace. A plain `inspect.getsource` scan is the
    correct tool for "was this specific sentence rewritten", and there is no
    call site, string constant, or attribute access for it to be confused
    with — the A-10 evasion (a phrase in a comment near unrelated code)
    doesn't apply to a test that is itself checking documentation prose.
    """
    import inspect

    src = inspect.getsource(rs)
    assert "has no auth checks at all" not in src
