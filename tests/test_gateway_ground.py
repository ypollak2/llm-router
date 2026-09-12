"""`POST /ground` — the grounding check, exposed to hosts that are not this hook.

The check is structural and calls no model: it asks whether the files and
symbols a draft cites actually exist. That is what makes it worth exposing —
any host that got an answer from a cheap model can run it in milliseconds, with
no second inference and no judge, and decide whether to relay it.

The contract these tests pin hardest is what it does NOT promise. `relayable`
means "nothing here is provably invented", never "this is correct", and
`checked.symbols` must tell the truth about whether the symbol check could run
at all — the index knows THIS repo, so outside it a missing symbol is not
evidence of an invented one.
"""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from llm_router.gateway import app  # noqa: E402


@pytest.fixture
def client():
    # A non-loopback Host is rejected by the CSRF/DNS-rebinding guard, and
    # TestClient's default is `testserver`.
    return TestClient(app, base_url="http://127.0.0.1")


def _ground(client, **body):
    return client.post("/ground", json=body)


def test_a_draft_citing_a_real_file_is_relayable(client):
    r = _ground(client, draft="See src/llm_router/okf.py for the store.", context="")
    assert r.status_code == 200
    assert r.json()["relayable"] is True
    assert r.json()["violations"] == []


def test_a_draft_citing_an_invented_file_is_not(client):
    r = _ground(client, draft="See src/llm_router/does_not_exist.py.", context="")
    body = r.json()
    assert body["relayable"] is False
    assert "src/llm_router/does_not_exist.py" in body["violations"]


def test_the_response_says_which_checks_actually_ran(client):
    """Reporting a clean symbol result the check never earned would be worse
    than reporting nothing."""
    r = _ground(client, draft="Call frobnicate_thing() to fix it.", context="")
    assert r.json()["checked"] == {"paths": True, "symbols": False}


def test_symbols_are_checked_when_repo_material_was_supplied(client):
    r = _ground(client, draft="Call frobnicate_thing().",
                context="<knowledge_context>\nsome repo material\n</knowledge_context>")
    assert r.json()["checked"]["symbols"] is True


@pytest.mark.parametrize("body", [{}, {"draft": ""}, {"draft": "   "}, {"draft": 42}])
def test_a_missing_or_empty_draft_is_a_400(client, body):
    assert _ground(client, **body).status_code == 400


@pytest.mark.parametrize("bad", [{"draft": "x", "context": 5}, {"draft": "x", "prompt": []}])
def test_non_string_context_or_prompt_is_a_400(client, bad):
    assert _ground(client, **bad).status_code == 400


def test_context_and_prompt_are_optional(client):
    assert _ground(client, draft="A plain sentence with no citations.").status_code == 200


def test_the_project_scope_does_not_leak_into_the_next_request(client, tmp_path):
    """Cross-project contamination is the exact failure OKF scoping exists to
    prevent: a scope left in the environment silently re-points the NEXT
    caller's check at the previous caller's repo."""
    import os

    before = os.environ.get("LLM_ROUTER_PROJECT_ROOT")
    _ground(client, draft="See src/llm_router/okf.py.", project_root=str(tmp_path))
    assert os.environ.get("LLM_ROUTER_PROJECT_ROOT") == before


def test_the_csrf_guard_still_covers_this_route():
    """A new POST route must not be a hole in the DNS-rebinding defence."""
    hostile = TestClient(app, base_url="http://evil.example.com")
    r = hostile.post("/ground", json={"draft": "x"})
    assert r.status_code == 403


def test_relayable_is_not_a_correctness_claim(client):
    """Pinned as documentation: a draft citing only real files can still be
    completely wrong about them. Callers treating this as a quality score will
    be misled."""
    r = _ground(client, draft="src/llm_router/okf.py deletes your database on import.",
                context="")
    assert r.json()["relayable"] is True
