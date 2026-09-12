"""Stage C — the proxy learns the caller's project from a header.

The gateway has the same defect as the MCP server for the same reason: it is a
long-lived process, its cwd is wherever it was launched, and OKF retrieval scoped
to that finds the wrong project or none.

It has no roots capability, and the OpenAI/Anthropic/Ollama wire schemas are fixed,
so the body cannot carry scope for four of the five endpoints. A header can, and
works identically for every client. The native `/route` endpoint additionally
accepts a `project_root` body field, since its schema is ours.

Precedence: header, then body, then env, then cwd. The header wins because it is
the more specific signal — a caller that set it on this request meant this request.

C4, the access question, decided here rather than left implicit. A header naming an
arbitrary path is a read primitive into any project's knowledge store on the
machine — the same concern CHZ-AUD-024 raised for session logs, arriving through a
different door. Three facts bound it: the gateway binds loopback, it already
rejects cross-origin browser requests, and the store holds only file paths and
symbol names extracted from the user's own repositories.

So the default is permissive-but-real: the path must exist and be a directory,
which stops a typo silently scoping to nothing and stops a caller probing for
whether an arbitrary path exists by watching for a different error. For anyone who
wants it locked down, `LLM_ROUTER_PROJECT_ALLOWLIST` restricts scope to named
prefixes. Off by default, because a router that silently ignores a correct header
is worse than one that trusts a local caller.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from llm_router.gateway import app


@pytest.fixture
def client():
    # Loopback base_url: the gateway rejects cross-origin/non-loopback Host as
    # browser CSRF (CHZ-SEC-04), and TestClient's default is neither.
    return TestClient(app, base_url="http://127.0.0.1")


@pytest.fixture
def routed(monkeypatch):
    """Capture what reaches the router."""
    seen = {}

    async def _fake(payload):
        seen.update(payload)
        return {
            "text": "ok", "model": "ollama/x", "provider": "ollama",
            "cost_usd": 0.0, "input_tokens": 1, "output_tokens": 1,
            "complexity": "simple",
        }

    import llm_router.route_server as rs
    monkeypatch.setattr(rs, "route_payload_async", _fake)
    return seen


@pytest.fixture
def repo(tmp_path):
    d = tmp_path / "repo"
    (d / ".git").mkdir(parents=True)
    return d


# ── the header works on every wire format ───────────────────────────────────

@pytest.mark.parametrize("path,body", [
    ("/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}]}),
    ("/v1/responses", {"input": "hi"}),
    ("/v1/messages", {"max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]}),
    ("/api/chat", {"messages": [{"role": "user", "content": "hi"}]}),
    ("/api/generate", {"prompt": "hi"}),
])
def test_the_header_scopes_every_endpoint(client, routed, repo, path, body):
    r = client.post(path, json=body, headers={"X-LLM-Router-Project": str(repo)})
    assert r.status_code == 200
    assert routed.get("project_root") == str(repo)


def test_the_native_endpoint_accepts_a_body_field(client, routed, repo):
    r = client.post("/route", json={"prompt": "hi", "project_root": str(repo)})
    assert r.status_code == 200
    assert routed.get("project_root") == str(repo)


def test_the_header_beats_the_body(client, routed, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    (a / ".git").mkdir(parents=True)
    (b / ".git").mkdir(parents=True)
    client.post(
        "/route", json={"prompt": "hi", "project_root": str(b)},
        headers={"X-LLM-Router-Project": str(a)},
    )
    assert routed.get("project_root") == str(a)


# ── absent or unusable: unchanged behaviour, never an error ─────────────────

def test_no_header_leaves_scope_alone(client, routed):
    r = client.post("/v1/chat/completions",
                    json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert not routed.get("project_root")


def test_a_nonexistent_path_is_ignored_not_an_error(client, routed):
    r = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}]},
        headers={"X-LLM-Router-Project": "/no/such/repo"},
    )
    assert r.status_code == 200, "a bad header must not fail the request"
    assert not routed.get("project_root")


def test_a_file_rather_than_a_directory_is_ignored(client, routed, tmp_path):
    f = tmp_path / "a-file.txt"
    f.write_text("x", encoding="utf-8")
    client.post("/v1/chat/completions",
                json={"messages": [{"role": "user", "content": "hi"}]},
                headers={"X-LLM-Router-Project": str(f)})
    assert not routed.get("project_root")


def test_an_empty_header_is_ignored(client, routed):
    client.post("/v1/chat/completions",
                json={"messages": [{"role": "user", "content": "hi"}]},
                headers={"X-LLM-Router-Project": "   "})
    assert not routed.get("project_root")


# ── C4: the opt-in allowlist ────────────────────────────────────────────────

def test_the_allowlist_admits_a_path_underneath_it(client, routed, repo, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_PROJECT_ALLOWLIST", str(repo.parent))
    client.post("/route", json={"prompt": "hi"},
                headers={"X-LLM-Router-Project": str(repo)})
    assert routed.get("project_root") == str(repo)


def test_the_allowlist_refuses_a_path_outside_it(client, routed, repo, tmp_path, monkeypatch):
    other = tmp_path / "elsewhere"
    (other / ".git").mkdir(parents=True)
    monkeypatch.setenv("LLM_ROUTER_PROJECT_ALLOWLIST", str(repo))
    r = client.post("/route", json={"prompt": "hi"},
                    headers={"X-LLM-Router-Project": str(other)})
    assert r.status_code == 200, "a refused scope narrows context, it does not fail"
    assert not routed.get("project_root")


def test_the_allowlist_is_not_defeated_by_traversal(client, routed, repo, monkeypatch):
    """`/allowed/../elsewhere` must not read as being under `/allowed`."""
    monkeypatch.setenv("LLM_ROUTER_PROJECT_ALLOWLIST", str(repo))
    sneaky = str(repo) + "/../elsewhere"
    client.post("/route", json={"prompt": "hi"},
                headers={"X-LLM-Router-Project": sneaky})
    assert not routed.get("project_root")


def test_a_prefix_string_match_is_not_enough(client, routed, tmp_path, monkeypatch):
    """`/srv/app-evil` must not pass an allowlist of `/srv/app`."""
    allowed = tmp_path / "app"
    (allowed / ".git").mkdir(parents=True)
    evil = tmp_path / "app-evil"
    (evil / ".git").mkdir(parents=True)
    monkeypatch.setenv("LLM_ROUTER_PROJECT_ALLOWLIST", str(allowed))
    client.post("/route", json={"prompt": "hi"},
                headers={"X-LLM-Router-Project": str(evil)})
    assert not routed.get("project_root")
