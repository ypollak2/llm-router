"""M-08 — the gateway had no per-request authentication.

Every route on this gateway can trigger a real, billed model call. The audit
found one grep hit for auth: a comment acknowledging the gap.

**What was already true**, and is worth stating precisely because the audit
understated it: the bind is gated (`net_bind.refuse_public_bind_or_exit`,
loopback by default) and `_guard_cross_origin` rejects browser CSRF and DNS
rebinding before any handler runs. This was never an open port.

**What was missing**: any *other local process* — a malicious dependency, another
user on a shared machine, a compromised browser extension — could spend money
through it, because reaching loopback is all it took.

**Why enforcement is opt-in.** Making a token mandatory would break every
existing user on upgrade: clients set `OPENAI_BASE_URL`/`ANTHROPIC_BASE_URL` and
send no `Authorization` header. A remediation that silently stops working traffic
is a worse outcome than the gap it closes. So auth turns on when an operator
supplies a token and stays off otherwise.

The check lives in the **middleware**, not in each handler, because a per-handler
check is how the next endpoint ships unguarded.
"""

from __future__ import annotations

import pathlib

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from llm_router import gateway  # noqa: E402

LOOPBACK = {"Host": "127.0.0.1:8080"}
BODY = {"model": "gpt-4o", "messages": [{"role": "user", "content": "hello"}]}


@pytest.fixture
def client():
    return TestClient(gateway.app, raise_server_exceptions=False)


@pytest.fixture
def token(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_GATEWAY_TOKEN", "s3cret-token-value")
    return "s3cret-token-value"


def test_no_token_configured_means_no_enforcement(client, tmp_path, monkeypatch):
    """Non-breaking by design: existing clients keep working.

    Asserts only that the request is not rejected as UNAUTHORISED — routing may
    still fail for unrelated reasons, and conflating the two would make this
    test pass for the wrong reason.
    """
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.delenv("LLM_ROUTER_GATEWAY_TOKEN", raising=False)
    r = client.post("/v1/chat/completions", headers=LOOPBACK, json=BODY)
    assert r.status_code != 401, "auth was enforced without a configured token"


def test_missing_header_is_rejected_when_a_token_is_configured(client, token):
    r = client.post("/v1/chat/completions", headers=LOOPBACK, json=BODY)
    assert r.status_code == 401, (
        f"an unauthenticated request reached a billed route ({r.status_code})"
    )


def test_wrong_token_is_rejected(client, token):
    r = client.post("/v1/chat/completions",
                    headers={**LOOPBACK, "Authorization": "Bearer wrong"}, json=BODY)
    assert r.status_code == 401


@pytest.mark.parametrize("header", [
    "Basic s3cret-token-value",     # wrong scheme
    "s3cret-token-value",           # no scheme
    "Bearer",                       # scheme only
    "Bearer ",
    "",
])
def test_malformed_authorization_is_rejected(client, token, header):
    r = client.post("/v1/chat/completions",
                    headers={**LOOPBACK, "Authorization": header}, json=BODY)
    assert r.status_code == 401, f"{header!r} was accepted"


def test_the_correct_token_is_not_rejected(client, token):
    """Anti-over-correction: a gate nothing can pass is not a gate."""
    r = client.post("/v1/chat/completions",
                    headers={**LOOPBACK, "Authorization": f"Bearer {token}"}, json=BODY)
    assert r.status_code != 401, "the configured token was refused"


def test_bearer_scheme_is_case_insensitive(client, token):
    """RFC 7235 makes the scheme case-insensitive; clients vary."""
    r = client.post("/v1/chat/completions",
                    headers={**LOOPBACK, "Authorization": f"bearer {token}"}, json=BODY)
    assert r.status_code != 401


def test_auth_covers_every_route_not_just_one(client, token):
    """Middleware, not per-handler. A per-handler check misses the next endpoint."""
    for path, body in [
        ("/v1/chat/completions", BODY),
        ("/v1/messages", {"model": "claude-opus-4-6",
                          "messages": [{"role": "user", "content": "hi"}]}),
        ("/v1/responses", {"model": "gpt-4o", "input": "hi"}),
    ]:
        r = client.post(path, headers=LOOPBACK, json=body)
        assert r.status_code == 401, f"{path} is not behind the auth check"


def test_comparison_is_constant_time():
    """A timing oracle on a loopback socket is cheap for a same-machine attacker."""
    src = pathlib.Path(gateway.__file__).read_text(encoding="utf-8")
    body = src.split("def _check_gateway_auth")[1].split("\n@app")[0]
    assert "compare_digest" in body, (
        "the token is compared with ==, which leaks its length and prefix to the "
        "local process this check exists to stop"
    )


def test_a_token_file_is_never_created_implicitly(tmp_path, monkeypatch):
    """A token appearing by itself would enable auth on upgrade and break traffic."""
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.delenv("LLM_ROUTER_GATEWAY_TOKEN", raising=False)
    assert gateway.gateway_token() is None
    assert not (tmp_path / "gateway.token").exists(), (
        "reading the token created it; that would silently turn auth on"
    )


def test_a_token_file_is_honoured_when_present(tmp_path, monkeypatch):
    """Env for containers, file for a persistent local setup."""
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    monkeypatch.delenv("LLM_ROUTER_GATEWAY_TOKEN", raising=False)
    (tmp_path / "gateway.token").write_text("from-file\n", encoding="utf-8")
    assert gateway.gateway_token() == "from-file"


def test_env_wins_over_file(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    (tmp_path / "gateway.token").write_text("from-file", encoding="utf-8")
    monkeypatch.setenv("LLM_ROUTER_GATEWAY_TOKEN", "from-env")
    assert gateway.gateway_token() == "from-env"


def test_the_cross_origin_guard_still_runs_first(client, token):
    """Defence in depth ordering: a browser CSRF attempt is 403, not 401.

    401 invites a client to retry with credentials; a cross-origin browser
    request should never get that far.
    """
    r = client.post("/v1/chat/completions", json=BODY)   # no loopback Host
    assert r.status_code == 403
