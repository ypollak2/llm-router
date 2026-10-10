"""LEDGER-ERR-1 (owner decision 2026-10-10): error and cache-served calls have ``routing_decisions``
rows (so the M0-3 gate, which reads that table, sees every call). They are not routing decisions:
every routing-metric reader leaves them out (``provider_classes.SQL_REAL_DECISION``).

The table holds 6 real rows for model R, plus 4 error rows and 3 cache rows that would each move
a count, a failure rate, a latency percentile, a share or a cost if they leaked in.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from llm_router.provider_classes import (SQL_REAL_DECISION, is_non_decision_reason, real_decision_sql)
from tests.test_p05_semantic_cache_key import cache_env as _p05_cache_env

cache_env = _p05_cache_env


async def _fill(db: Path) -> None:
    from llm_router import cost

    d = await cost._get_db()  # creates the schema
    await d.close()
    c = sqlite3.connect(str(db))
    cols = ("prompt_hash, task_type, profile, classifier_type, complexity, recommended_model, base_model, "
            "final_model, final_provider, success, input_tokens, output_tokens, cost_usd, latency_ms, "
            "reason_code, provenance, was_good")
    ins = f"INSERT INTO routing_decisions ({cols}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
    real = ("h", "code", "budget", "heuristic", "simple", "ollama/R", "ollama/R", "ollama/R", "ollama", 1, 10, 5,
            0.5, 1000.0, "router_chain", "runtime", 1)
    for i in range(6):
        c.execute(ins, real)
    for reason in ("error_all_models_failed", "error_timeout", "error_cancelled", "error_all_models_failed"):
        c.execute(ins, ("", "code", "budget", "unhinted", "simple", "ollama/R", "ollama/R", "ollama/R", "ollama",
                        0, 0, 0, 0.0, 244000.0, reason, "runtime", 0))
    for _ in range(3):
        c.execute(ins, ("h", "code", "budget", "unhinted", "simple", "cache/ollama/R", "cache/ollama/R",
                        "cache/ollama/R", "cache", 1, 0, 0, 0.0, 0.0, "cache_hit", "runtime", 1))
    c.commit()
    c.close()


def test_predicate_helpers(tmp_path):
    assert is_non_decision_reason("cache_hit") and is_non_decision_reason("error_timeout")
    assert not is_non_decision_reason("router_chain") and not is_non_decision_reason(None)
    c = sqlite3.connect(":memory:")
    assert real_decision_sql(c) == "1"  # no routing_decisions table: nothing to exclude
    c.execute("CREATE TABLE routing_decisions (reason_code TEXT, final_provider TEXT)")
    assert real_decision_sql(c) == SQL_REAL_DECISION
    assert "r.reason_code" in real_decision_sql(c, "r")


@pytest.mark.asyncio
async def test_cost_readers_exclude_error_and_cache_rows(cache_env):
    from llm_router import cost

    await _fill(cache_env)
    rep = await cost.get_quality_report(days=1)
    assert rep["total_decisions"] == 6 and rep["by_model"] == {"ollama/R": {
        "count": 6, "avg_latency": 1000.0, "total_cost": 3.0}}
    eff = await cost.get_router_efficiency("all")
    assert eff["total"] == 6
    assert (await cost.get_classifier_overhead("all"))["count"] == 6
    assert (await cost.get_routing_savings_vs_sonnet())["total_calls"] == 6
    assert await cost.get_model_failure_rates() == {"ollama/R": 0.0}   # 4 error rows would read 40%
    assert await cost.get_model_acceptance_scores() == {"ollama/R": 1.0}
    lat = await cost.get_model_latency_stats()
    assert lat["ollama/R"]["count"] == 6 and lat["ollama/R"]["p95"] == 1000.0


@pytest.mark.asyncio
async def test_other_readers_exclude_error_and_cache_rows(cache_env, monkeypatch):
    await _fill(cache_env)
    import datetime as dt

    from llm_router import community, retrospective, test_delta

    bench = await community.get_benchmark_stats()
    assert bench["code"]["total"] == 6
    conn = sqlite3.connect(str(cache_env))
    assert test_delta._read_routing_table(conn).rows == 6
    monkeypatch.setattr(retrospective, "_db_path", lambda: cache_env)
    now = dt.datetime.now(dt.timezone.utc)
    got = retrospective.fetch_session_decisions(now - dt.timedelta(days=1), now + dt.timedelta(days=1))
    assert len(got) == 6


def test_session_end_hook_predicate_equals_the_shared_one():
    root = Path(__file__).resolve().parent.parent
    for rel in ("hooks/session-end.py", "src/llm_router/hooks/session-end.py"):
        text = (root / rel).read_text()
        ns: dict = {}
        line = next(i for i, ln in enumerate(text.splitlines()) if ln.startswith("_REAL_DECISION = "))
        block = "\n".join(text.splitlines()[line:line + 2])
        exec(block, ns)
        assert ns["_REAL_DECISION"] == SQL_REAL_DECISION, rel
