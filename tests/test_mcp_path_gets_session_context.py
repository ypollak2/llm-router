"""A routed model on the MCP path must receive THIS session's context.

N2. The session-pointer fix (222bbfd) was verified by unit test and by a manual
`resolve_session_id()` check, never end to end. That is the gap this closes: the
fix claims a Codex/Gemini/`llm()` call now sees the session's own conversation and
tool facts, and nothing asserted it.

The defect it guards against, measured 2026-09-14:

    resolve_session_id() with the MCP server's environment -> None

The server runs with cwd=$HOME and no session env var, and the pointer was written
once at session start into a directory keyed on the cwd AT THAT MOMENT. All 30
pointers on the machine were past the 6h TTL. So the retrieval machinery ran on
nothing, every time, while the hook path — which receives session_id in its
payload — worked fine and hid the problem.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from llm_router import session_store as ss
from llm_router.context import build_context_messages


@pytest.fixture
def a_session_with_history(tmp_path, monkeypatch):
    """A real session store, and a process that has no idea which session it is."""
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    for var in ("CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "LLM_ROUTER_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT", "all")

    work = tmp_path / "a_project"
    work.mkdir()
    monkeypatch.chdir(work)

    sid = "abcd1234"
    ss.record_event(sid, "user_prompt", "fix the retry ordering", role="user")
    ss.record_event(sid, "tool_call", 'Bash({"command": "pytest -q"}) -> 3 failed',
                    role="assistant")
    ss.write_pointer(sid)          # what the hook now does on EVERY prompt
    return sid, work


def _context_as_the_mcp_server_sees_it(elsewhere, project_root=None) -> str:
    """Resolve and build context from a DIFFERENT cwd, with no session env var.

    `project_root` is what the MCP router passes: the project the CALLER asked
    about, resolved from the client's workspace roots. Omitting it is the bug —
    the bucket then comes from this process's cwd, which for the real server is
    $HOME.
    """
    cwd = os.getcwd()
    try:
        os.chdir(elsewhere)
        msgs = asyncio.run(build_context_messages(
            caller_context="keep going with that", target_provider="codex",
            is_free_model=True,
            project_root=str(project_root) if project_root else None))
        return " ".join(m.get("content", "") for m in msgs)
    finally:
        os.chdir(cwd)


def test_the_routed_model_receives_this_sessions_tool_facts(a_session_with_history, tmp_path):
    sid, _ = a_session_with_history          # _ is the project root the caller declares
    elsewhere = tmp_path / "not_the_project"
    elsewhere.mkdir()

    blob = _context_as_the_mcp_server_sees_it(elsewhere, project_root=_)
    assert "pytest -q" in blob, (
        "a routed model got no tool facts from the live session. The machinery "
        "exists (build_context_messages layer 2b); what fails is IDENTITY — the "
        "process could not tell which session it was serving."
    )


def test_identity_survives_a_reader_in_another_directory(a_session_with_history, tmp_path):
    sid, _ = a_session_with_history
    elsewhere = tmp_path / "somewhere"
    elsewhere.mkdir()
    cwd = os.getcwd()
    try:
        os.chdir(elsewhere)
        assert ss.resolve_session_id() == sid
    finally:
        os.chdir(cwd)


def test_a_stale_pointer_yields_no_context_rather_than_the_wrong_session(
        a_session_with_history, tmp_path, monkeypatch):
    import json
    import time

    sid, _ = a_session_with_history
    for ptr in (ss._pointer_path(), ss._global_pointer_path()):
        ptr.write_text(json.dumps(
            {"session_id": sid, "ts": time.time() - ss._POINTER_MAX_AGE_SECONDS - 60}))
    elsewhere = tmp_path / "later"
    elsewhere.mkdir()
    blob = _context_as_the_mcp_server_sees_it(elsewhere, project_root=_)
    assert "pytest -q" not in blob, (
        "an expired pointer handed a routed model an old session's conversation. "
        "Wrong context is worse than none — it is the shape of the semantic-cache "
        "incident that served one passport's answer for another."
    )


def test_the_privacy_gate_still_applies_to_paid_providers(a_session_with_history, tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT", "local")
    elsewhere = tmp_path / "paid"
    elsewhere.mkdir()
    cwd = os.getcwd()
    try:
        os.chdir(elsewhere)
        msgs = asyncio.run(build_context_messages(
            caller_context="keep going", target_provider="openai", is_free_model=False))
        blob = " ".join(m.get("content", "") for m in msgs)
    finally:
        os.chdir(cwd)
    assert "pytest -q" not in blob, (
        "session content reached an external paid provider under privacy mode "
        "'local'; identity must not become an egress hole"
    )
