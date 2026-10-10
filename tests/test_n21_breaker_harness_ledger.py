"""N21: research/harness sessions must not trip the live quality breaker, and a breaker refusal in
``llm()`` leaves exactly one session-attributed row in ``usage`` and ``routing_decisions``.

Incident (M0-3 rerun2, 2026-10-10 14:25-14:35Z): two research-tagged headless sessions called
``llm(task="code")`` 20 times and discarded each answer; the breaker read 20 failures, opened
``mcp_llm:code`` for 24 h, and the next two calls (A10, B10) were refused with no ledger row.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from llm_router import call_identity
from llm_router import quality_breaker as qb
from llm_router.provider_classes import (SQL_NOT_ERROR_ROW, SQL_REAL_DECISION, is_non_decision_reason,
                                         not_error_row_sql, real_decision_sql)
from tests.test_p05_semantic_cache_key import cache_env as _p05_cache_env

cache_env = _p05_cache_env
SID = "22222222-aaaa-bbbb-cccc-333333333333"


def _unit(outcome, i, kind):
    return {"session_id": "s", "ts": datetime.fromtimestamp(1000.0 + i, tz=timezone.utc).isoformat(),
            "kind": "routed_mcp", "lever": "mcp_llm", "task_type": "code", "model": "ollama/q",
            "outcome": outcome, "signal": "t", "session_kind": kind}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_HOME", str(tmp_path))
    return tmp_path


# ── 1. non-organic units never feed the breaker ─────────────────────────────

@pytest.mark.parametrize("kind", ["research", "harness"])
def test_discarded_units_of_a_non_organic_session_do_not_open_the_class(home, kind):
    units = [_unit("discarded", i, kind) for i in range(20)]
    d = qb.should_route("mcp_llm", "code", units_fn=lambda **_: units)
    assert d.allowed is True and d.state == qb.CLOSED and d.n == 0


@pytest.mark.parametrize("kind", ["organic", "headless", None])
def test_the_same_units_from_a_counted_session_still_open_the_class(home, kind):
    units = [_unit("discarded", i, kind) for i in range(20)]
    d = qb.should_route("mcp_llm", "code", units_fn=lambda **_: units)
    assert d.allowed is False and d.state == qb.OPEN and d.n == 20


def test_research_units_do_not_dilute_or_trip_an_organic_class(home):
    units = ([_unit("used", i, "organic") for i in range(20)]
             + [_unit("discarded", 100 + i, "research") for i in range(40)])
    d = qb.should_route("mcp_llm", "code", units_fn=lambda **_: units)
    assert d.allowed is True and d.n == 20 and d.failure_rate == 0.0
    assert all(r["would_be"] == "closed" for r in qb.dry_run(units_fn=lambda **_: units))


# ── 2. a refusal writes one row in each table ───────────────────────────────

class _Ctx:
    async def info(self, *a, **k):
        pass

    async def report_progress(self, *a, **k):
        pass


def _q(db, sql):
    c = sqlite3.connect(str(db))
    try:
        return c.execute(sql).fetchall()
    finally:
        c.close()


@pytest.mark.asyncio
async def test_breaker_refusal_writes_one_row_in_usage_and_routing_decisions(cache_env, monkeypatch):
    import json
    import time

    from llm_router import cost
    from llm_router.tools import consolidated

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
    (cache_env.parent / qb.STATE_FILE).write_text(json.dumps({"classes": {
        qb.class_key("mcp_llm", "code"): {"state": qb.OPEN, "opened_at": time.time(), "n": 20,
                                          "failure_rate": 1.0}}}))

    async def _boom(*a, **k):
        raise AssertionError("must not dispatch")
    monkeypatch.setattr(consolidated, "llm_code", _boom)

    tok = call_identity.bind("toolu_n21")
    try:
        out = await consolidated.llm("p", _Ctx(), task="code")
    finally:
        call_identity.reset(tok)
    assert out.startswith("[llm_router] quality_breaker:")

    u = _q(cache_env, "SELECT session_id, task_type, reason, success, input_tokens, output_tokens, cost_usd "
                      "FROM usage")
    r = _q(cache_env, "SELECT session_id, task_type, reason_code, provenance, success, cost_usd, correlation_id "
                      "FROM routing_decisions")
    assert u == [(SID, "code", "breaker_open", 0, 0, 0, 0.0)]
    assert len(r) == 1 and r[0][:6] == (SID, "code", "breaker_open", cost._write_provenance(), 0, 0.0) and r[0][6]
    # exactly one per call: a second refused call is a second pair, never a third row
    tok = call_identity.bind("toolu_n21b")
    try:
        await consolidated.llm("p", _Ctx(), task="code")
    finally:
        call_identity.reset(tok)
    assert len(_q(cache_env, "SELECT 1 FROM usage")) == 2
    assert len(_q(cache_env, "SELECT 1 FROM routing_decisions")) == 2


@pytest.mark.asyncio
async def test_a_ledger_failure_never_changes_the_refusal_reply(cache_env, monkeypatch):
    import json
    import time

    from llm_router import cost
    from llm_router.tools import consolidated

    (cache_env.parent / qb.STATE_FILE).write_text(json.dumps({"classes": {
        qb.class_key("mcp_llm", "code"): {"state": qb.OPEN, "opened_at": time.time(), "n": 20,
                                          "failure_rate": 1.0}}}))

    async def _fail(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(cost, "log_route_error", _fail)
    out = await consolidated.llm("p", _Ctx(), task="code")  # must not raise
    assert out.startswith("[llm_router] quality_breaker:")


# ── 3. consumers exclude the breaker rows ───────────────────────────────────

def test_predicates_exclude_breaker_open():
    assert is_non_decision_reason("breaker_open")
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE usage (reason TEXT)")
    c.execute("CREATE TABLE routing_decisions (reason_code TEXT, final_provider TEXT)")
    for x in ("router_chain", "breaker_open", "error_timeout"):
        c.execute("INSERT INTO usage VALUES (?)", (x,))
        c.execute("INSERT INTO routing_decisions VALUES (?, 'ollama')", (x,))
    assert c.execute(f"SELECT reason FROM usage WHERE {not_error_row_sql(c)}").fetchall() == [("router_chain",)]
    for pred in (SQL_REAL_DECISION, real_decision_sql(c)):
        assert c.execute(f"SELECT reason_code FROM routing_decisions WHERE {pred}").fetchall() == [("router_chain",)]
    assert SQL_NOT_ERROR_ROW  # shared constant is what the dashboard server uses


@pytest.mark.asyncio
async def test_cost_readers_and_routing_report_exclude_breaker_rows(cache_env):
    from llm_router import cost

    d = await cost._get_db()
    await d.close()
    c = sqlite3.connect(str(cache_env))
    cols = ("prompt_hash, task_type, profile, classifier_type, complexity, recommended_model, base_model, "
            "final_model, final_provider, success, input_tokens, output_tokens, cost_usd, latency_ms, "
            "reason_code, provenance, was_good")
    ins = f"INSERT INTO routing_decisions ({cols}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
    real = ("h", "code", "budget", "heuristic", "simple", "ollama/R", "ollama/R", "ollama/R", "ollama", 1, 10, 5,
            0.5, 1000.0, "router_chain", "runtime", 1)
    for _ in range(6):
        c.execute(ins, real)
    for _ in range(4):
        c.execute(ins, ("", "code", "budget", "unhinted", "simple", "none", "none", "none", "none",
                        0, 0, 0, 0.0, 0.0, "breaker_open", "runtime", 0))
    c.commit()
    c.close()
    rep = await cost.get_quality_report(days=1)
    assert rep["total_decisions"] == 6
    assert await cost.get_model_failure_rates() == {"ollama/R": 0.0}
    assert (await cost.get_router_efficiency("all"))["total"] == 6
