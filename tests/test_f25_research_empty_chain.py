"""F25 / D-170: ``llm(task="research")`` no longer fails silently when the premium research chain is empty.

F26 / D-175 amends this: an empty TEXT chain is answered by a last-resort floor (local, then Codex/Gemini CLI,
then the Claude CLI subscription). The refusal path below therefore needs a nothing-reachable machine (no local
model, no CLI, no Claude subscription, no key); on the incident machine the floor now answers.

Incident (2026-10-16, 12:48-16:07Z, ledger rows 2481-2492): with no Perplexity key and Claude in
subscription mode, ``llm_research`` forced PREMIUM; the premium/research dynamic chain was ``[]``
(#382 removed its only local entry; subscription mode drops ``anthropic``), the router raised
``ValueError("No providers available for research/premium")`` before any attempt, and the MCP ledger
guard wrote ``error_exception`` rows labelled with the *active* profile (balanced). Every router-path
research call failed; nothing logged above debug.

These tests drive the real ``consolidated.llm`` -> ``llm_research`` -> ``route_and_call`` path and
the real chain builder. Only the dynamic table, provider availability and the final provider call
(``providers.call_llm``) are faked: no network, no real model call.
"""
from __future__ import annotations

import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from llm_router import call_identity
from llm_router.types import LLMResponse, RoutingProfile, TaskType
from tests.test_p05_semantic_cache_key import cache_env as _p05_cache_env

cache_env = _p05_cache_env
SID = "f25f25f2-5aaa-bbbb-cccc-f25f25f25f25"
LOCAL = "qwen3-coder:30b"
_KEYS = ("OPENAI_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY", "PERPLEXITY_API_KEY", "DEEPSEEK_API_KEY",
         "GROQ_API_KEY", "MISTRAL_API_KEY", "TOGETHER_API_KEY", "XAI_API_KEY", "COHERE_API_KEY",
         "MINIMAX_API_KEY", "OPENROUTER_API_KEY", "MOONSHOT_API_KEY", "KIMI_API_KEY")


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


@pytest.fixture
def env(cache_env, monkeypatch):
    """The live machine's shape: Claude subscription on, no Perplexity (or any other) key, codex and
    gemini CLI absent, every dynamic research chain empty. ``ollama`` decides whether a local model
    is installed and configured."""
    import llm_router.config as cfg_mod
    from llm_router import dynamic_routing, router

    for k in _KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LLM_ROUTER_CLAUDE_SUBSCRIPTION", "true")
    monkeypatch.setenv("LLM_ROUTER_GEMINI_SUBSCRIPTION", "false")
    monkeypatch.setenv("LLM_ROUTER_PROFILE", "balanced")
    monkeypatch.setenv("LLM_ROUTER_ENFORCE", "off")
    monkeypatch.setenv("LLM_ROUTER_SEMANTIC_CACHE", "off")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
    monkeypatch.setattr(router, "is_codex_available", lambda: False)
    monkeypatch.setattr(router, "is_gemini_cli_available", lambda: False)
    monkeypatch.setattr("llm_router.claude_usage.get_claude_pressure", lambda *a, **k: 0.0)
    tables: dict[tuple[RoutingProfile, TaskType], list[str]] = {}
    monkeypatch.setattr(dynamic_routing, "get_dynamic_model_chain",
                        lambda p, t: list(tables.get((p, t), [])))
    getattr(router, "_EMPTY_DYNAMIC_CHAIN_WARNED", set()).clear()
    resolved: list[RoutingProfile] = []
    _real_resolve = router._resolve_profile

    def _spy(*a, **k):
        out = _real_resolve(*a, **k)
        resolved.append(out[0])
        return out
    monkeypatch.setattr(router, "_resolve_profile", _spy)

    def ollama(installed: bool, subscription: bool = True):
        # F26 / D-175: with Claude in subscription mode the last-resort floor reaches the Claude CLI, so the
        # nothing-reachable fixture (where research is still refused) needs ``subscription=False`` too.
        monkeypatch.setenv("LLM_ROUTER_CLAUDE_SUBSCRIPTION", "true" if subscription else "false")
        if installed:
            monkeypatch.setenv("OLLAMA_BUDGET_MODELS", LOCAL)
        else:
            monkeypatch.delenv("OLLAMA_BUDGET_MODELS", raising=False)
        models = [f"ollama/{LOCAL}"] if installed else []
        monkeypatch.setattr(cfg_mod.RouterConfig, "all_ollama_models", lambda self: list(models))
        cfg_mod._config = None
        cfg = cfg_mod.get_config()
        object.__setattr__(cfg, "llm_router_db_path", cache_env)
        return cfg

    ollama.tables = tables
    ollama.resolved = resolved  # the profile route_and_call resolved, per call
    ollama.db = cache_env
    yield ollama
    getattr(router, "_EMPTY_DYNAMIC_CHAIN_WARNED", set()).clear()


async def _research(calls: list, *, tool_use="toolu_f25"):
    from llm_router import router
    from llm_router.tools import consolidated

    def fake_call_llm(model, messages, **kw):
        calls.append(model)
        return LLMResponse(content="Local answer about the topic.", model=model, input_tokens=11,
                           output_tokens=7, cost_usd=0.0, latency_ms=5.0,
                           provider=model.split("/", 1)[0])

    tok = call_identity.bind(tool_use)
    try:
        with patch("llm_router.router.providers.call_llm", new_callable=AsyncMock,
                   side_effect=fake_call_llm), \
             patch("llm_router.quality_feedback.should_skip_model", return_value=False):
            out = await consolidated.llm("What changed in the 2026 EU AI Act guidance?", _Ctx(),
                                         task="research")
            await router.drain_bg_tasks(3.0)
    finally:
        call_identity.reset(tok)
    return out


# ── (1) premium only when reachable ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_perplexity_subscription_with_ollama_answers_via_the_floor(env):
    """The incident machine plus a local model: research is answered by the local model (F26: through the
    floor of the empty premium chain, local first), and the answer says it is not web-grounded."""
    env(installed=True)
    calls: list = []
    out = await _research(calls)

    assert calls == [f"ollama/{LOCAL}"], out
    assert "Local answer about the topic." in out
    assert "UNVERIFIED — not web-grounded" in out
    rows = _q(env.db, "SELECT task_type, profile, final_model, reason_code FROM routing_decisions "
                      "WHERE task_type = 'research'")
    assert rows and rows[-1][1] == env.resolved[-1].value and rows[-1][2] == f"ollama/{LOCAL}", rows
    assert not any(r[3] == "error_exception" for r in rows), rows


@pytest.mark.asyncio
async def test_reachable_premium_research_chain_is_still_chosen(env):
    """A reachable premium research provider keeps the old escalation: PREMIUM is requested and
    its model answers, ahead of the local model the default profile would have used."""
    env(installed=True)
    env.tables[(RoutingProfile.PREMIUM, TaskType.RESEARCH)] = ["anthropic/claude-opus-4-1"]
    calls: list = []
    out = await _research(calls)

    assert calls and calls[0] == "anthropic/claude-opus-4-1", (calls, out)
    assert env.resolved == [RoutingProfile.PREMIUM]
    rows = _q(env.db, "SELECT profile, final_model FROM routing_decisions WHERE task_type = 'research'")
    assert rows and rows[-1] == ("premium", "anthropic/claude-opus-4-1"), rows


@pytest.mark.asyncio
async def test_the_reachability_probe_is_the_router_chain_builder(env):
    from llm_router import router
    from llm_router.tools import text

    cfg = env(installed=False, subscription=False)  # nothing reachable: the floor is empty too
    assert await text._premium_research_reachable(cfg) is False
    env.tables[(RoutingProfile.PREMIUM, TaskType.RESEARCH)] = ["anthropic/claude-opus-4-1"]
    cfg = env(installed=False, subscription=True)
    assert await text._premium_research_reachable(cfg) is True
    env.tables.clear()
    router._FLOOR_TIERS.clear()
    router._FLOOR_WARNED.clear()
    cfg = env(installed=True)  # F26: the probe runs with the floor off, so an empty table stays empty
    assert await text._premium_research_reachable(cfg) is False
    assert not router._FLOOR_TIERS and not router._FLOOR_WARNED


# ── (2) an empty research chain is a refusal with its own reason ────────────

@pytest.mark.asyncio
async def test_empty_research_chain_is_a_refusal_not_an_error(env):
    env(installed=False, subscription=False)
    calls: list = []
    out = await _research(calls)  # must not raise

    assert calls == []
    assert out.startswith("[llm_router] research refused (refused_no_research_provider)"), out
    assert len(env.resolved) == 1 and env.resolved[0] != RoutingProfile.PREMIUM, env.resolved
    assert f"research/{env.resolved[0].value}" in out and "research/premium" in out, out
    assert "API_KEY" not in out and "GEMINI" not in out, out
    u = _q(env.db, "SELECT session_id, task_type, reason, success, cost_usd FROM usage")
    r = _q(env.db, "SELECT session_id, task_type, reason_code, success, final_model FROM routing_decisions")
    assert u == [(SID, "research", "refused_no_research_provider", 0, 0.0)], u
    assert r == [(SID, "research", "refused_no_research_provider", 0, "none")], r


@pytest.mark.asyncio
async def test_refusal_row_records_the_calls_real_profile_not_the_active_one(env, monkeypatch):
    """(4): the guard row names the profile the router resolved for the call (from the prompt), not
    the process-wide active profile (here REASONING, which this prompt never resolves to)."""
    from llm_router import state

    env(installed=False, subscription=False)
    monkeypatch.setattr(state, "get_active_profile", lambda: RoutingProfile.REASONING)
    await _research([])
    assert len(env.resolved) == 1 and env.resolved[0] != RoutingProfile.REASONING, env.resolved
    real = env.resolved[0].value
    assert _q(env.db, "SELECT profile FROM usage") == [(real,)]
    assert _q(env.db, "SELECT profile FROM routing_decisions") == [(real,)]


@pytest.mark.asyncio
async def test_error_row_for_another_task_also_records_the_real_profile(env, monkeypatch):
    """(4) is not research-only: an error row the guard writes for any routed call names the
    resolved profile."""
    from llm_router import state
    from llm_router.tools import consolidated

    env(installed=False, subscription=False)
    monkeypatch.setattr(state, "get_active_profile", lambda: RoutingProfile.REASONING)
    with pytest.raises(ValueError):
        await consolidated.llm("write a haiku", _Ctx(), task="generate")
    assert len(env.resolved) == 1 and env.resolved[0] != RoutingProfile.REASONING, env.resolved
    assert _q(env.db, "SELECT profile, reason FROM usage") == [(env.resolved[0].value, "error_exception")]


def test_refusal_rows_are_excluded_from_every_decision_predicate():
    from llm_router import cost
    from llm_router.provider_classes import (SQL_NOT_ERROR_ROW, SQL_REAL_DECISION, is_non_decision_reason,
                                             not_error_row_sql, real_decision_sql)

    code = "refused_no_research_provider"
    assert cost.REASON_REFUSED_NO_RESEARCH_PROVIDER == code
    assert is_non_decision_reason(code)
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE usage (reason TEXT)")
    c.execute("CREATE TABLE routing_decisions (reason_code TEXT, final_provider TEXT)")
    for x in ("router_chain", code):
        c.execute("INSERT INTO usage VALUES (?)", (x,))
        c.execute("INSERT INTO routing_decisions VALUES (?, 'none')", (x,))
    for pred in (SQL_NOT_ERROR_ROW, not_error_row_sql(c)):
        assert c.execute(f"SELECT reason FROM usage WHERE {pred}").fetchall() == [("router_chain",)]
    for pred in (SQL_REAL_DECISION, real_decision_sql(c)):
        assert c.execute(f"SELECT reason_code FROM routing_decisions WHERE {pred}").fetchall() == [
            ("router_chain",)]


# ── (3) an empty dynamic chain is logged at WARNING ─────────────────────────

@pytest.mark.asyncio
async def test_empty_dynamic_chain_logs_a_warning_once(env, monkeypatch):
    from llm_router import router
    from llm_router.types import Complexity

    cfg = env(installed=False, subscription=False)
    log = MagicMock()
    monkeypatch.setattr(router, "log", log)
    for _ in range(2):
        await router._build_and_filter_chain(TaskType.RESEARCH, RoutingProfile.PREMIUM, None, None,
                                             Complexity.MODERATE, cfg)
    hits = [c for c in log.warning.call_args_list if "is empty" in c.args[0]]
    assert len(hits) == 1, log.warning.call_args_list
    assert (hits[0].args[0] % hits[0].args[1:]).startswith("Dynamic routing chain premium/research is empty")


@pytest.mark.asyncio
async def test_non_empty_dynamic_chain_logs_no_warning(env, monkeypatch):
    from llm_router import router
    from llm_router.types import Complexity

    cfg = env(installed=False)
    env.tables[(RoutingProfile.PREMIUM, TaskType.RESEARCH)] = ["anthropic/claude-opus-4-1"]
    log = MagicMock()
    monkeypatch.setattr(router, "log", log)
    await router._build_and_filter_chain(TaskType.RESEARCH, RoutingProfile.PREMIUM, None, None,
                                         Complexity.MODERATE, cfg)
    assert not [c for c in log.warning.call_args_list if "is empty" in c.args[0]]
