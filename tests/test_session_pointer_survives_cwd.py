"""A session must be identifiable by a reader with a different working directory.

Measured 2026-09-14. The pointer that tells a reader "which session is current"
was written once by session-start.py, into a directory keyed on the cwd at that
moment. Consequences, all observed on this machine:

  * It went stale. Every one of the 30 pointers present was older than the 6h TTL,
    so `resolve_session_id()` returned None for all of them.
  * It was written where the reader does not look. The MCP server's cwd is $HOME,
    a directory no session ever writes a pointer to.

The effect was that routed models — Codex, Gemini, `llm()` — ran with NO session
context at all, while the hook path had it, because the hook receives session_id
in its payload and never needed the pointer.

The TTL itself is correct and must stay: a pointer that outlives its session hands
the next reader somebody else's conversation, which is worse than no context.
"""
from __future__ import annotations

import json
import time

import pytest

from llm_router import session_store as ss


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    for var in ("CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "LLM_ROUTER_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def test_a_reader_in_a_different_cwd_still_finds_the_session(state, monkeypatch, tmp_path):
    proj_a = tmp_path / "repo_a"
    proj_a.mkdir()
    monkeypatch.chdir(proj_a)
    ss.write_pointer("sess-1234")

    # A different working directory — the MCP server's situation exactly.
    elsewhere = tmp_path / "somewhere_else"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert ss.resolve_session_id() == "sess-1234", (
        "a reader outside the writing project cannot identify the session, so "
        "every routed model runs with no session context"
    )


def test_both_pointers_are_written(state, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    ss.write_pointer("sess-abcd")
    assert ss._pointer_path().exists(), "project pointer missing"
    assert ss._global_pointer_path().exists(), "machine-wide pointer missing"
    assert ss._global_pointer_path() != ss._pointer_path()


def test_a_stale_pointer_resolves_to_nothing_not_to_the_old_session(state, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    ss.write_pointer("sess-old")
    for ptr in (ss._pointer_path(), ss._global_pointer_path()):
        ptr.write_text(json.dumps(
            {"session_id": "sess-old", "ts": time.time() - ss._POINTER_MAX_AGE_SECONDS - 60}))
    assert ss.resolve_session_id() is None, (
        "a stale pointer handed the reader an old session; wrong context is worse "
        "than none"
    )


def test_an_explicit_id_and_env_still_win(state, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    ss.write_pointer("sess-pointer")
    assert ss.resolve_session_id("sess-explicit") == "sess-explicit"
    monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-env")
    assert ss.resolve_session_id() == "sess-env"


def test_a_malformed_pointer_does_not_raise(state, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    ss.write_pointer("sess-ok")
    ss._global_pointer_path().write_text("{not json")
    ss._pointer_path().write_text("{not json")
    assert ss.resolve_session_id() is None
