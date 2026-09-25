"""X4: llm(...) calls retrieve from the CALLER's project, not the server's cwd.

2026-09-25: every Claude Code llm_router MCP server on this machine ran with
cwd=$HOME (Claude Desktop's with cwd=/), and Claude Code does not send MCP
roots — so root_from_ctx returned None, retrieval fell back to the cwd, and
llm() questions about the repo got no repo code at all (the model invented an
env var and a condition). Claude Code does put CLAUDE_CODE_SESSION_ID in each
MCP server's environment, and the prompt hook sees the session's cwd on every
prompt. The hook records it per session; the server looks up its own.
"""
import asyncio
import json
import time

from llm_router import mcp_roots, session_store


def test_the_pointer_records_the_sessions_cwd(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "home"))
    session_store.write_pointer("abc-123", cwd=str(tmp_path))
    assert session_store.read_session_cwd("abc-123") == str(tmp_path)


def test_a_stale_or_missing_record_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "home"))
    assert session_store.read_session_cwd("never-seen") is None
    session_store.write_pointer("old-1", cwd=str(tmp_path))
    p = next((tmp_path / "home").rglob("session_cwd_old-1.json"))
    p.write_text(json.dumps({"cwd": str(tmp_path), "ts": time.time() - 13 * 3600}))
    assert session_store.read_session_cwd("old-1") is None


def test_the_server_uses_its_own_sessions_project(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "home"))
    project = tmp_path / "proj"
    project.mkdir()
    session_store.write_pointer("sess-mine", cwd=str(project))
    session_store.write_pointer("sess-other", cwd=str(tmp_path))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-mine")
    assert asyncio.run(mcp_roots.root_from_ctx(None)) == project


def test_no_session_env_means_no_guess(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "home"))
    session_store.write_pointer("sess-mine", cwd=str(tmp_path))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    assert asyncio.run(mcp_roots.root_from_ctx(None)) is None
