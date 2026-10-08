"""North Star MCP-attribution bug (audit 2026-09-28): a real ``llm_edit`` MCP
call wrote ``edit_outcomes.jsonl`` rows with ``session_id: ""`` because the
writer read ``CLAUDE_SESSION_ID`` directly from the environment — a variable
the long-lived MCP server process never reliably sees. Every other MCP-path
writer resolves the session id through
``llm_router.session_store.resolve_session_id()`` (env vars, THEN the
``current_session.json`` pointer a hook wrote); ``edit_ledger.py`` must do
the same, and must write ``null`` (never ``""``) when even that fails.
"""
from __future__ import annotations

import json

import pytest

from llm_router import edit_ledger, session_store


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Same isolation as tests/test_session_store.py: no real ~/.llm-router,
    no leaking CLAUDE_SESSION_ID from the environment this test runs in."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "router_home"))
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    yield tmp_path


def _ledger_path(tmp_path) -> "object":
    from llm_router import paths
    return paths.state_path(edit_ledger.LEDGER_FILENAME)


def _last_row(tmp_path) -> dict:
    path = _ledger_path(tmp_path)
    lines = path.read_text(encoding="utf-8").splitlines()
    return json.loads(lines[-1])


def test_mcp_path_write_carries_the_resolved_session_id(tmp_path):
    """The MCP server never sees CLAUDE_SESSION_ID in its own environment —
    only the current_session.json pointer a hook wrote for this session is
    available to it. record_edit_outcome must resolve through that pointer,
    the same way routing_quality.jsonl's MCP-path rows already do (router.py
    -> session_store.resolve_session_id), not through a direct env read.
    """
    session_store.write_pointer("real-session-abc123")

    edit_ledger.record_edit_outcome(file="src/foo.py", model="codex/gpt-5.5", applied=True)

    row = _last_row(tmp_path)
    assert row["session_id"] == "real-session-abc123"


def test_unresolvable_session_id_writes_null_not_empty_string(tmp_path):
    """No env var, no pointer file at all (a cold MCP server, first call of a
    session before any hook has written a pointer): the row must carry JSON
    null, which round-trips to Python None — never the empty string, which
    silently sank every one of these rows in the North Star join."""
    edit_ledger.record_edit_outcome(file="src/bar.py", model="ollama/qwen3.5", applied=True)

    row = _last_row(tmp_path)
    assert row["session_id"] is None
    assert row["session_id"] != ""


def test_resolved_session_id_wins_over_env_when_pointer_is_stale(tmp_path, monkeypatch):
    """Sanity check on the resolver itself (not a new claim about edit_ledger):
    an explicit CLAUDE_SESSION_ID still wins, matching session_store's own
    documented precedence, so a shell-launched harness that DOES set the env
    var is unaffected by this change."""
    monkeypatch.setenv("CLAUDE_SESSION_ID", "env-session-xyz")

    edit_ledger.record_edit_outcome(file="src/baz.py", model="codex/gpt-5.5", applied=True)

    row = _last_row(tmp_path)
    assert row["session_id"] == "env-session-xyz"


def test_zero_claude_row_without_a_payload_session_id_is_null_not_the_pointer_guess(tmp_path):
    """M0.3c / M0.4: a zero-Claude row carries the hook payload's own id or NULL. The pointer file
    belongs to whichever session wrote last, so using it would credit a turn to the wrong session."""
    session_store.write_pointer("pointer-session")

    edit_ledger.record_edit_outcome(file="a.py", model="ollama/x", applied=True, source="zero_claude")
    assert _last_row(tmp_path)["session_id"] is None

    edit_ledger.record_edit_outcome(file="a.py", model="ollama/x", applied=True, source="zero_claude",
                                    session_id="payload-session")
    assert _last_row(tmp_path)["session_id"] == "payload-session"

    # llm_edit runs in the MCP server, which has no payload: it still resolves through the pointer.
    edit_ledger.record_edit_outcome(file="a.py", model="ollama/x", applied=True, source="llm_edit")
    assert _last_row(tmp_path)["session_id"] == "pointer-session"
