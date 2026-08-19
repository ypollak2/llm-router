"""Regression test for CHZ-AUD-024.

All session_context files shared a flat ~/.llm-router/ namespace, so Project B
could enumerate and read Project A's session context. This test asserts that
two projects (distinct CWDs / LLM_ROUTER_PROJECT_IDs) get isolated storage: a
glob from Project B's namespace must not surface Project A's file, and a
session written under Project A must not be loadable from Project B's scope.
"""

from __future__ import annotations

import pytest

from llm_router import session_store as ss


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("LLM_ROUTER_SESSION_CONTEXT", raising=False)
    monkeypatch.delenv("LLM_ROUTER_PROJECT_ID", raising=False)
    yield tmp_path


def test_session_files_are_project_scoped(monkeypatch):
    # Project A writes a sensitive session.
    monkeypatch.setenv("LLM_ROUTER_PROJECT_ID", "proj-alpha")
    ss.record_event("sess-a", "user_prompt", "SENSITIVE ALPHA CONTEXT")
    path_a = ss._session_path("sess-a")
    assert path_a.exists()

    # Project B has its own scope.
    monkeypatch.setenv("LLM_ROUTER_PROJECT_ID", "proj-beta")
    path_b = ss._session_path("sess-b")

    # Distinct project directories.
    assert path_a.parent != path_b.parent

    # Project B cannot enumerate Project A's session context via its own dir.
    proj_b_dir = path_b.parent
    leaked = list(proj_b_dir.glob("session_context_*.jsonl"))
    assert path_a not in leaked

    # Project B loading the same session id it does not own yields nothing.
    assert ss.load_events("sess-a") == []


def test_same_project_still_roundtrips(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_PROJECT_ID", "proj-alpha")
    ss.record_event("sess-a", "user_prompt", "hello alpha")
    assert [e["content"] for e in ss.load_events("sess-a")] == ["hello alpha"]
