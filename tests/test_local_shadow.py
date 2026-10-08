"""P2 local-usage plan: local_shadow units and the shadow-only "local" tier.

Rules under test: local_shadow rows are informational (never NS/D1/D2, kpi output
unchanged but for its own line); LLM_ROUTER_LOCAL_TIER defaults off, and in shadow
records the would-be tier without changing the served route.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from llm_router import local_tier, northstar as ns, session_kind
from llm_router.commands import kpi
from llm_router.types import ClassificationResult, Complexity, TaskType


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    d = tmp_path / "claude-projects"
    d.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(d))
    monkeypatch.delenv("LLM_ROUTER_KPI_BENCHMARK_PATH", raising=False)
    monkeypatch.delenv("LLM_ROUTER_LOCAL_TIER", raising=False)
    session_kind._FOUND.clear()
    session_kind.tag_session("s-org", "/Users/someone/Projects/app", env={})
    yield
    session_kind._FOUND.clear()


def _fixture_units(monkeypatch):
    attempted = sorted(ns.ATTEMPTED_KINDS)[0]
    now = time.time()
    rows = ([{"session_id": "s-org", "kind": attempted, "outcome": ns.OUTCOME_USED, "lever": None, "ts": now}] * 20
            + [{"session_id": "s-org", "kind": "claude_only", "outcome": ns.OUTCOME_NOT_ROUTED, "lever": None,
                "ts": now}] * 60)
    monkeypatch.setattr(ns, "units", lambda days=30, session_id=None, root=None, backfill=False: iter(rows))


def _make_db(path, rows):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE routing_decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, "
        "task_type TEXT, complexity TEXT, final_model TEXT, final_provider TEXT, latency_ms REAL, "
        "provenance TEXT, session_id TEXT, shadow_tier TEXT)")
    conn.executemany(
        "INSERT INTO routing_decisions (timestamp, task_type, complexity, final_model, final_provider, "
        "latency_ms, provenance, session_id, shadow_tier) VALUES (?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


def _now_ts():
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


ROWS = [
    (_now_ts(), "query", "simple", "ollama/lfm2.5:8b", "ollama", 10000.0, "runtime", "s-org", None),
    (_now_ts(), "query", "simple", "ollama/lfm2.5:8b", "ollama", 11000.0, "runtime", "s-org", "local"),
    (_now_ts(), "code", "moderate", "ollama/qwen3-coder:30b", "ollama", 14000.0, "runtime", "s-org", None),
    (_now_ts(), "code", "moderate", "codex/gpt", "codex", 9000.0, "runtime", "s-org", None),      # not ollama
    (_now_ts(), "query", "simple", "ollama/x", "ollama", 5.0, "synthetic", "s-org", None),         # not runtime
    ("2020-01-01 00:00:00", "query", "simple", "ollama/x", "ollama", 5.0, "runtime", "s-old", None),  # outside window
]


def test_local_shadow_units_shape_and_filter(tmp_path):
    db = tmp_path / "usage.db"
    _make_db(db, ROWS)
    out = list(ns.local_shadow_units(days=30, db_path=db))
    assert len(out) == 3
    assert {u["task_type"] for u in out} == {"query", "code"}
    for u in out:
        assert u["kind"] == ns.UNIT_LOCAL_SHADOW and u["lever"] == "local_shadow"
        assert u["provenance"] == "runtime" and u["outcome"] == ns.OUTCOME_UNKNOWN
        assert u["model"] and u["latency_ms"] and u["complexity"]
    assert [u["shadow_tier"] for u in out] == [None, "local", None]
    assert ns.UNIT_LOCAL_SHADOW not in ns.ALL_KINDS
    assert ns.UNIT_LOCAL_SHADOW not in ns.ATTEMPTED_KINDS


def test_local_shadow_missing_db_is_empty(tmp_path):
    assert list(ns.local_shadow_units(db_path=tmp_path / "nope.db")) == []


def test_kpi_byte_identical_with_and_without_local_shadow_rows(monkeypatch, tmp_path):
    _fixture_units(monkeypatch)
    home = tmp_path / "lrhome"
    home.mkdir()
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home))
    session_kind._FOUND.clear()
    session_kind.tag_session("s-org", "/Users/someone/Projects/app", env={})

    without = kpi.compute_scorecard(days=7, now=1_800_000_000.0)
    text_without = kpi.render_scorecard(without)
    assert without["local_shadow"]["n"] == 0
    assert without["kpis"]["NS"]["value"] == "0.0% (n=80)"   # fixture is measurable (strict-used, M0.2)
    assert without["kpis_diag"]["NS_heuristic"]["value"] == "25.0% (n=80)"

    _make_db(home / "usage.db", ROWS)
    with_rows = kpi.compute_scorecard(days=7, now=1_800_000_000.0)
    text_with = kpi.render_scorecard(with_rows)
    assert with_rows["local_shadow"] == {"n": 3, "by_task_type": {"code": 1, "query": 2}}

    # G3's per-writer verdict (P0.8-c) reads usage.db's routing_decisions as a writer by
    # definition (R-EVL-1). These rows sit outside the scorecard's fixed-now window, so its n
    # stays 0; what moves is the schema it now finds (this fixture table lacks reason_code,
    # cost_usd, ...: named "no_column"). Set that block aside; everything else is checked.
    for card in (with_rows, without):
        assert card["kpis"]["G3"]["prd"]["writers"]["routing_decisions"]["n"] == 0

    def kpis(card):
        out = dict(card["kpis"])
        out["G3"] = {k: v for k, v in out["G3"].items() if k != "prd"}
        return out

    # NS/D1/D2 and every other KPI: byte-identical.
    assert json.dumps(kpis(with_rows), sort_keys=True) == json.dumps(kpis(without), sort_keys=True)
    assert json.dumps(with_rows["joins"], sort_keys=True) == json.dumps(without["joins"], sort_keys=True)
    # Rendered scorecard: identical except for the one local (shadow) line.
    extra = [ln for ln in text_with.splitlines() if ln.startswith("local (shadow)")]
    assert len(extra) == 1 and "n=3" in extra[0] and "query 2" in extra[0] and "code 1" in extra[0]
    # P0.14-a: the proxy ledger liveness line reads usage.db (unreadable before it exists), so it differs by design.
    live = ("proxy_rows_24h:", "WARN proxy ledger")
    assert "\n".join(ln for ln in text_with.splitlines() if not ln.startswith(("local (shadow)",) + live)) == \
        "\n".join(ln for ln in text_without.splitlines() if not ln.startswith(live))
    assert "local (shadow)" not in text_without


def test_units_never_yields_local_shadow(monkeypatch, tmp_path):
    home = tmp_path / "lrhome"
    home.mkdir()
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home))
    _make_db(home / "usage.db", ROWS)
    assert all(u["kind"] != ns.UNIT_LOCAL_SHADOW for u in ns.units(days=30))
    assert ns.report(days=30)["by_kind"].keys() == set(ns.ALL_KINDS)


# ── LLM_ROUTER_LOCAL_TIER ────────────────────────────────────────────────────

def test_flag_default_off(monkeypatch):
    assert local_tier.mode() == "off"
    assert local_tier.would_be_tier("query", "simple") is None
    for bad in ("on", "1", "true", "SHADOWS", ""):
        monkeypatch.setenv("LLM_ROUTER_LOCAL_TIER", bad)
        assert local_tier.mode() == "off" and local_tier.would_be_tier("query", "simple") is None


@pytest.mark.parametrize("task,cx,want", [
    ("query", "simple", "local"), ("generate", "simple", "local"), ("summary", "simple", "local"),
    ("classification", "simple", "local"), ("extraction", "simple", "local"),
    ("query", "moderate", None), ("query", "complex", None), ("code", "simple", None),
    ("analyze", "simple", None), ("research", "simple", None),
])
def test_shadow_eligibility(monkeypatch, task, cx, want):
    monkeypatch.setenv("LLM_ROUTER_LOCAL_TIER", "shadow")
    assert local_tier.would_be_tier(task, cx) == want
    assert local_tier.would_be_tier(TaskType(task) if task in {t.value for t in TaskType} else task,
                                    Complexity(cx)) == want


def _result(task, cx):
    return ClassificationResult(complexity=Complexity(cx), confidence=0.9, reasoning="",
                                inferred_task_type=TaskType(task), classifier_model="m",
                                classifier_cost_usd=0.0, classifier_latency_ms=1.0)


def test_classification_result_shadow_tier_does_not_touch_fields(monkeypatch):
    r_off = _result("query", "simple")
    snap = (r_off.complexity, r_off.inferred_task_type, r_off.confidence)
    assert r_off.shadow_tier is None
    monkeypatch.setenv("LLM_ROUTER_LOCAL_TIER", "shadow")
    assert r_off.shadow_tier == "local"                      # property: cached results do not freeze it
    assert (r_off.complexity, r_off.inferred_task_type, r_off.confidence) == snap


async def _drive(flag, tmp_path):
    """Drive router.route_and_call once; return (models tried, served model, log kwargs)."""
    import tests.test_tq007_daily_cap_downgrade as t
    from llm_router import router
    from llm_router.types import LLMResponse, RoutingProfile, TaskType

    calls, logged = [], {}

    async def fake_call_llm(model, *a, **k):
        calls.append(model)
        return LLMResponse(content="ok", model=model, input_tokens=1, output_tokens=1,
                           cost_usd=0.0, latency_ms=1.0, provider=model.split("/")[0])

    async def fake_log(**kw):
        logged.update(kw)

    env = {"LLM_ROUTER_ENFORCE": "off", "LLM_ROUTER_SESSION_ID": "shadowsess",
           "LLM_ROUTER_EXECUTION_LEDGER_DB": str(tmp_path / "ledger.db")}
    if flag:
        env["LLM_ROUTER_LOCAL_TIER"] = flag
    with ExitStack() as es:
        p = es.enter_context
        p(patch.dict(os.environ, env))
        if not flag:
            os.environ.pop("LLM_ROUTER_LOCAL_TIER", None)
        p(patch("llm_router.router.get_config", return_value=t._Cfg()))
        tr = MagicMock()
        tr.is_healthy.return_value = True
        p(patch("llm_router.router.get_tracker", return_value=tr))
        ml = MagicMock()
        ml.bind.return_value = MagicMock()
        p(patch("llm_router.router.log", ml))
        p(patch("llm_router.router._native_notify", lambda *a, **k: None))
        for fn in ("get_monthly_spend", "get_daily_spend", "get_daily_spend_by_task_type"):
            p(patch(f"llm_router.router.cost.{fn}", new_callable=AsyncMock, return_value=0.0))
        p(patch("llm_router.router.cost.log_usage", new_callable=AsyncMock))
        p(patch("llm_router.router.cost.log_routing_decision", side_effect=fake_log))
        p(patch("llm_router.policy.load_org_policy", return_value=None))
        p(patch("llm_router.policy.get_active_policy", return_value=None))
        p(patch("llm_router.router.reserve_envelope", new_callable=AsyncMock, return_value=(None, True, "k")))
        p(patch("llm_router.router.commit_envelope", new_callable=AsyncMock))
        p(patch("llm_router.router.release_envelope", new_callable=AsyncMock))
        p(patch("llm_router.semantic_cache.check", new_callable=AsyncMock, return_value=None))
        p(patch("llm_router.semantic_cache.store", new_callable=AsyncMock))
        p(patch("llm_router.quality_feedback.should_skip_model", return_value=False))
        p(patch("llm_router.router._build_and_filter_chain", new_callable=AsyncMock,
                return_value=["openai/gpt-4o-mini", "ollama/a"]))
        p(patch("llm_router.router.providers.call_llm", side_effect=fake_call_llm))
        try:
            resp = await router.route_and_call(
                TaskType.QUERY, "what is 2+2", profile=RoutingProfile.BALANCED,
                classification_data={"task_type": "query", "complexity": "simple"})
        finally:
            await router.drain_bg_tasks(3.0)
    return calls, resp.model, logged


@pytest.mark.asyncio
async def test_router_off_identical_shadow_records_tier_route_unchanged(tmp_path):
    base = await _drive(None, tmp_path)
    off = await _drive("off", tmp_path)
    shadow = await _drive("shadow", tmp_path)
    # Served route (chain tried + model served) is identical in all three.
    assert base[0] == off[0] == shadow[0] == ["openai/gpt-4o-mini"]
    assert base[1] == off[1] == shadow[1] == "openai/gpt-4o-mini"
    assert base[2], "finalizer must have logged a routing decision"
    # Default / off: nothing recorded. Shadow: would-be tier recorded.
    assert base[2]["shadow_tier"] is None and off[2]["shadow_tier"] is None
    assert shadow[2]["shadow_tier"] == "local"
    # Every other logged field is identical (the decision itself did not change).
    def strip(d):
        return {k: v for k, v in d.items() if k not in ("shadow_tier", "correlation_id")}

    assert strip(base[2]) == strip(off[2]) == strip(shadow[2])


def test_log_routing_decision_writes_shadow_tier(monkeypatch, tmp_path):
    from llm_router import cost

    home = tmp_path / "lrhome"
    home.mkdir()
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home))

    async def go():
        await cost.log_routing_decision(
            prompt="p", task_type="query", profile="balanced", classifier_type="heuristic",
            classifier_model=None, classifier_confidence=0.9, classifier_latency_ms=1.0,
            complexity="simple", recommended_model="ollama/x", base_model="ollama/x",
            was_downshifted=False, budget_pct_used=0.0, quality_mode="balanced",
            final_model="ollama/x", final_provider="ollama", success=True, input_tokens=1,
            output_tokens=1, cost_usd=0.0, latency_ms=1.0, shadow_tier="local")
        await cost.log_routing_decision(
            prompt="q", task_type="query", profile="balanced", classifier_type="heuristic",
            classifier_model=None, classifier_confidence=0.9, classifier_latency_ms=1.0,
            complexity="simple", recommended_model="ollama/x", base_model="ollama/x",
            was_downshifted=False, budget_pct_used=0.0, quality_mode="balanced",
            final_model="ollama/x", final_provider="ollama", success=True, input_tokens=1,
            output_tokens=1, cost_usd=0.0, latency_ms=1.0)

    asyncio.run(go())
    conn = sqlite3.connect(home / "usage.db")
    got = [r[0] for r in conn.execute("SELECT shadow_tier FROM routing_decisions ORDER BY id")]
    conn.close()
    assert got == ["local", None]
