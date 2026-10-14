"""P1.2 into P1.1: build_pack reads the session's rolling summary.

The summary is opt-in (``LLM_ROUTER_ROLLING_SUMMARY``), comes only into a pack
(never a full one), only at the doors that know the Claude Code session, and
obeys the same privacy gate as ``recent``. Synthetic fixtures only.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from llm_router import call_identity, context_pack as cp, context_summary as cs
from llm_router.token_budget import estimate_tokens

GOLDEN = Path(__file__).parent / "fixtures" / "context_pack" / "golden_30.jsonl"
SID = "synthetic-session-1"
TEXT = "## Goal\nSYNTHETIC-GOAL\n\n## Current state\nSYNTHETIC-STATE"


@pytest.fixture
def db(monkeypatch, tmp_path):
    path = tmp_path / "sessions.db"
    monkeypatch.setenv("LLM_ROUTER_SESSIONS_PATH", str(path))
    monkeypatch.setenv("LLM_ROUTER_ROLLING_SUMMARY", "1")
    monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT", "all")
    return path


def _build(**kw):
    kw.setdefault("target_window", 100)
    kw.setdefault("door", "hook")
    kw.setdefault("session_id", SID)
    kw.setdefault("transcript_path", str(GOLDEN))
    return cp.build_pack("synthetic final request", **kw)


@pytest.mark.parametrize("door", ["hook", "agent", "mcp"])
def test_pack_carries_the_stored_summary(db, door):
    cs._store(SID, 20, TEXT, "none", "extractive", db)
    pack = _build(door=door)
    assert pack.mode == "pack"
    assert pack.summary == TEXT
    assert f"<conversation-summary>{TEXT}</conversation-summary>" in pack.system
    assert pack.tokens >= estimate_tokens(pack.system)


def test_mcp_door_resolves_the_calling_session(db, monkeypatch):
    cs._store(SID, 20, TEXT, "none", "extractive", db)
    monkeypatch.setattr(call_identity, "call_session_id", lambda: SID)
    assert _build(door="mcp", session_id=None).summary == TEXT


def test_off_by_default_reads_nothing(db, monkeypatch):
    cs._store(SID, 20, TEXT, "none", "extractive", db)
    monkeypatch.delenv("LLM_ROUTER_ROLLING_SUMMARY")

    calls = []
    monkeypatch.setattr(cs, "load_summary", lambda *a, **k: calls.append(a))
    assert _build().summary is None
    assert calls == []


def test_missing_database_is_not_created(db):
    assert _build().summary is None
    assert not db.exists()


def test_full_mode_has_no_summary(db):
    cs._store(SID, 20, TEXT, "none", "extractive", db)
    pack = _build(target_window=10**6)
    assert pack.mode == "full" and pack.summary is None


@pytest.mark.parametrize("door", ["proxy", "sdk", "gateway", "codex"])
def test_doors_without_a_claude_session_get_none(db, door):
    cs._store(SID, 20, TEXT, "none", "extractive", db)
    kw = {"messages": [{"role": "user", "content": "q " * 50}] * 20, "transcript_path": None}
    pack = _build(door=door, **kw)
    assert pack.mode == "pack" and pack.summary is None


@pytest.mark.parametrize("mode,provider,expected", [
    ("local", "openai", None),
    ("local", "ollama", TEXT),
    ("off", "ollama", None),
])
def test_summary_obeys_the_privacy_gate(db, monkeypatch, mode, provider, expected):
    cs._store(SID, 20, TEXT, "none", "extractive", db)
    monkeypatch.setenv("LLM_ROUTER_SESSION_CONTEXT", mode)
    assert _build(target_provider=provider).summary == expected


def test_unknown_session_and_bad_id_get_none(db):
    cs._store(SID, 20, TEXT, "none", "extractive", db)
    assert _build(session_id="other-session").summary is None
    assert _build(session_id="../etc/passwd").summary is None


def test_long_summary_is_capped_on_whole_lines(db):
    lines = [f"line {i} " + "w" * 60 for i in range(400)]
    cs._store(SID, 20, "\n".join(lines), "none", "extractive", db)
    summary = _build().summary
    assert estimate_tokens(summary) <= cp.SUMMARY_MAX_TOKENS
    assert summary.splitlines() == lines[: len(summary.splitlines())]


def test_one_overlong_line_is_cut_not_dropped(db):
    cs._store(SID, 20, "x" * (cp.SUMMARY_MAX_TOKENS * 10), "host", "host_compact", db)
    assert _build().summary == "x" * (cp.SUMMARY_MAX_TOKENS * 4)


def test_storage_error_fails_open(db, monkeypatch):
    cs._store(SID, 20, TEXT, "none", "extractive", db)

    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(cs, "load_summary", boom)
    pack = _build()
    assert pack.summary is None and pack.request == "synthetic final request"


def test_summary_changes_the_stable_hash(db):
    before = _build().stable_hash
    cs._store(SID, 20, TEXT, "none", "extractive", db)
    assert _build().stable_hash != before
