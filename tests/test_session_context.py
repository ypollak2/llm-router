"""OKF session context (#2): verified-only capture + cross-session retrieval.

The load-bearing guarantee: model free-text prose is NEVER written to the store
(that was the self-poisoning vector). Only the user's real prompt and extracted
file/symbol structure are persisted.
"""
from __future__ import annotations

import pytest

from llm_router import okf


@pytest.fixture(autouse=True)
def _okf_on(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_OKF", "on")


# ── default-on policy ────────────────────────────────────────────────────────
def test_okf_enabled_by_default(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_OKF", raising=False)
    assert okf._okf_enabled() is True


def test_okf_disabled_when_off(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_OKF", "off")
    assert okf._okf_enabled() is False


# ── verified-only capture ────────────────────────────────────────────────────
def test_records_files_and_symbols_not_prose(tmp_path):
    prompt = "refactor src/auth.py to fix the login bug"
    response = (
        "Here is my detailed reasoning about why the login flow is broken and a "
        "long fabricated explanation of the plugin API that must not be stored.\n"
        "def authenticate(user):\n    return True\n"
    )
    path = okf.record_session_turn("sess-A", prompt, response, "ollama/qwen", base=tmp_path)
    assert path is not None
    text = path.read_text()
    # Verified structure IS present...
    assert "auth.py" in text
    assert "authenticate" in text
    # ...model prose is NOT.
    assert "fabricated explanation" not in text
    assert "detailed reasoning" not in text


def test_stores_the_real_user_prompt(tmp_path):
    path = okf.record_session_turn(
        "sess-B", "update src/router.py logging", "def route(): pass", "m", base=tmp_path
    )
    assert path is not None
    assert "update src/router.py logging" in path.read_text()


def test_skips_chatter_with_no_structure(tmp_path):
    # No file, no symbol → nothing verifiable → not stored.
    path = okf.record_session_turn("sess-C", "thanks, that's great", "you're welcome", "m", base=tmp_path)
    assert path is None


def test_strips_injected_knowledge_block(tmp_path):
    prompt = "<knowledge_context>\nstale injected junk about foo.py\n</knowledge_context>\nedit real.py"
    path = okf.record_session_turn("sess-D", prompt, "def real(): pass", "m", base=tmp_path)
    assert path is not None
    text = path.read_text()
    assert "stale injected junk" not in text
    assert "real.py" in text


def test_disabled_records_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_OKF", "off")
    path = okf.record_session_turn("sess-E", "edit a.py", "def f(): pass", "m", base=tmp_path)
    assert path is None


# ── cross-session retrieval ──────────────────────────────────────────────────
def test_find_relevant_sessions_retrieves_prior_session(tmp_path):
    okf.record_session_turn(
        "sess-old", "implement retry backoff in webhooks.py", "def backoff(): pass", "m", base=tmp_path
    )
    okf.invalidate_cache()
    hits = okf.find_relevant_sessions("how did we do the webhook backoff", base=tmp_path)
    assert any("webhooks.py" in c.body or "backoff" in c.body for c in hits)


def test_find_relevant_sessions_excludes_current_session(tmp_path):
    okf.record_session_turn("sess-self", "edit parser.py grammar rules", "def parse(): pass", "m", base=tmp_path)
    okf.invalidate_cache()
    hits = okf.find_relevant_sessions(
        "parser grammar", exclude_session="sess-self", base=tmp_path
    )
    assert hits == []


def test_multiple_turns_increment(tmp_path):
    p1 = okf.record_session_turn("sess-multi", "edit one.py", "def a(): pass", "m", base=tmp_path)
    p2 = okf.record_session_turn("sess-multi", "edit two.py", "def b(): pass", "m", base=tmp_path)
    assert p1 is not None and p2 is not None
    assert p1.name == "turn-0001.md"
    assert p2.name == "turn-0002.md"
