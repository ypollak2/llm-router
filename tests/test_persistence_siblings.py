"""CHZ-AUD-D-01/D-02/D-04 (siblings): two on-disk content sinks were missed by
the original cluster-1 persistence hardening — `context.save_session_summary`
(session_summaries in usage.db) and `idempotency.IdempotencyStore`
(~/.llm-router/idempotency.db). Both persisted LLM/session content 100% raw at 0644.

These tests reproduce the audit's own probe: write a canary secret through each
sink's real public path, then scan the RAW BYTES on disk (never an API accessor)
and check file perms — plus a class-completeness guard so a NEW sink can't slip
through un-hardened.
"""
import os
import sqlite3
import stat

import pytest

CANARY = "sk-CANARY-1234567890ABCDEF"
EMAIL = "victim@example.com"


def _reset_config():
    import llm_router.config as cfg
    cfg._config = None


@pytest.mark.asyncio
async def test_session_summary_redacts_secret_and_is_0600(tmp_path, monkeypatch):
    db = tmp_path / "usage.db"
    monkeypatch.delenv("LLM_ROUTER_PERSIST_RAW", raising=False)
    monkeypatch.setenv("LLM_ROUTER_PERSIST_REDACTION", "on")
    _reset_config()
    from llm_router import context
    monkeypatch.setattr(context, "_get_db_path", lambda: db)
    await context.save_session_summary(
        summary=f"User asked about API key {CANARY} and account {EMAIL}",
        message_count=3, task_types=["query"], project_id="p", session_id="s",
    )
    raw = db.read_bytes()
    assert CANARY.encode() not in raw, "session summary persisted a raw secret"
    assert EMAIL.encode() not in raw, "session summary persisted a raw email"
    assert stat.S_IMODE(os.stat(db).st_mode) == 0o600, "usage.db not 0600"
    _reset_config()


@pytest.mark.asyncio
async def test_session_summary_ttl_physically_deletes(tmp_path, monkeypatch):
    db = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_ROUTER_PERSIST_TTL_DAYS", "1")
    monkeypatch.delenv("LLM_ROUTER_PERSIST_RAW", raising=False)
    _reset_config()
    from llm_router import context
    monkeypatch.setattr(context, "_get_db_path", lambda: db)
    context._ensure_session_table(db)
    old = "2000-01-01T00:00:00+00:00"
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA secure_delete=ON")
    conn.execute(
        "INSERT INTO session_summaries "
        "(session_start, session_end, summary, message_count, task_types) "
        "VALUES (?,?,?,?,?)",
        (old, old, "OLD_MARKER_XYZ_9f3", 1, "[]"),
    )
    conn.commit()
    conn.close()
    # A fresh save triggers the TTL purge of rows older than the retention window.
    await context.save_session_summary(
        summary="fresh", message_count=1, task_types=[], project_id="p", session_id="s")
    raw = db.read_bytes()
    assert b"OLD_MARKER_XYZ_9f3" not in raw, "expired summary not physically purged"
    _reset_config()


def test_idempotency_redacts_by_default_and_is_0600(tmp_path, monkeypatch):
    db = tmp_path / "idem.db"
    monkeypatch.setenv("LLM_ROUTER_IDEMPOTENCY_PATH", str(db))
    monkeypatch.delenv("LLM_ROUTER_PERSIST_RAW", raising=False)
    monkeypatch.setenv("LLM_ROUTER_PERSIST_REDACTION", "on")
    _reset_config()
    from llm_router import idempotency
    from llm_router.types import LLMResponse
    idempotency.reset_store_for_tests()
    r = LLMResponse(content=f"the key is {CANARY}", model="m", provider="p",
                    input_tokens=1, output_tokens=1, cost_usd=0.0, latency_ms=1.0)
    idempotency.get_store().store("k1", r)
    raw = db.read_bytes()
    assert CANARY.encode() not in raw, "idempotency store persisted a raw secret"
    assert stat.S_IMODE(os.stat(db).st_mode) == 0o600, "idempotency.db not 0600"
    idempotency.reset_store_for_tests()
    _reset_config()


def test_idempotency_raw_optin_preserves_exact_replay(tmp_path, monkeypatch):
    """The escape hatch LLM_ROUTER_PERSIST_RAW=1 must keep byte-exact dedupe replay."""
    db = tmp_path / "idem2.db"
    monkeypatch.setenv("LLM_ROUTER_IDEMPOTENCY_PATH", str(db))
    monkeypatch.setenv("LLM_ROUTER_PERSIST_RAW", "1")
    _reset_config()
    from llm_router import idempotency
    from llm_router.types import LLMResponse
    idempotency.reset_store_for_tests()
    r = LLMResponse(content=f"the key is {CANARY}", model="m", provider="p",
                    input_tokens=1, output_tokens=1, cost_usd=0.0, latency_ms=1.0)
    st = idempotency.get_store()
    st.store("k2", r)
    got = st.lookup("k2")
    assert got is not None and CANARY in got.content, \
        "raw opt-in must preserve exact replay for callers that need it"
    idempotency.reset_store_for_tests()
    _reset_config()


def test_all_known_content_sinks_route_through_persist_redact():
    """Class-completeness guard: every module that persists LLM/session CONTENT
    to disk must call persist_redact. This is the lock that turns the D-01 class
    from 'fix the named files' into 'no content sink writes raw'. When a NEW
    persistence sink is added, add it here (and harden it) — a bare append here
    without the persist_redact call will fail this test."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "src" / "llm_router"
    content_sinks = [
        "result_cache.py", "semantic_cache.py", "session_store.py",
        "context.py", "idempotency.py",
    ]
    for s in content_sinks:
        src = (root / s).read_text()
        assert "persist_redact" in src, \
            f"{s} persists content but does not route it through persist_redact"


def test_execution_ledger_creates_usage_db_0600(tmp_path, monkeypatch):
    """CHZ-AUD-D-02 (RED-2): execution_ledger must secure usage.db to 0600 even
    when it is the first/only writer of the shared file."""
    db = tmp_path / "usage.db"
    monkeypatch.setenv("LLM_ROUTER_EXECUTION_LEDGER_DB", str(db))
    from llm_router.execution_ledger import LedgerEvent, record_event
    record_event(LedgerEvent(session_id="s", route_id="r", event_type="route_completed",
                             terminal_state="accepted"))
    assert db.exists()
    assert stat.S_IMODE(os.stat(db).st_mode) == 0o600, "execution_ledger left usage.db world-readable"
