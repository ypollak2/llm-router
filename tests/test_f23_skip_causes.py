"""F23: routing_decisions rows record what was skipped before the served provider, and why.

Evidence: post-deploy-24h 2026-10-17 sections 2-3. A Codex usage-limit bench made the router
serve local Ollama; the row said only ``router_unhinted``, so bench-driven local share could not
be separated from policy-driven local share. No network: dispatch is stubbed.
"""
from __future__ import annotations

import json
import sqlite3
import time
from unittest.mock import AsyncMock, patch

import pytest

from llm_router import skip_causes
from llm_router.types import LLMResponse, TaskType

OLLAMA = "ollama/qwen3:8b"
GEMINI = "gemini/gemini-2.5-flash"
CODEX = "codex/gpt-5"


@pytest.fixture
def env(mock_env, temp_db, tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LLM_ROUTER_PROVIDER_RESET_PATH", str(tmp_path / "provider_reset.json"))
    monkeypatch.setattr("llm_router.router.is_codex_available", lambda: False)
    monkeypatch.setenv("LLM_ROUTER_PROMOTE_GATE", "off")  # P0.15 would sink the unconfigured ollama id
    monkeypatch.setenv("LLM_ROUTER_BANDIT", "off")
    return temp_db


def _resp(model: str) -> LLMResponse:
    provider = model.split("/", 1)[0]
    return LLMResponse(content="an answer long enough to be usable", model=model, input_tokens=42,
                       output_tokens=12, cost_usd=0.0, latency_ms=10.0, provider=provider)


async def _route(chain: list[str], failing: tuple[str, ...] = (), task=TaskType.QUERY):
    from llm_router.router import route_and_call

    async def _build(*a, **k):
        return list(chain)

    async def _call_text(model, *a, **k):
        if model in failing:
            raise RuntimeError("boom")
        return _resp(model)

    with patch("llm_router.cost.get_daily_spend", new_callable=AsyncMock, return_value=0.0), \
         patch("llm_router.cost.get_monthly_spend", new_callable=AsyncMock, return_value=0.0), \
         patch("llm_router.router._build_and_filter_chain", _build), \
         patch("llm_router.router._call_text", _call_text):
        return await route_and_call(task, "In one sentence, what does `wc -l` do?")


def _row(db_path):
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute("SELECT * FROM routing_decisions").fetchall()
    finally:
        con.close()
    assert len(rows) == 1, len(rows)
    return rows[0]


@pytest.mark.asyncio
async def test_benched_codex_is_recorded_with_bench_reason_and_until(env):
    from llm_router import provider_reset

    until = time.time() + 3600
    assert provider_reset.record_provider_reset("codex", until, "usage limit")
    await _route([CODEX, OLLAMA])
    row = _row(env)
    assert row["final_provider"] == "ollama"
    skipped = json.loads(row["skipped_json"])
    assert [(e["model"], e["reason"]) for e in skipped] == [(CODEX, "bench")]
    assert skipped[0]["kind"] == "provider_reset"
    assert abs(skipped[0]["until"] - until) < 1.0
    assert row["served_by_last_resort"] == 1


@pytest.mark.asyncio
async def test_d116_last_resort_after_a_failed_non_local_provider(env):
    await _route([GEMINI, OLLAMA], failing=(GEMINI,))
    row = _row(env)
    assert row["final_provider"] == "ollama"
    assert json.loads(row["skipped_json"]) == []     # failed, not skipped
    assert row["served_by_last_resort"] == 1


@pytest.mark.asyncio
async def test_policy_first_local_has_empty_skipped_and_no_last_resort(env):
    # CODE is not a Q&A type, so D-116 does not move local behind gemini: local leads by policy.
    await _route([OLLAMA, GEMINI], task=TaskType.CODE)
    row = _row(env)
    assert row["final_provider"] == "ollama"
    assert json.loads(row["skipped_json"]) == []
    assert row["served_by_last_resort"] == 0


@pytest.mark.asyncio
async def test_only_local_chain_for_qa_is_policy_not_last_resort(env):
    await _route([OLLAMA])
    row = _row(env)
    assert json.loads(row["skipped_json"]) == []
    assert row["served_by_last_resort"] == 0


@pytest.mark.asyncio
async def test_non_local_served_is_never_last_resort(env):
    await _route([GEMINI, OLLAMA])
    row = _row(env)
    assert row["final_provider"] == "gemini"
    assert row["served_by_last_resort"] == 0


@pytest.mark.asyncio
async def test_breaker_is_not_labelled_bench(env):
    from llm_router.health import get_tracker

    t = get_tracker()
    for _ in range(10):
        t.record_failure("gemini")
    try:
        await _route([GEMINI, OLLAMA])
    finally:
        t._providers.pop("gemini", None)
    skipped = json.loads(_row(env)["skipped_json"])
    assert [(e["model"], e["reason"]) for e in skipped] == [(GEMINI, "breaker")]


@pytest.mark.asyncio
async def test_capability_filtered_local_model_is_recorded(env, monkeypatch):
    """A local model the P0.15 gate drops from a critical chain is a skip with its own reason."""
    from llm_router import router as r

    monkeypatch.setattr(r, "order_for_chain", lambda models, *a, **k: ([], []))
    cfg = r.get_config()
    monkeypatch.setattr(type(cfg), "all_ollama_models", lambda self: ["qwen3:8b"], raising=False)
    skip_causes.begin()
    await r._build_and_filter_chain(
        TaskType.QUERY, r.RoutingProfile.BALANCED, None, None, r.Complexity.SIMPLE, cfg)
    notes = skip_causes.entries_so_far()
    assert {"model": "qwen3:8b", "reason": "capability_filtered"} in notes


def test_no_begin_means_unknown_not_empty():
    # a fresh context never began a list: NULL (unknown), never [] ("nothing skipped")
    import contextvars

    def run():
        return skip_causes.entries_so_far(), skip_causes.to_json(skip_causes.entries_so_far())

    assert contextvars.Context().run(run) == (None, None)


# ---------------------------------------------------------------- migration and report


def _fixture_db(path, *, with_new_columns=True):
    from llm_router.cost import CREATE_ROUTING_DECISIONS_TABLE

    con = sqlite3.connect(path)
    con.execute(CREATE_ROUTING_DECISIONS_TABLE)
    for stmt in ("ALTER TABLE routing_decisions ADD COLUMN provenance TEXT",
                 "ALTER TABLE routing_decisions ADD COLUMN reason_code TEXT"):
        con.execute(stmt)
    if with_new_columns:
        from llm_router.cost import MIGRATE_ROUTING_DECISIONS_ADD_SKIP_CAUSES
        for stmt in MIGRATE_ROUTING_DECISIONS_ADD_SKIP_CAUSES:
            con.execute(stmt)
    return con


BENCH = '[{"kind":"provider_reset","model":"codex/gpt-5","reason":"bench","until":1.0}]'
OTHER = '[{"model":"gemini/x","reason":"budget"}]'


def test_report_splits_local_share_by_cause(tmp_path):
    from llm_router.routing_report import executed_routes, format_local_by_cause

    p = tmp_path / "usage.db"
    con = _fixture_db(p)
    rows = [  # provider, skipped_json, last_resort
        ("ollama", "[]", 0), ("ollama", "[]", 0), ("ollama", "[]", 0),         # policy x3
        ("ollama", BENCH, 1), ("ollama", BENCH, 0),                            # bench x2 (beats last_resort)
        ("ollama", "[]", 1),                                                   # last_resort
        ("ollama", OTHER, 0),                                                  # other_skip
        ("ollama", None, None),                                                # unknown
        ("codex", BENCH, 0),                                                   # not local: not counted
    ]
    for prov, sk, lr in rows:
        con.execute("INSERT INTO routing_decisions (timestamp, final_provider, provenance, skipped_json, "
                    "served_by_last_resort) VALUES (datetime('now'), ?, 'runtime', ?, ?)", (prov, sk, lr))
    con.execute("INSERT INTO routing_decisions (timestamp, final_provider, provenance, skipped_json) "
                "VALUES (datetime('now', '-3 days'), 'ollama', 'runtime', '[]')")  # outside window
    con.commit()
    con.close()
    res = executed_routes(24.0, db_path=p)
    assert res["by_provider"]["ollama"] == 8
    assert res["local_by_cause"] == {"policy": 3, "bench": 2, "last_resort": 1, "other_skip": 1, "unknown": 1}
    assert format_local_by_cause(res) == "local by cause: policy 3 bench 2 last_resort 1 other_skip 1 unknown 1"
    assert format_local_by_cause({"local_by_cause": {}}) == ""


def test_report_on_old_schema_reads_all_local_as_unknown(tmp_path):
    from llm_router.routing_report import executed_routes

    p = tmp_path / "usage.db"
    con = _fixture_db(p, with_new_columns=False)
    for _ in range(3):
        con.execute("INSERT INTO routing_decisions (timestamp, final_provider, provenance) "
                    "VALUES (datetime('now'), 'ollama', 'runtime')")
    con.commit()
    con.close()
    assert executed_routes(24.0, db_path=p)["local_by_cause"]["unknown"] == 3


@pytest.mark.asyncio
async def test_migration_adds_columns_and_old_rows_stay_null(tmp_path, monkeypatch):
    from llm_router import cost

    p = tmp_path / "usage.db"
    con = _fixture_db(p, with_new_columns=False)
    con.execute("INSERT INTO routing_decisions (timestamp, final_provider, provenance) "
                "VALUES (datetime('now'), 'ollama', 'runtime')")
    con.commit()
    con.close()
    monkeypatch.setenv("LLM_ROUTER_DB_PATH", str(p))
    import llm_router.config as config_module
    config_module._config = None
    db = await cost._get_db()
    try:
        cur = await db.execute("SELECT skipped_json, served_by_last_resort FROM routing_decisions")
        assert [tuple(r) for r in await cur.fetchall()] == [(None, None)]
    finally:
        await db.close()
