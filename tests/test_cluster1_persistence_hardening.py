"""Cluster 1 — sensitive-content lifecycle hardening (D-01/D-02/D-04/B-02/B-03).

Verifies, for EACH persistence path (result_cache, semantic_cache,
session_store):

  * D-01/D-04 — content is redacted BEFORE it touches disk. Synthetic
    secrets (OpenAI key, AWS key, GitHub token, JWT, password, email, and a
    prose-phrased secret) must never appear in the raw on-disk bytes of the
    SQLite `.db` file (including its FTS5 shadow tables, which live in the
    same file), the `-wal` sidecar, or the JSONL store — NOT just absent
    from an API-level read. Every assertion here reads the file directly
    off disk and greps the raw bytes.
  * D-02 — every sensitive DB/JSONL file is created 0600, and an existing
    file with unsafe (world/group-readable) permissions gets repaired to
    0600 the next time the owning store opens it.
  * B-02/B-03 — the global `LLM_ROUTER_PERSIST_TTL_DAYS` retention window
    PHYSICALLY deletes expired rows/lines (not just filters them out of
    reads), and the deletion survives a fresh connection/process instance.
  * Safe-failure — if the redactor raises, the raw secret must never be
    persisted (either a withheld placeholder or a fallback-scrubbed value
    is stored instead).
  * `LLM_ROUTER_PERSIST_RAW=1` is an explicit opt-in escape hatch that disables
    all of the above and retains content verbatim.

Do NOT weaken these assertions to API-only checks — the whole point of
this suite is to prove secrets are absent from the BYTES on disk.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import time
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from llm_router.types import LLMResponse, TaskType

# ── Synthetic secrets (never real credentials) ──────────────────────────────

SECRET_OPENAI = "sk-A1b2C3d4E5f6G7h8I9j0K1l2M3n4"          # openai_key pattern
SECRET_AWS = "AKIAIOSFODNN7EXAMPLE"                         # aws_access_key pattern
SECRET_GITHUB = "ghp_" + "x" * 36                           # github_token pattern
SECRET_JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ."
    "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
)
SECRET_PASSWORD = "Tr0ub4dor&3"
SECRET_EMAIL = "victim@example.com"
SECRET_PROSE_VALUE = "ORANGE-742"                            # "the launch code is ORANGE-742"

SECRET_BLOB = (
    "Here are some credentials for a support ticket, please remember them:\n"
    f"OpenAI key: {SECRET_OPENAI}\n"
    f"AWS key: {SECRET_AWS}\n"
    f"GitHub token: {SECRET_GITHUB}\n"
    f"JWT: {SECRET_JWT}\n"
    f"password: {SECRET_PASSWORD}\n"
    f"Contact me at {SECRET_EMAIL}\n"
    f"the launch code is {SECRET_PROSE_VALUE}\n"
)

ALL_SECRET_SUBSTRINGS = [
    SECRET_OPENAI,
    SECRET_AWS,
    SECRET_GITHUB,
    SECRET_JWT,
    SECRET_PASSWORD,
    SECRET_EMAIL,
    SECRET_PROSE_VALUE,
]


def _assert_all_secrets_absent(raw: bytes) -> None:
    for secret in ALL_SECRET_SUBSTRINGS:
        assert secret.encode() not in raw, f"secret leaked into on-disk bytes: {secret!r}"


def _make_embedding(dim: int = 768, val: float = 1.0) -> list[float]:
    import math
    v = [val] * dim
    mag = math.sqrt(sum(x * x for x in v))
    return [x / mag for x in v]


def _make_response(content: str) -> LLMResponse:
    return LLMResponse(
        content=content, model="openai/gpt-4o",
        input_tokens=10, output_tokens=5,
        cost_usd=0.001, latency_ms=100, provider="openai",
    )


@pytest.fixture(autouse=True)
def _reset_persistence_config(monkeypatch):
    """Baseline every test on defaults (redaction on, raw off, ttl=30d)."""
    import llm_router.config as cfg_mod
    monkeypatch.delenv("LLM_ROUTER_PERSIST_RAW", raising=False)
    monkeypatch.delenv("LLM_ROUTER_PERSIST_REDACTION", raising=False)
    monkeypatch.delenv("LLM_ROUTER_PERSIST_TTL_DAYS", raising=False)
    cfg_mod._config = None
    yield
    cfg_mod._config = None


# ═════════════════════════════════════════════════════════════════════════
# result_cache.py
# ═════════════════════════════════════════════════════════════════════════

def test_result_cache_redacts_secrets_on_write(tmp_path, monkeypatch):
    monkeypatch.setattr("llm_router.result_cache._ROUTER_DIR", tmp_path)
    from llm_router import result_cache as rc

    rc.store_result(
        user_prompt=f"please remember these for later: {SECRET_BLOB}",
        response=SECRET_BLOB,
        task_type="query",
        complexity="simple",
        model_used="test-model",
    )

    db_path = rc._get_db_path(None, "query")
    assert db_path.exists()

    # Raw whole-file bytes (covers the main table AND the FTS5 external-
    # content shadow tables — they live in the same single .db file).
    raw = db_path.read_bytes()
    _assert_all_secrets_absent(raw)

    # WAL sidecar, defense-in-depth against checkpoint timing.
    wal = db_path.with_name(db_path.name + "-wal")
    if wal.exists():
        _assert_all_secrets_absent(wal.read_bytes())

    # API-level SELECT against the main table.
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute("SELECT user_prompt, response FROM results").fetchall()
    assert len(rows) == 1
    for prompt, resp in rows:
        for secret in ALL_SECRET_SUBSTRINGS:
            assert secret not in prompt
            assert secret not in resp

    # Explicit FTS5 shadow-table check.
    fts_rows = conn.execute("SELECT user_prompt, response FROM results_fts").fetchall()
    conn.close()
    assert len(fts_rows) == 1
    for prompt, resp in fts_rows:
        for secret in ALL_SECRET_SUBSTRINGS:
            assert secret not in (prompt or "")
            assert secret not in (resp or "")


def test_result_cache_db_created_with_0600(tmp_path, monkeypatch):
    monkeypatch.setattr("llm_router.result_cache._ROUTER_DIR", tmp_path)
    from llm_router import result_cache as rc

    rc.store_result("prompt one", "a response that is plenty long enough", "query", "simple", "m")
    db_path = rc._get_db_path(None, "query")
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600


def test_result_cache_repairs_unsafe_perms_on_existing_file(tmp_path, monkeypatch):
    monkeypatch.setattr("llm_router.result_cache._ROUTER_DIR", tmp_path)
    from llm_router import result_cache as rc

    db_path = rc._get_db_path(None, "query")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.touch()
    os.chmod(db_path, 0o644)
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o644

    rc.store_result("prompt two", "another sufficiently long response body", "query", "simple", "m")
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600


def test_result_cache_ttl_physically_deletes_expired_rows(tmp_path, monkeypatch):
    monkeypatch.setattr("llm_router.result_cache._ROUTER_DIR", tmp_path)
    monkeypatch.setenv("LLM_ROUTER_PERSIST_TTL_DAYS", "30")
    import llm_router.config as cfg_mod
    cfg_mod._config = None
    from llm_router import result_cache as rc

    rc.store_result(
        "old prompt marker",
        "response body long enough OLDRESULTCACHEMARKER123",
        "query", "simple", "m",
    )
    db_path = rc._get_db_path(None, "query")
    assert b"OLDRESULTCACHEMARKER123" in db_path.read_bytes()

    # Backdate the row directly on disk to simulate age beyond the TTL.
    old_ts = time.time() - (31 * 86400)
    conn = sqlite3.connect(str(db_path))
    conn.execute("UPDATE results SET timestamp = ?", (old_ts,))
    conn.commit()
    conn.close()

    removed = rc.purge_expired(db_path)
    assert removed == 1

    raw = db_path.read_bytes()
    assert b"OLDRESULTCACHEMARKER123" not in raw

    # Survives a brand new connection (fresh "process").
    conn2 = sqlite3.connect(str(db_path))
    count = conn2.execute("SELECT COUNT(*) FROM results").fetchone()[0]
    conn2.close()
    assert count == 0


def test_result_cache_persist_raw_opt_in_retains_verbatim(tmp_path, monkeypatch):
    monkeypatch.setattr("llm_router.result_cache._ROUTER_DIR", tmp_path)
    monkeypatch.setenv("LLM_ROUTER_PERSIST_RAW", "1")
    import llm_router.config as cfg_mod
    cfg_mod._config = None
    from llm_router import result_cache as rc

    rc.store_result(
        "prompt with a secret",
        f"response containing {SECRET_AWS} verbatim with extra padding text",
        "query", "simple", "m",
    )
    db_path = rc._get_db_path(None, "query")
    raw = db_path.read_bytes()
    assert SECRET_AWS.encode() in raw


def test_result_cache_safe_failure_never_persists_raw_on_redaction_error(tmp_path, monkeypatch):
    monkeypatch.setattr("llm_router.result_cache._ROUTER_DIR", tmp_path)
    from llm_router import result_cache as rc

    def _boom(_text):
        raise RuntimeError("redactor exploded")

    monkeypatch.setattr("llm_router.enterprise.redaction.persist_redact", _boom)

    rc.store_result(
        "prompt marker UNSAFEFAILUREMARKER000",
        f"response containing {SECRET_AWS} and more padding text to pass length check",
        "query", "simple", "m",
    )
    db_path = rc._get_db_path(None, "query")
    raw = db_path.read_bytes()
    assert SECRET_AWS.encode() not in raw
    assert b"UNSAFEFAILUREMARKER000" not in raw
    assert b"REDACTION-FAILED" in raw


# ═════════════════════════════════════════════════════════════════════════
# semantic_cache.py
# ═════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_semantic_cache_redacts_secrets_on_write(monkeypatch, tmp_path):
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(db_path))
    import llm_router.config as cfg_mod
    cfg_mod._config = None

    emb = _make_embedding()
    with patch("llm_router.semantic_cache._get_embedding", return_value=emb):
        from llm_router.semantic_cache import store
        await store("prompt asking to remember secrets", TaskType.QUERY, _make_response(SECRET_BLOB))

    assert db_path.exists()
    raw = db_path.read_bytes()
    _assert_all_secrets_absent(raw)

    conn = sqlite3.connect(str(db_path))
    rows = conn.execute("SELECT response_content FROM semantic_cache").fetchall()
    conn.close()
    assert len(rows) == 1
    for (content,) in rows:
        for secret in ALL_SECRET_SUBSTRINGS:
            assert secret not in content


@pytest.mark.asyncio
async def test_semantic_cache_repairs_unsafe_perms_on_existing_file(monkeypatch, tmp_path):
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
    db_path = tmp_path / "shared.db"
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(db_path))
    import llm_router.config as cfg_mod
    cfg_mod._config = None

    emb = _make_embedding()
    with patch("llm_router.semantic_cache._get_embedding", return_value=emb):
        from llm_router.semantic_cache import store
        # First call creates the shared db file.
        await store("prompt one", TaskType.QUERY, _make_response("first"))
    assert db_path.exists()

    # Simulate an unsafe pre-existing file (e.g. created by an older
    # version, or another process that didn't apply 0600).
    os.chmod(db_path, 0o644)
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o644

    with patch("llm_router.semantic_cache._get_embedding", return_value=emb):
        from llm_router.semantic_cache import store
        # Next open must repair perms before touching the shared db.
        await store("prompt two", TaskType.QUERY, _make_response("second"))
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_semantic_cache_ttl_physically_deletes_expired_rows(monkeypatch, tmp_path):
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(db_path))
    monkeypatch.setenv("LLM_ROUTER_PERSIST_TTL_DAYS", "30")
    import llm_router.config as cfg_mod
    cfg_mod._config = None

    emb = _make_embedding()
    with patch("llm_router.semantic_cache._get_embedding", return_value=emb):
        from llm_router.semantic_cache import store
        await store("old prompt", TaskType.QUERY, _make_response("OLDSEMANTICMARKER456"))

    assert b"OLDSEMANTICMARKER456" in db_path.read_bytes()

    # Backdate the row directly on disk beyond the TTL.
    old_ts = (datetime.utcnow() - timedelta(days=31)).strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(str(db_path))
    conn.execute("UPDATE semantic_cache SET created_at = ?", (old_ts,))
    conn.commit()
    conn.close()

    # store() purges TTL-expired rows on every call.
    with patch("llm_router.semantic_cache._get_embedding", return_value=emb):
        from llm_router.semantic_cache import store
        await store("trigger prompt", TaskType.QUERY, _make_response("trigger response"))

    raw = db_path.read_bytes()
    assert b"OLDSEMANTICMARKER456" not in raw

    # Survives a fresh connection.
    conn2 = sqlite3.connect(str(db_path))
    rows = conn2.execute("SELECT response_content FROM semantic_cache").fetchall()
    conn2.close()
    assert all("OLDSEMANTICMARKER456" not in r[0] for r in rows)


@pytest.mark.asyncio
async def test_semantic_cache_persist_raw_opt_in_retains_verbatim(monkeypatch, tmp_path):
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(db_path))
    monkeypatch.setenv("LLM_ROUTER_PERSIST_RAW", "1")
    import llm_router.config as cfg_mod
    cfg_mod._config = None

    emb = _make_embedding()
    with patch("llm_router.semantic_cache._get_embedding", return_value=emb):
        from llm_router.semantic_cache import store
        await store("prompt", TaskType.QUERY, _make_response(f"verbatim {SECRET_AWS} padding text"))

    raw = db_path.read_bytes()
    assert SECRET_AWS.encode() in raw


@pytest.mark.asyncio
async def test_semantic_cache_safe_failure_never_persists_raw_on_redaction_error(monkeypatch, tmp_path):
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(db_path))
    import llm_router.config as cfg_mod
    cfg_mod._config = None

    def _boom(_text):
        raise RuntimeError("redactor exploded")

    monkeypatch.setattr("llm_router.enterprise.redaction.persist_redact", _boom)

    emb = _make_embedding()
    with patch("llm_router.semantic_cache._get_embedding", return_value=emb):
        from llm_router.semantic_cache import store
        await store("prompt", TaskType.QUERY, _make_response(f"secret {SECRET_AWS} content padding"))

    raw = db_path.read_bytes()
    assert SECRET_AWS.encode() not in raw
    assert b"REDACTION-FAILED" in raw


# ═════════════════════════════════════════════════════════════════════════
# session_store.py
# ═════════════════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("LLM_ROUTER_SESSION_CONTEXT", raising=False)
    yield tmp_path


def test_session_store_redacts_secrets_on_write():
    from llm_router import session_store as ss

    ss.record_event("s1", "assistant", SECRET_BLOB)
    path = ss._session_path("s1")
    raw = path.read_bytes()
    _assert_all_secrets_absent(raw)

    events = ss.load_events("s1")
    assert len(events) == 1
    for e in events:
        for secret in ALL_SECRET_SUBSTRINGS:
            assert secret not in e["content"]


def test_session_store_file_created_with_0600():
    from llm_router import session_store as ss

    ss.record_event("s1", "user_prompt", "hello world, this is fine content")
    path = ss._session_path("s1")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_session_store_repairs_unsafe_perms_on_existing_file():
    from llm_router import session_store as ss

    ss.record_event("s1", "user_prompt", "first line of content here")
    path = ss._session_path("s1")
    os.chmod(path, 0o644)
    assert stat.S_IMODE(path.stat().st_mode) == 0o644

    ss.record_event("s1", "user_prompt", "second distinct line of content")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_session_store_ttl_physically_deletes_expired_records(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_PERSIST_TTL_DAYS", "30")
    import llm_router.config as cfg_mod
    cfg_mod._config = None
    from llm_router import session_store as ss

    ss.record_event("s1", "user_prompt", "old marker OLDSESSIONMARKER789")
    path = ss._session_path("s1")
    assert b"OLDSESSIONMARKER789" in path.read_bytes()

    # Backdate the record directly in the JSONL.
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1
    rec = json.loads(lines[0])
    rec["ts"] = time.time() - (31 * 86400)
    path.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)

    ss.purge_expired("s1")

    raw = path.read_bytes()
    assert b"OLDSESSIONMARKER789" not in raw

    # Survives a fresh read from disk (fresh "process").
    events = ss.load_events("s1")
    assert all("OLDSESSIONMARKER789" not in e["content"] for e in events)


def test_session_store_persist_raw_opt_in_retains_verbatim(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_PERSIST_RAW", "1")
    import llm_router.config as cfg_mod
    cfg_mod._config = None
    from llm_router import session_store as ss

    ss.record_event("s1", "user_prompt", f"verbatim retention check {SECRET_AWS} padding text")
    path = ss._session_path("s1")
    raw = path.read_bytes()
    assert SECRET_AWS.encode() in raw


def test_session_store_safe_failure_never_persists_raw_on_redaction_error(monkeypatch):
    from llm_router import session_store as ss

    def _boom(_text):
        raise RuntimeError("redactor exploded")

    monkeypatch.setattr("llm_router.enterprise.redaction.persist_redact", _boom)

    ss.record_event("s1", "user_prompt", f"secret leak check {SECRET_AWS} more padding text")
    path = ss._session_path("s1")
    raw = path.read_bytes()
    assert SECRET_AWS.encode() not in raw


class TestPrivateFilesAreCreatedRestricted:
    """Files holding secrets must be 0600 AT CREATION, not created-then-tightened.

    The established idiom here was::

        with open(path, "a") as fh:
            fh.write(secret)
        os.chmod(path, 0o600)

    correct at rest, wrong in between. `open` uses the umask default (0644
    typically), so on first creation the file held its contents world-readable
    for the duration of the write. Permissions are checked at open time, so a
    handle obtained inside that window keeps working after the chmod.

    Narrow — it needs local access and only affects first creation — but it
    applied to the dashboard AUTH TOKEN and to prompt-transcript shards, and the
    fix is a keyword argument.

    The chmod is deliberately kept alongside the opener: an opener only sets the
    mode when it CREATES the file, so it cannot repair files written by earlier
    versions.
    """

    def test_private_opener_creates_at_0600(self, tmp_path):
        from llm_router.paths import private_opener

        target = tmp_path / "secret.txt"
        with open(target, "w", encoding="utf-8", opener=private_opener) as fh:
            during = stat.S_IMODE(os.stat(target).st_mode)
            fh.write("sk-live-secret")
        assert during == 0o600, f"world-readable while writing: {oct(during)}"
        assert stat.S_IMODE(os.stat(target).st_mode) == 0o600

    def test_plain_open_is_the_thing_being_avoided(self, tmp_path):
        """Control: without the opener the file really is 0644 mid-write.

        If this ever stops holding, the test above proves nothing and the
        umask assumption behind this whole class needs revisiting.
        """
        prev = os.umask(0o022)
        try:
            target = tmp_path / "plain.txt"
            with open(target, "w", encoding="utf-8") as fh:
                during = stat.S_IMODE(os.stat(target).st_mode)
                fh.write("x")
            assert during == 0o644
        finally:
            os.umask(prev)
