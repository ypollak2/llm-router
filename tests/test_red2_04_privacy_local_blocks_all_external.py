"""Regression: RED2-04 — LLM_ROUTER_SESSION_CONTEXT=local must block context egress
to ANY non-free-local provider, not just openai/gemini.

The privacy gate hardcoded `target_provider in ("openai","gemini")`, so
Perplexity (which every research-task prompt is routed to) received full session
history in `local` mode. The gate is now an inverted check against the
free-local set {ollama, codex, gemini_cli}: any provider outside it is blocked
under `local`.
"""

from __future__ import annotations

import pytest

from llm_router import session_store


@pytest.fixture()
def local_session(tmp_path, monkeypatch):
    # Isolate ~/.llm-router and force local privacy mode.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT", "local")
    (tmp_path / ".llm-router").mkdir(parents=True, exist_ok=True)
    sid = "priv-sess-1"
    session_store.record_event(sid, "routed_qa", "the deploy key is SECRET123",
                               role="assistant", task_type="query")
    return sid


def test_local_blocks_perplexity(local_session):
    ctx = session_store.build_session_context(local_session, target_provider="perplexity")
    assert ctx == "", "RED2-04: local mode leaked session context to Perplexity"


def test_local_blocks_openai_and_gemini_still(local_session):
    # Regression of the original (correct) behavior.
    assert session_store.build_session_context(local_session, target_provider="openai") == ""
    assert session_store.build_session_context(local_session, target_provider="gemini") == ""


def test_local_blocks_unknown_provider(local_session):
    # Fail-closed: an unrecognized/None target is treated as external under local.
    assert session_store.build_session_context(local_session, target_provider="some_new_api") == ""
    assert session_store.build_session_context(local_session, target_provider=None) == ""


def test_local_allows_free_local_providers(local_session):
    # A genuinely local/free provider still receives context under local mode.
    ctx = session_store.build_session_context(local_session, target_provider="ollama")
    assert ctx != "", "local mode must still deliver context to free-local providers"
    assert "SECRET123" in ctx  # the recorded content is present for the local target
