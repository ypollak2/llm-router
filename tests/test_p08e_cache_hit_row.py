"""P0.8-e: a call the semantic cache answers is attributable to its session.

Live M0-3 run (2026-10-09): two concurrent sessions made 10 ``llm(task="code")`` calls each;
18 wrote ``usage`` / ``routing_decisions`` rows with the right session_id, the other 2 were
semantic-cache hits and wrote no session-attributed row anywhere. Two causes:

* ``semantic_cache_lookups`` had no ``session_id`` column;
* the cache-hit branch wrote no ``usage`` row, and its ``routing_decisions`` row is rejected by
  ``_validate_routing_insert`` (provider ``cache`` is not a real provider), swallowed as a warning.

These tests run the real ``route_and_call`` against a real SQLite file (no provider call is made
for the hit; the miss path uses a stubbed provider).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from llm_router import call_identity
from llm_router import semantic_cache as sc
from tests.test_p05_semantic_cache_key import _route
from tests.test_p05_semantic_cache_key import cache_env as _p05_cache_env

cache_env = _p05_cache_env  # re-exported fixture (a bare import would trip F811 on every use)

SID = "11111111-aaaa-bbbb-cccc-222222222222"


@pytest.fixture
def caller(monkeypatch):
    """Inside an MCP tool call of session SID (what ``call_session_id`` requires)."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
    tok = call_identity.bind("toolu_test_1")
    yield
    call_identity.reset(tok)


@pytest.fixture
def real_usage_writer(monkeypatch):
    """The p05 harness stubs ``cost.log_usage``; this file needs the real one."""
    import tests.test_p05_semantic_cache_key as p05
    from llm_router import cost

    real = cost.log_usage
    orig_patch = p05.patch

    class _Patch:
        dict = staticmethod(orig_patch.dict)
        object = staticmethod(orig_patch.object)

        def __call__(self, target, *a, **kw):
            if target == "llm_router.router.cost.log_usage":
                return orig_patch.object(cost, "log_usage", real)
            return orig_patch(target, *a, **kw)

    monkeypatch.setattr(p05, "patch", _Patch())


def _q(db: Path, sql: str) -> list[tuple]:
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_cache_hit_writes_a_session_attributed_usage_row(cache_env, caller, real_usage_writer):
    calls: list = []
    first = await _route("explain the retry policy", caller_context="c", calls=calls)
    second = await _route("explain the retry policy", caller_context="c", calls=calls)
    assert len(calls) == 1 and second.cache_hit is True and first.cache_hit is False

    hit = _q(cache_env, "SELECT session_id, reason, provider, model, input_tokens, output_tokens, "
                        "cost_usd, saved_usd FROM usage WHERE reason = 'cache_hit'")
    assert hit == [(SID, "cache_hit", "cache", "cache/openai/gpt-4o", 0, 0, 0.0, 0.0)]

    # The miss wrote exactly its one ordinary row; nothing extra per lookup.
    rows = _q(cache_env, "SELECT reason, session_id FROM usage ORDER BY id")
    assert rows == [("router_chain", SID), ("cache_hit", SID)]

    # Per-lookup log: miss then hit, both carry the session.
    assert _q(cache_env, "SELECT hit, session_id FROM semantic_cache_lookups ORDER BY id") == \
        [(0, SID), (1, SID)]
    # No prompt text in either ledger.
    dump = repr(_q(cache_env, "SELECT * FROM usage")) + repr(_q(cache_env, "SELECT * FROM semantic_cache_lookups"))
    assert "retry policy" not in dump


@pytest.mark.asyncio
async def test_a_miss_writes_no_cache_hit_row(cache_env, caller, real_usage_writer):
    calls: list = []
    await _route("first question", caller_context="c", calls=calls)
    await _route("a different question", caller_context="c", calls=calls)
    assert len(calls) == 2
    assert _q(cache_env, "SELECT COUNT(*) FROM usage WHERE reason = 'cache_hit'") == [(0,)]
    assert _q(cache_env, "SELECT reason FROM usage ORDER BY id") == [("router_chain",), ("router_chain",)]


@pytest.mark.asyncio
async def test_cache_hit_adds_no_routing_decision_row(cache_env, caller, real_usage_writer):
    """Deliberate: a replay is not a routing decision (see the PR body); the judge queue, the
    bandit and the offload shares read ``routing_decisions`` and would count it twice."""
    calls: list = []
    await _route("explain the retry policy", caller_context="c", calls=calls)
    before = _q(cache_env, "SELECT COUNT(*) FROM routing_decisions")
    await _route("explain the retry policy", caller_context="c", calls=calls)
    assert _q(cache_env, "SELECT COUNT(*) FROM routing_decisions") == before
    assert _q(cache_env, "SELECT COUNT(*) FROM routing_decisions WHERE final_provider = 'cache'") == [(0,)]


@pytest.mark.asyncio
async def test_outside_an_mcp_call_the_session_is_null_not_guessed(cache_env, real_usage_writer, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)  # in the env, but no tool_use bound
    calls: list = []
    await _route("explain the retry policy", caller_context="c", calls=calls)
    await _route("explain the retry policy", caller_context="c", calls=calls)
    assert _q(cache_env, "SELECT session_id FROM usage WHERE reason = 'cache_hit'") == [(None,)]
    assert _q(cache_env, "SELECT session_id FROM semantic_cache_lookups ORDER BY id") == [(None,), (None,)]


def test_the_session_column_migration_is_additive_and_idempotent(tmp_path, monkeypatch):
    import asyncio

    import aiosqlite

    db = tmp_path / "old.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE semantic_cache_lookups (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                 "ts REAL NOT NULL, task_type TEXT NOT NULL, hit INTEGER NOT NULL, "
                 "saved_usd REAL NOT NULL DEFAULT 0)")
    conn.execute("INSERT INTO semantic_cache_lookups (ts, task_type, hit) VALUES (1.0, 'code', 1)")
    conn.commit()
    conn.close()

    async def run():
        async with aiosqlite.connect(str(db)) as adb:
            # semantic_cache itself must exist for the earlier migration steps.
            await adb.execute(sc.CREATE_SEMANTIC_CACHE_LOOKUPS_TABLE)
            await adb.execute("CREATE TABLE IF NOT EXISTS semantic_cache (id INTEGER PRIMARY KEY, "
                              "task_type TEXT, prompt_hash TEXT, embedding TEXT, response_content TEXT, "
                              "response_model TEXT, response_cost_usd REAL, created_at TEXT)")
            await sc._ensure_project_scope_column(adb)
            await sc._ensure_project_scope_column(adb)

    asyncio.run(run())
    cols = [r[1] for r in _q(db, "PRAGMA table_info(semantic_cache_lookups)")]
    assert cols.count("session_id") == 1
    assert _q(db, "SELECT hit, session_id FROM semantic_cache_lookups") == [(1, None)]


def test_the_cache_hit_writer_is_the_registered_usage_writer():
    """No new INSERT site: the row goes through ``cost.log_usage``, which P0.8-d's writer scan and
    AST check already require to name session_id and reason."""
    from tests.test_p08d_ledger_completeness import EXPECTED_WRITERS, _found

    assert ("src/llm_router/cost.py", "usage") in EXPECTED_WRITERS
    assert not {k for k in _found() if k[1] == "usage"} - set(EXPECTED_WRITERS)
