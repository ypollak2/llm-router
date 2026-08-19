"""Regression test for CHZ-AUD-022.

session_store.record_event persisted full prompt/response content verbatim
to per-session JSONL files. Two protections are asserted here:

  1. With LLM_ROUTER_SESSION_CONTEXT=off, no JSONL file is created and no content
     is written (privacy kill-switch honored at the write boundary).
  2. Common secret patterns embedded in content are redacted before the line
     is written to disk.
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


def test_session_context_off_writes_nothing(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT", "off")
    ss.record_event("sid", "user_prompt", "CANARY_PROMPT_TEXT")
    path = ss._session_path("sid")
    assert not path.exists(), "JSONL created despite LLM_ROUTER_SESSION_CONTEXT=off"
    assert ss.load_events("sid") == []


def test_secret_in_content_is_redacted_before_persist():
    secret = "sk-ant-api03-" + "B" * 40
    ss.record_event("sid", "user_prompt", f"my key is {secret} please use it")
    path = ss._session_path("sid")
    raw = path.read_text(encoding="utf-8")
    assert secret not in raw, f"raw secret persisted to JSONL: {raw!r}"
    assert "REDACTED" in raw
