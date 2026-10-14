"""P0.8-d: a ``routing_decisions`` row never has a NULL ``session_id``.

Live copy 2026-10-11: routing_decisions ids 2353-2355 (the same router_chain calls as
usage 1885-1887) had NULL session_id. ``cost.log_routing_decision`` stored
``session_id or None``, and ``router.py`` passes ``call_session_id()``, which is None
outside an MCP call. Every writer into usage.db ``routing_decisions`` is exercised here and
the row is read back: cost.log_routing_decision (router, error row, DIRECT hook),
judge_discrimination_eval, backfill_sidecars; and the cc-usage-track ``usage`` hook.
"""
from __future__ import annotations

import asyncio
import io
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from llm_router import call_identity, cost
from llm_router.types import RoutingProfile, TaskType
from tests.test_p05_semantic_cache_key import _route
from tests.test_p05_semantic_cache_key import cache_env as _cache_env
from tests.test_p08e_cache_hit_row import real_usage_writer as _real_usage_writer

cache_env = _cache_env
real_usage_writer = _real_usage_writer
_REPO = Path(__file__).resolve().parent.parent


def _rd(**kw):
    return cost.log_routing_decision(
        prompt="p", task_type="code", profile="balanced", classifier_type="heuristic",
        classifier_model=None, classifier_confidence=0.9, classifier_latency_ms=1.0,
        complexity="simple", recommended_model="m", base_model="m", was_downshifted=False,
        budget_pct_used=0.0, quality_mode="balanced", final_model="m", final_provider="openai",
        success=True, input_tokens=1, output_tokens=1, cost_usd=0.0, latency_ms=1.0,
        reason_code=cost.REASON_ROUTER_CHAIN, **kw)


def _sessions(db, table="routing_decisions"):
    con = sqlite3.connect(str(db))
    try:
        return [r[0] for r in con.execute(f"SELECT session_id FROM {table} ORDER BY id")]
    finally:
        con.close()


@pytest.fixture(autouse=True)
def _no_env_session(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)


def test_none_outside_mcp_is_unknown_non_mcp(temp_db):
    asyncio.run(_rd(session_id=None))
    assert _sessions(temp_db) == ["unknown:non_mcp"]


def test_placeholder_and_empty_are_unknown(temp_db):
    asyncio.run(_rd(session_id="sdk"))
    asyncio.run(_rd(session_id=""))
    assert _sessions(temp_db) == ["unknown:non_mcp"] * 2


def test_a_real_session_is_kept(temp_db):
    asyncio.run(_rd(session_id="sess-real-1"))
    assert _sessions(temp_db) == ["sess-real-1"]


def test_inside_mcp_call_without_a_session_is_unknown_mcp(temp_db):
    async def tool() -> str:
        await _rd(session_id=None)
        return "ok"

    server = call_identity.IdentityMCPServer("probe")
    server.tool()(tool)
    asyncio.run(server.call_tool("tool", {}, None))
    assert _sessions(temp_db) == ["unknown:mcp"]


@pytest.mark.asyncio
async def test_route_and_call_writes_both_rows_with_a_session(cache_env, real_usage_writer):
    """The reproduced live shape: router_chain call with no session anywhere."""
    await _route("explain the retry policy", caller_context="c", calls=[])
    assert _sessions(cache_env, "usage") == ["unknown:non_mcp"]
    assert _sessions(cache_env) == ["unknown:non_mcp"]


def test_error_row_writer_is_never_null(temp_db):
    asyncio.run(cost.log_route_error(TaskType.CODE, RoutingProfile.BALANCED,
                                     reason="error_all_models_failed", correlation_id="c1"))
    assert _sessions(temp_db) == ["unknown:non_mcp"]
    assert _sessions(temp_db, "usage") == ["unknown:non_mcp"]


def test_judge_eval_and_sidecar_backfill_are_never_null(temp_db):
    sys.path.insert(0, str(_REPO / "scripts"))
    try:
        import backfill_sidecars as bs
        import judge_discrimination_eval as jde
    finally:
        sys.path.pop(0)
    asyncio.run(jde._insert_decision("code", "ollama/x"))
    d = temp_db.parent / "sidecars"
    d.mkdir()
    (d / "last_route_no-sess.json").write_text(json.dumps(
        {"task_type": "code", "complexity": "simple", "tool": "llm_code",
         "saved_at": 1_800_000_000.0}))
    asyncio.run(bs.backfill(d, dry_run=False))
    got = _sessions(temp_db)
    # judge eval: no env session -> unknown:script. Backfill: the id comes from the
    # sidecar file name, so it is a real value, not NULL.
    assert got == ["unknown:script", "no-sess"], got


@pytest.mark.parametrize("rel", ["src/llm_router/hooks/cc-usage-track.py", "hooks/cc-usage-track.py"])
def test_usage_hook_without_a_payload_session_is_unknown_hook(tmp_path, monkeypatch, rel):
    import importlib.util

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("LLM_ROUTER_HOME", str(home))
    spec = importlib.util.spec_from_file_location("_p08d_hook_" + rel[:3], _REPO / rel)
    hook = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = hook
    spec.loader.exec_module(hook)
    payload = {"tool_name": "Agent", "tool_input": {"subagent_type": "Explore", "prompt": "p" * 40},
               "tool_response": {"output": "r" * 40}, "duration_ms": 1200}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    with pytest.raises(SystemExit):
        hook.main()
    assert _sessions(home / "usage.db", "usage") == ["unknown:hook"]
