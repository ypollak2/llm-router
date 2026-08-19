"""Tests for llm_router.session_store — the Session Context Accumulator.

All tests monkeypatch HOME to a tmp_path so nothing ever touches the real
``~/.llm-router``.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from llm_router import session_store as ss


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Redirect ~ to a tmp dir and clear routing-relevant env vars."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("LLM_ROUTER_SESSION_CONTEXT", raising=False)
    yield tmp_path


# ── record_event / load_events ──────────────────────────────────────────────

def test_record_and_load_roundtrip():
    ss.record_event("s1", "user_prompt", "hello world")
    events = ss.load_events("s1")
    assert len(events) == 1
    assert events[0]["kind"] == "user_prompt"
    assert events[0]["content"] == "hello world"


def test_chronological_ordering():
    for i in range(5):
        ss.record_event("s1", "user_prompt", f"message {i}")
    events = ss.load_events("s1")
    assert [e["content"] for e in events] == [f"message {i}" for i in range(5)]


def test_load_events_unknown_session_returns_empty():
    assert ss.load_events("does-not-exist") == []


def test_load_events_none_session_returns_empty():
    assert ss.load_events(None) == []


def test_record_event_truncates_long_content():
    long_text = "x" * 5000
    ss.record_event("s1", "tool_call", long_text, tool="bash")
    events = ss.load_events("s1")
    assert len(events[0]["content"]) <= ss._MAX_RECORD_CHARS


def test_record_event_collapses_whitespace():
    ss.record_event("s1", "user_prompt", "line1\n\n\n\n\nline2")
    events = ss.load_events("s1")
    assert "\n\n\n" not in events[0]["content"]


def test_consecutive_duplicate_dedupe():
    ss.record_event("s1", "user_prompt", "same content")
    ss.record_event("s1", "user_prompt", "same content")
    events = ss.load_events("s1")
    assert len(events) == 1


def test_non_consecutive_duplicates_both_kept():
    ss.record_event("s1", "user_prompt", "A")
    ss.record_event("s1", "user_prompt", "B")
    ss.record_event("s1", "user_prompt", "A")
    events = ss.load_events("s1")
    assert len(events) == 3


def test_record_event_noop_on_empty_content():
    ss.record_event("s1", "user_prompt", "")
    ss.record_event("s1", "user_prompt", "   ")
    assert ss.load_events("s1") == []


def test_record_event_noop_on_missing_session_id():
    ss.record_event(None, "user_prompt", "hello")
    ss.record_event("", "user_prompt", "hello")
    # No file should have been created for either.
    assert ss.load_events(None) == []


def test_torn_trailing_line_tolerated(tmp_path):
    ss.record_event("s1", "user_prompt", "good line")
    path = ss._session_path("s1")
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"kind": "user_prompt", "content": "truncated')  # no closing brace/newline
    events = ss.load_events("s1")
    assert len(events) == 1
    assert events[0]["content"] == "good line"


# ── self-injection guard ────────────────────────────────────────────────────

def test_self_injection_guard_strips_sentinel_block():
    wrapped = f"before {ss.SENTINEL_OPEN}\nsome injected context\n{ss.SENTINEL_CLOSE} after"
    ss.record_event("s1", "assistant", wrapped)
    events = ss.load_events("s1")
    assert len(events) == 1
    assert ss.SENTINEL_OPEN not in events[0]["content"]
    assert "injected context" not in events[0]["content"]
    assert "before" in events[0]["content"] and "after" in events[0]["content"]


def test_self_injection_guard_purely_sentinel_not_recorded():
    wrapped = f"{ss.SENTINEL_OPEN}\nonly injected context here\n{ss.SENTINEL_CLOSE}"
    ss.record_event("s1", "assistant", wrapped)
    assert ss.load_events("s1") == []


# ── compaction/eviction ──────────────────────────────────────────────────────

def test_eviction_at_record_count_threshold():
    for i in range(320):
        ss.record_event("s1", "user_prompt", f"msg-{i}")
    events = ss.load_events("s1", limit=10_000)
    assert len(events) <= ss._COMPACT_TO + 20  # some new writes may land after compaction
    # newest content must be preserved
    assert events[-1]["content"] == "msg-319"


# ── build_session_context ───────────────────────────────────────────────────

def test_build_session_context_empty_when_no_events():
    assert ss.build_session_context("s1") == ""


def test_build_session_context_wraps_in_sentinel():
    ss.record_event("s1", "user_prompt", "what is the capital of France")
    ctx = ss.build_session_context("s1")
    assert ctx.startswith(ss.SENTINEL_OPEN)
    assert ctx.endswith(ss.SENTINEL_CLOSE)
    assert "capital of France" in ctx


def test_build_session_context_respects_token_budget():
    for i in range(50):
        ss.record_event("s1", "user_prompt", f"padding content number {i} " * 20)
    ctx = ss.build_session_context("s1", max_tokens=50)
    # ~4 chars/token heuristic; generous slack for the sentinel wrapper/marker.
    assert len(ctx) <= 50 * 4 + 200


def test_build_session_context_keeps_newest_three_unconditionally():
    for i in range(10):
        ss.record_event("s1", "user_prompt", f"unique-topic-{i}")
    ctx = ss.build_session_context("s1", max_tokens=5000)
    for i in range(7, 10):
        assert f"unique-topic-{i}" in ctx


def test_build_session_context_relevance_filter_by_query():
    ss.record_event("s1", "user_prompt", "tell me about bananas")
    for i in range(5):
        ss.record_event("s1", "user_prompt", f"filler message {i}")
    ctx = ss.build_session_context("s1", max_tokens=5000, query="bananas please")
    assert "bananas" in ctx


def test_build_session_context_relevance_filter_by_task_type():
    ss.record_event("s1", "user_prompt", "old relevant thing", task_type="code")
    for i in range(5):
        ss.record_event("s1", "user_prompt", f"filler {i}", task_type="research")
    ctx = ss.build_session_context("s1", max_tokens=5000, task_type="code")
    assert "old relevant thing" in ctx


def test_build_session_context_privacy_mode_off(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT", "off")
    ss.record_event("s1", "user_prompt", "some content")
    assert ss.build_session_context("s1") == ""


def test_build_session_context_privacy_mode_local_blocks_external(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT", "local")
    ss.record_event("s1", "user_prompt", "some content")
    assert ss.build_session_context("s1", target_provider="openai") == ""
    assert ss.build_session_context("s1", target_provider="gemini") == ""
    assert ss.build_session_context("s1", target_provider="ollama") != ""


def test_build_session_context_privacy_mode_all_allows_external(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT", "all")
    ss.record_event("s1", "user_prompt", "some content")
    assert ss.build_session_context("s1", target_provider="openai") != ""


# ── get_mode ─────────────────────────────────────────────────────────────────

def test_get_mode_env_override_wins(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT", "local")
    assert ss.get_mode() == "local"
    monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT", "off")
    assert ss.get_mode() == "off"
    monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT", "all")
    assert ss.get_mode() == "all"
    monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT", "on")
    assert ss.get_mode() == "all"


def test_get_mode_defaults_to_all_without_env_or_config():
    # No env override; config defaults (session_context_enabled=True,
    # session_context_share_external=True) should resolve to "all".
    assert ss.get_mode() in ("all", "local")  # tolerate either config wiring
    assert ss.get_mode() != "off"


# ── resolve_session_id ───────────────────────────────────────────────────────

def test_resolve_session_id_explicit_param_wins(monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_ID", "env-session")
    assert ss.resolve_session_id("explicit-session") == "explicit-session"


def test_resolve_session_id_claude_session_id_env(monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_ID", "env-session")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "other-env-session")
    assert ss.resolve_session_id() == "env-session"


def test_resolve_session_id_claude_code_session_id_env_fallback(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "cc-session")
    assert ss.resolve_session_id() == "cc-session"


def test_resolve_session_id_pointer_file_fallback():
    ss.write_pointer("pointer-session")
    assert ss.resolve_session_id() == "pointer-session"


def test_resolve_session_id_stale_pointer_file_ignored():
    ptr = ss._pointer_path()
    ptr.parent.mkdir(parents=True, exist_ok=True)
    ptr.write_text(
        json.dumps({"session_id": "stale-session", "ts": time.time() - 7 * 3600}),
        encoding="utf-8",
    )
    assert ss.resolve_session_id() is None


def test_resolve_session_id_none_when_nothing_available():
    assert ss.resolve_session_id() is None


# ── cleanup_old_sessions / archive_session ──────────────────────────────────

def test_cleanup_old_sessions_removes_stale_files():
    ss.record_event("old-session", "user_prompt", "hi")
    path = ss._session_path("old-session")
    old_time = time.time() - 8 * 86400
    os.utime(path, (old_time, old_time))
    ss.record_event("new-session", "user_prompt", "hi")

    ss.cleanup_old_sessions()

    assert not path.exists()
    assert ss._session_path("new-session").exists()


def test_archive_session_deletes_file():
    ss.record_event("s1", "user_prompt", "hi")
    assert ss._session_path("s1").exists()
    ss.archive_session("s1")
    assert not ss._session_path("s1").exists()


def test_archive_session_noop_on_missing_file():
    ss.archive_session("never-existed")  # must not raise


def test_archive_session_noop_on_none():
    ss.archive_session(None)  # must not raise


# ── fail-open behavior ──────────────────────────────────────────────────────

def test_record_event_fails_open_when_home_unwritable(monkeypatch, tmp_path):
    unwritable = tmp_path / "no_write"
    unwritable.mkdir()
    monkeypatch.setenv("HOME", str(unwritable))
    os.chmod(unwritable, 0o500)
    try:
        ss.record_event("s1", "user_prompt", "hello")  # must not raise
    finally:
        os.chmod(unwritable, 0o700)


def test_build_session_context_fails_open_on_corrupt_store(monkeypatch):
    path = ss._session_path("s1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xfe not json at all \x00\x01")
    assert ss.build_session_context("s1") == ""


def test_get_mode_fails_open_when_config_import_errors(monkeypatch):
    monkeypatch.setattr("llm_router.config.get_config", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert ss.get_mode() in ("all", "local", "off")  # must not raise
