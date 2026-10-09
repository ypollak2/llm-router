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


# --- the "paid" predicate: a cache row is a call to nobody -------------------------------------

import importlib.util  # noqa: E402
import re  # noqa: E402
import time  # noqa: E402

from llm_router.provider_classes import CACHE_PROVIDER, is_cache_provider  # noqa: E402

_REPO = Path(__file__).resolve().parent.parent
_HOOK_DIRS = (_REPO / "hooks", _REPO / "src" / "llm_router" / "hooks")


def _usage_db(tmp_path: Path) -> Path:
    """1 paid (anthropic), 1 free (ollama), 1 cache row, all within the last minute."""
    db = tmp_path / "usage.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE usage (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT DEFAULT "
                 "CURRENT_TIMESTAMP, model TEXT, provider TEXT, task_type TEXT, input_tokens INT, "
                 "output_tokens INT, cost_usd REAL, latency_ms REAL, success INT DEFAULT 1, "
                 "is_simulated INT DEFAULT 0, cache_hit INT DEFAULT 0, cache_savings_usd REAL DEFAULT 0)")
    for model, prov, itok, otok, cost in (("anthropic/claude-x", "anthropic", 10, 5, 0.01),
                                          ("ollama/q", "ollama", 10, 5, 0.0),
                                          ("cache/anthropic/claude-x", "cache", 0, 0, 0.0)):
        conn.execute("INSERT INTO usage (model, provider, task_type, input_tokens, output_tokens, "
                     "cost_usd, latency_ms) VALUES (?,?,?,?,?,?,?)", (model, prov, "code", itok, otok, cost, 1.0))
    conn.commit()
    conn.close()
    return db


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_provider_classes_is_the_one_definition():
    assert CACHE_PROVIDER == "cache" and is_cache_provider("cache") and not is_cache_provider("ollama")
    from llm_router.semantic_cache import CACHE_PROVIDER as sc_provider
    assert sc_provider == CACHE_PROVIDER


@pytest.mark.parametrize("hook", ["status-bar.py", "session-end.py", "session-start.py"])
def test_hook_copies_name_the_cache_provider_and_stay_identical(hook):
    texts = [(d / hook).read_text() for d in _HOOK_DIRS]
    assert texts[0] == texts[1], f"{hook}: hooks/ and src/llm_router/hooks/ differ"
    assert re.search(rf'^_CACHE_PROVIDER\s*=\s*"{CACHE_PROVIDER}"', texts[0], re.M)


def test_the_clawcode_hook_names_the_cache_provider():
    t = (_HOOK_DIRS[1] / "session-end-clawcode.py").read_text()
    assert re.search(rf'^_CACHE_PROVIDER\s*=\s*"{CACHE_PROVIDER}"', t, re.M)


def test_status_bar_session_calls_exclude_cache(tmp_path, monkeypatch):
    db = _usage_db(tmp_path)
    mod = _load(_HOOK_DIRS[0] / "status-bar.py", "sb_p08e")
    start = tmp_path / "start.txt"
    start.write_text(str(time.time() - 3600))
    monkeypatch.setattr(mod, "_usage_db", lambda: str(db))
    monkeypatch.setattr(mod, "_session_start_file", lambda: str(start))
    assert mod._read_session_calls() == (0, 1, 1)  # (sub, free, paid): the cache row is in none


def test_session_end_paid_rows_exclude_cache(tmp_path, monkeypatch):
    db = _usage_db(tmp_path)
    mod = _load(_HOOK_DIRS[0] / "session-end.py", "se_p08e")
    monkeypatch.setattr(mod, "_db_path", lambda: str(db))
    paid, cc, free = mod._query_session_data(time.time() - 3600)
    assert [r["provider"] for r in paid] == ["anthropic"] and cc == []
    assert [r["provider"] for r in free] == ["ollama"]


def test_statusline_mix_excludes_cache(tmp_path):
    from llm_router.statusline_segments import mix_segment
    _usage_db(tmp_path)
    assert mix_segment(str(tmp_path)) == {"mix_local": "1", "mix_paid": "1"}


def test_share_card_and_digest_exclude_cache(tmp_path):
    import asyncio

    from llm_router.commands.share import _gather_stats
    stats = _gather_stats(str(_usage_db(tmp_path)))
    assert (stats.total_calls, stats.paid_calls, stats.free_calls) == (2, 1, 1)

    from llm_router import digest
    from llm_router.cost import _get_db  # noqa: F401  (digest reads through it)
    import llm_router.config as cfg_mod
    cfg_mod._config = None
    import os
    os.environ["LLM_ROUTER_DB_PATH"] = str(tmp_path / "usage.db")
    cfg_mod._config = None
    try:
        data = asyncio.run(digest._fetch_period_data("all time"))
    finally:
        os.environ.pop("LLM_ROUTER_DB_PATH", None)
        cfg_mod._config = None
    assert data["calls"] == 2 and "cache" not in data["by_provider"]


def test_routing_health_excludes_cache(tmp_path):
    import datetime as dt

    from llm_router.routing_health import routed_calls
    db = _usage_db(tmp_path)
    out = routed_calls(days=2, db=db, today=dt.date.today() + dt.timedelta(days=0))
    day = next(iter(out.values()))
    assert day["calls"] == 2 and day["local"] == 1 and day["claude"] == 1 and day["other"] == 0


def test_prompt_cache_hit_rate_denominator_excludes_semantic_cache_rows(tmp_path, monkeypatch):
    """usage.cache_hit is the provider prompt cache (nothing sets it on a semantic-cache row);
    cache rows must not dilute its denominator."""
    import asyncio

    import llm_router.config as cfg_mod
    from llm_router import cost
    db = _usage_db(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE usage SET cache_hit = 1, cache_savings_usd = 0.5 WHERE provider = 'anthropic'")
    conn.commit()
    conn.close()
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(db))
    cfg_mod._config = None
    try:
        r = asyncio.run(cost.get_cache_savings("all", include_simulated=True))
    finally:
        cfg_mod._config = None
    assert r["total_calls_cached"] == 1 and r["cache_hit_rate"] == 50.0  # 1 of 2 real calls, not 1 of 3
