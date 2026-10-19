"""F112 / D-242: jobs gated by a mechanical acceptance check keep a failed-lane local model.

llm_text_job (check_output + fallback, qa lane) and llm_edit (validated edits, code lane) pass
``eligibility_exempt_reason`` to route_and_call. The F106 filter then keeps the chain and notes
``eligibility_bypassed``. Plain callers are unchanged. Model names are fixtures (D-205).
"""
from __future__ import annotations

import json
import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from llm_router import eligibility_routing as er
from llm_router import skip_causes
from llm_router.qa_policy import strip_local_for_qa
from llm_router.types import Complexity, TaskType
from tests.test_f106_eligibility_in_router import (
    BAD, CHAIN, FAILED, GEMINI, OTHER, _qa, _resp, _write,
)

CODE_FAILED = {"code": {"simple": FAILED, "moderate": FAILED}}


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("LLM_ROUTER_HOME", str(h))
    monkeypatch.delenv("LLM_ROUTER_IGNORE_ELIGIBILITY", raising=False)
    er.reset_cache()
    yield h
    er.reset_cache()


@pytest.fixture
def env(home, mock_env, temp_db, tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_PROVIDER_RESET_PATH", str(tmp_path / "provider_reset.json"))
    monkeypatch.setattr("llm_router.router.is_codex_available", lambda: False)
    monkeypatch.setenv("LLM_ROUTER_PROMOTE_GATE", "off")
    monkeypatch.setenv("LLM_ROUTER_BANDIT", "off")
    return temp_db


# ── unit: note_bypass ──────────────────────────────────────────────────────

def test_note_bypass_keeps_chain_and_records_each_would_be_drop(home):
    _write(home, _qa(FAILED))
    skip_causes.begin()
    assert er.note_bypass(CHAIN, TaskType.GENERATE, Complexity.SIMPLE, "verified_text_job") == CHAIN
    assert skip_causes.entries_so_far() == [{
        "model": BAD, "reason": "eligibility_bypassed", "lane": "qa/simple",
        "source": "verified_text_job",
    }]
    assert skip_causes.ELIGIBILITY_BYPASSED in skip_causes.BUILD_REASONS


def test_note_bypass_records_nothing_without_a_failed_lane_or_when_opted_out(home, monkeypatch):
    skip_causes.begin()
    assert er.note_bypass(CHAIN, TaskType.GENERATE, Complexity.SIMPLE, "x") == CHAIN  # no file
    _write(home, _qa(FAILED))
    assert er.note_bypass(CHAIN, TaskType.GENERATE, Complexity.COMPLEX, "x") == CHAIN  # no lane
    monkeypatch.setenv("LLM_ROUTER_IGNORE_ELIGIBILITY", "1")
    assert er.note_bypass(CHAIN, TaskType.GENERATE, Complexity.SIMPLE, "x") == CHAIN
    assert skip_causes.entries_so_far() == []


# ── router: plain call still filters; exempt call keeps ───────────────────

async def _route(chain, task, *, exempt=None, serve=None, complexity="simple"):
    from llm_router.router import route_and_call

    tried: list[str] = []

    async def _build(*a, **k):
        return list(chain)

    async def _call_text(model, *a, **k):
        tried.append(model)
        if model != serve:
            raise RuntimeError("boom")
        return _resp(model)

    kw = {"eligibility_exempt_reason": exempt} if exempt else {}
    with patch("llm_router.cost.get_daily_spend", new_callable=AsyncMock, return_value=0.0), \
         patch("llm_router.cost.get_monthly_spend", new_callable=AsyncMock, return_value=0.0), \
         patch("llm_router.router._build_and_filter_chain", _build), \
         patch("llm_router.router._call_text", _call_text):
        call = route_and_call(task, "In one sentence, what does `wc -l` do?",
                              complexity_hint=complexity, **kw)
        if serve is None:
            with pytest.raises(Exception):  # noqa: B017,PT011 - every model fails by design
                await call
        else:
            await call
    return tried


@pytest.mark.asyncio
async def test_plain_generate_still_drops_the_failed_lane_model(env, home):
    _write(home, _qa(FAILED))
    tried = await _route(CHAIN, TaskType.GENERATE)
    assert BAD not in tried and GEMINI in tried


@pytest.mark.asyncio
async def test_exempt_generate_keeps_the_failed_lane_model(env, home):
    _write(home, _qa(FAILED))
    tried = await _route(CHAIN, TaskType.GENERATE, exempt="verified_text_job")
    assert BAD in tried and GEMINI in tried and OTHER in tried


@pytest.mark.asyncio
async def test_exempt_code_lane_keeps_and_plain_code_drops(env, home):
    _write(home, CODE_FAILED)
    assert BAD not in await _route(CHAIN, TaskType.CODE)
    assert BAD in await _route(CHAIN, TaskType.CODE, exempt="validated_edit")


@pytest.mark.asyncio
async def test_bypass_is_in_the_ledger_and_in_why(env, home, tmp_path):
    import shutil

    from llm_router import why

    _write(home, _qa(FAILED))
    await _route([BAD, GEMINI], TaskType.GENERATE, exempt="verified_text_job", serve=BAD)
    con = sqlite3.connect(str(env))
    con.row_factory = sqlite3.Row
    try:
        (row,) = con.execute("SELECT * FROM routing_decisions").fetchall()
    finally:
        con.close()
    assert json.loads(row["skipped_json"]) == [{
        "model": BAD, "reason": "eligibility_bypassed", "lane": "qa/simple",
        "source": "verified_text_job"}]
    assert row["served_by_last_resort"] == 0
    base = tmp_path / "why"
    base.mkdir()
    shutil.copy(env, base / "usage.db")
    recs, _ = why.last_decisions(1, base=base)
    text = why.render(recs[0])
    assert f"{BAD}: eligibility_bypassed" in text and "verified_text_job" in text
    assert "wc -l" not in text


@pytest.mark.asyncio
async def test_emergency_budget_chain_honours_the_exemption(env, home):
    from llm_router.router import route_and_call
    from llm_router.types import RoutingProfile

    _write(home, _qa(FAILED))

    async def run(exempt):
        tried: list[str] = []

        async def _build(task_type, profile, *a, **k):
            return [BAD, OTHER] if profile == RoutingProfile.BUDGET else [GEMINI]

        async def _call_text(model, *a, **k):
            tried.append(model)
            raise RuntimeError("boom")

        kw = {"eligibility_exempt_reason": exempt} if exempt else {}
        with patch("llm_router.cost.get_daily_spend", new_callable=AsyncMock, return_value=0.0), \
             patch("llm_router.cost.get_monthly_spend", new_callable=AsyncMock, return_value=0.0), \
             patch("llm_router.router._build_and_filter_chain", _build), \
             patch("llm_router.router._call_text", _call_text):
            with pytest.raises(Exception):  # noqa: B017,PT011 - every model fails by design
                await route_and_call(TaskType.GENERATE, "In one sentence, what does `wc -l` do?",
                                     complexity_hint="moderate", **kw)
        return tried

    plain = await run(None)
    assert OTHER in plain and BAD not in plain
    assert BAD in await run("verified_text_job")


# ── the two callers pass their reason, and it reaches the filter ──────────

@pytest.mark.asyncio
async def test_llm_text_job_path_keeps_the_failed_lane_model(env, home, monkeypatch):
    from llm_router.router import route_and_call as real
    from llm_router.tools.text import llm_text_job

    monkeypatch.setenv("LLM_ROUTER_LOCAL_TEXT_JOBS", "1")
    _write(home, _qa(FAILED))
    seen: list[str] = []
    tried: list[str] = []

    async def wrapped(*a, **k):
        seen.append(k.get("eligibility_exempt_reason"))
        return await real(*a, complexity_hint="simple", **k)

    async def _build(*a, **k):
        return list(CHAIN)

    async def _call_text(model, *a, **k):
        tried.append(model)
        raise RuntimeError("boom")

    ctx = MagicMock()
    ctx.request_id = "rid"
    with patch("llm_router.cost.get_daily_spend", new_callable=AsyncMock, return_value=0.0), \
         patch("llm_router.cost.get_monthly_spend", new_callable=AsyncMock, return_value=0.0), \
         patch("llm_router.router._build_and_filter_chain", _build), \
         patch("llm_router.router._call_text", _call_text), \
         patch("llm_router.tools.text.route_and_call", wrapped), \
         patch("llm_router.tools.text._announce_routing", new_callable=AsyncMock):
        try:
            await llm_text_job("commit_message", "diff --git a/x b/x\n+x = 1\n", ctx)
        except Exception:  # noqa: BLE001 - every model fails by design; only the chain matters
            pass
    assert seen and set(seen) == {"verified_text_job"}
    assert BAD in tried


@pytest.mark.asyncio
async def test_llm_edit_path_keeps_the_failed_lane_model(env, home, tmp_path):
    from llm_router.router import route_and_call as real
    from llm_router.tools.text import llm_edit

    _write(home, CODE_FAILED)
    f = tmp_path / "a.py"
    f.write_text("x = 1\n")
    seen: list[str] = []
    tried: list[str] = []

    async def wrapped(*a, **k):
        seen.append(k.get("eligibility_exempt_reason"))
        return await real(*a, complexity_hint="simple", **k)

    async def _build(*a, **k):
        return list(CHAIN)

    async def _call_text(model, *a, **k):
        tried.append(model)
        raise RuntimeError("boom")

    ctx = MagicMock()
    ctx.request_id = "rid"
    with patch("llm_router.cost.get_daily_spend", new_callable=AsyncMock, return_value=0.0), \
         patch("llm_router.cost.get_monthly_spend", new_callable=AsyncMock, return_value=0.0), \
         patch("llm_router.router._build_and_filter_chain", _build), \
         patch("llm_router.router._call_text", _call_text), \
         patch("llm_router.tools.text.route_and_call", wrapped), \
         patch("llm_router.tools.text._announce_routing", new_callable=AsyncMock):
        try:
            await llm_edit("bump x", [str(f)], ctx)
        except Exception:  # noqa: BLE001 - every model fails by design; only the chain matters
            pass
    assert seen and set(seen) == {"validated_edit"}
    assert BAD in tried


def test_exempt_reason_is_not_an_mcp_parameter():
    import inspect

    from llm_router.tools import text

    for fn in (text.llm_text_job, text.llm_edit):
        assert "eligibility_exempt_reason" not in inspect.signature(fn).parameters


# ── ledger_view: a bypass note is not a skip ──────────────────────────────

def test_served_local_with_bypass_note_is_not_other_skip(tmp_path):
    from llm_router.routing_report import executed_routes
    from tests.test_f23_skip_causes import _fixture_db

    bypass = '{"lane":"qa/simple","model":"ollama/m","reason":"eligibility_bypassed","source":"verified_text_job"}'
    budget = '{"model":"gemini/x","reason":"budget"}'
    failed = '{"lane":"qa/simple","model":"ollama/m","reason":"failed_eligibility"}'
    p = tmp_path / "usage.db"
    con = _fixture_db(p)
    for sk in (f"[{bypass}]", f"[{bypass},{bypass}]", f"[{bypass},{budget}]", f"[{failed}]", f"[{budget}]"):
        con.execute("INSERT INTO routing_decisions (timestamp, final_provider, provenance, skipped_json, "
                    "served_by_last_resort) VALUES (datetime('now'), 'ollama', 'runtime', ?, 0)", (sk,))
    con.commit()
    con.close()
    got = executed_routes(24.0, db_path=p)["local_by_cause"]
    # bypass-only x2 -> policy (no skip); bypass+budget, failed_eligibility, budget -> other_skip
    assert (got["policy"], got["other_skip"]) == (2, 3)


# ── D-116 finding: where does a local model sit in the GENERATE chain? ────

def test_d116_local_trails_every_non_claude_entry_for_generate():
    claude = "anthropic/claude-haiku-4-5"
    local = "ollama/fixture-local:7b"
    # A non-local, non-Claude model ahead of it: local is moved BEHIND it.
    assert strip_local_for_qa([local, GEMINI, claude], TaskType.GENERATE) == [GEMINI, local, claude]
    # Only Claude otherwise: local leads.
    assert strip_local_for_qa([local, claude], TaskType.GENERATE) == [local, claude]
