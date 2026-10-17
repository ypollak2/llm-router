"""F26 / D-175: a TEXT chain that is still empty after filters and injections is answered by a last-resort floor.

Order: local (any profile, P0.15 gate honoured) -> Codex / Gemini CLI -> the static chain re-filtered by the
provider predicate (Claude only through the CLI subscription, never an API key) -> ``NoRoutableModelsError``.
Media keeps raising; a chain that is non-empty without the floor is returned byte-identical.

The machine is the live one: no provider key, dynamic table empty for every chain. Only provider availability
(Codex, Ollama, Claude subscription), the dynamic table and the final provider call are faked.
"""
from __future__ import annotations

import itertools
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from llm_router import call_identity
from llm_router.types import Complexity, LLMResponse, RoutingProfile, TaskType
from tests.test_p05_semantic_cache_key import cache_env as _p05_cache_env

cache_env = _p05_cache_env
LOCAL = "qwen3-coder:30b"
PROFILES = [RoutingProfile.BUDGET, RoutingProfile.BALANCED, RoutingProfile.PREMIUM, RoutingProfile.REASONING]
TEXT = [TaskType.QUERY, TaskType.GENERATE, TaskType.ANALYZE, TaskType.CODE, TaskType.RESEARCH]
MEDIA = [TaskType.IMAGE, TaskType.VIDEO, TaskType.AUDIO]
COMBOS = list(itertools.product([False, True], repeat=3))  # (codex, ollama, subscription)
_KEYS = ("OPENAI_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY", "PERPLEXITY_API_KEY", "DEEPSEEK_API_KEY",
         "GROQ_API_KEY", "MISTRAL_API_KEY", "TOGETHER_API_KEY", "XAI_API_KEY", "COHERE_API_KEY",
         "MINIMAX_API_KEY", "OPENROUTER_API_KEY", "MOONSHOT_API_KEY", "KIMI_API_KEY",
         "LLM_ROUTER_BLOCK_PROVIDERS")
_TIER = {"ollama": 0, "codex": 1, "gemini_cli": 1, "anthropic": 2}


class _Ctx:
    async def info(self, *a, **k):
        pass

    async def report_progress(self, *a, **k):
        pass


@pytest.fixture
def machine(cache_env, monkeypatch):
    import llm_router.config as cfg_mod
    from llm_router import dynamic_routing, router

    for k in _KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LLM_ROUTER_GEMINI_SUBSCRIPTION", "false")
    monkeypatch.setenv("LLM_ROUTER_PROFILE", "balanced")
    monkeypatch.setenv("LLM_ROUTER_ENFORCE", "off")
    monkeypatch.setenv("LLM_ROUTER_SEMANTIC_CACHE", "off")
    monkeypatch.setattr(router, "is_gemini_cli_available", lambda: False)
    monkeypatch.setattr("llm_router.claude_usage.get_claude_pressure", lambda *a, **k: 0.0)
    tables: dict = {}
    monkeypatch.setattr(dynamic_routing, "get_dynamic_model_chain", lambda p, t: list(tables.get((p, t), [])))
    router._FLOOR_WARNED.clear()
    router._EMPTY_DYNAMIC_CHAIN_WARNED.clear()

    def setup(codex=False, ollama=False, subscription=False):
        monkeypatch.setenv("LLM_ROUTER_CLAUDE_SUBSCRIPTION", "true" if subscription else "false")
        monkeypatch.setattr(router, "is_codex_available", lambda: codex)
        if ollama:
            monkeypatch.setenv("OLLAMA_BUDGET_MODELS", LOCAL)
        else:
            monkeypatch.delenv("OLLAMA_BUDGET_MODELS", raising=False)
        models = [f"ollama/{LOCAL}"] if ollama else []
        monkeypatch.setattr(cfg_mod.RouterConfig, "all_ollama_models", lambda self: list(models))
        cfg_mod._config = None
        cfg = cfg_mod.get_config()
        object.__setattr__(cfg, "llm_router_db_path", cache_env)
        return cfg

    setup.tables = tables
    setup.db = cache_env
    setup.monkeypatch = monkeypatch
    yield setup
    router._FLOOR_WARNED.clear()
    router._EMPTY_DYNAMIC_CHAIN_WARNED.clear()


async def _chain(task, profile, cfg, **kw):
    from llm_router import router

    return await router._build_and_filter_chain(task, profile, None, None, Complexity.MODERATE, cfg, **kw)


def _prov(m):
    return m.split("/", 1)[0]


# -- the matrix: 4 profiles x 5 text tasks x {codex, ollama, subscription} on/off --

@pytest.mark.asyncio
@pytest.mark.parametrize("codex,ollama,sub", COMBOS)
async def test_matrix_never_empty_while_anything_is_reachable(machine, codex, ollama, sub):
    cfg = machine(codex=codex, ollama=ollama, subscription=sub)
    checked = 0
    for profile, task in itertools.product(PROFILES, TEXT):
        chain = await _chain(task, profile, cfg)
        where = f"{profile.value}/{task.value} codex={codex} ollama={ollama} sub={sub}: {chain}"
        assert bool(chain) == (codex or ollama or sub), where
        provs = {_prov(m) for m in chain}
        assert provs <= {"ollama", "codex", "anthropic"}, where
        assert ("anthropic" in provs) <= sub, where
        assert ("ollama" in provs) <= ollama, where
        assert ("codex" in provs) <= codex, where
        ranks = [_TIER[_prov(m)] for m in chain]
        assert ranks == sorted(ranks), where  # local, then Codex, then Claude
        checked += 1
    assert checked == 20


@pytest.mark.asyncio
@pytest.mark.parametrize("codex,ollama,sub", COMBOS)
async def test_matrix_a_chain_that_was_not_empty_is_byte_identical(machine, codex, ollama, sub):
    """The floor never touches a chain that the old code already filled."""
    cfg = machine(codex=codex, ollama=ollama, subscription=sub)
    same = 0
    for profile, task in itertools.product(PROFILES, TEXT):
        before = await _chain(task, profile, cfg, last_resort_floor=False)
        after = await _chain(task, profile, cfg)
        if before:
            assert after == before, (profile.value, task.value, before, after)
            same += 1
    if codex or ollama:
        assert same > 0  # the comparison saw non-empty chains


@pytest.mark.asyncio
async def test_golden_static_and_populated_dynamic_chains_are_identical(machine):
    from llm_router import dynamic_routing

    cfg = machine(codex=True, ollama=True, subscription=True)
    machine.tables[(RoutingProfile.PREMIUM, TaskType.QUERY)] = ["xai/grok-3"]
    machine.tables[(RoutingProfile.BALANCED, TaskType.ANALYZE)] = ["anthropic/claude-sonnet-4-6"]
    compared = 0
    for profile, task in itertools.product(PROFILES, TEXT):
        off = await _chain(task, profile, cfg, last_resort_floor=False)
        if off:  # an empty one is the floor's business, covered above
            assert await _chain(task, profile, cfg) == off
            compared += 1
    assert compared >= 12
    machine.monkeypatch.setattr(dynamic_routing, "get_dynamic_model_chain", lambda p, t: None)  # static chains
    for profile, task in itertools.product(PROFILES, TEXT):
        on, off = await _chain(task, profile, cfg), await _chain(task, profile, cfg, last_resort_floor=False)
        assert on == off and on, (profile.value, task.value, on, off)


@pytest.mark.asyncio
async def test_floor_serves_the_chains_that_used_to_be_empty(machine):
    """premium/reasoning x research is empty on main even with Codex up (research is not a Codex task and
    Ollama is not injected for premium); the floor reaches the local model first, then Codex, then Claude."""
    cfg = machine(codex=True, ollama=True, subscription=True)
    for profile in (RoutingProfile.PREMIUM, RoutingProfile.REASONING):
        assert await _chain(TaskType.RESEARCH, profile, cfg, last_resort_floor=False) == []
        chain = await _chain(TaskType.RESEARCH, profile, cfg)
        assert chain[0] == f"ollama/{LOCAL}", chain
        assert chain[1].startswith("codex/"), chain
        assert any(m.startswith("anthropic/") for m in chain[2:]), chain
        assert not any(m.startswith("anthropic/") for m in chain[:2]), chain


@pytest.mark.asyncio
async def test_claude_is_last(machine):
    cfg = machine(codex=False, ollama=False, subscription=True)
    chain = await _chain(TaskType.RESEARCH, RoutingProfile.PREMIUM, cfg)
    assert chain and all(m.startswith("anthropic/") for m in chain), chain
    cfg = machine(codex=True, ollama=True, subscription=True)
    chain = await _chain(TaskType.RESEARCH, RoutingProfile.PREMIUM, cfg)
    assert _prov(chain[-1]) == "anthropic" and _prov(chain[0]) == "ollama", chain


# -- media keeps raising --

@pytest.mark.asyncio
@pytest.mark.parametrize("task", MEDIA)
async def test_media_chains_stay_empty_and_raise(machine, task):
    from llm_router import router

    cfg = machine(codex=True, ollama=True, subscription=True)
    for profile in PROFILES:
        assert await _chain(task, profile, cfg) == [], (task.value, profile.value)
    with pytest.raises(router.NoRoutableModelsError) as ei:
        await router.route_and_call(task, "draw a cat", profile=RoutingProfile.BALANCED)
    assert ei.value.task_type == task


# -- the filters still win --

@pytest.mark.asyncio
async def test_hard_block_wins_over_the_floor(machine, monkeypatch):
    from llm_router import router

    cfg = machine(codex=True, ollama=True, subscription=True)
    monkeypatch.setenv("LLM_ROUTER_BLOCK_PROVIDERS", "ollama,codex")
    chain = await _chain(TaskType.RESEARCH, RoutingProfile.PREMIUM, cfg)
    assert chain and all(m.startswith("anthropic/") for m in chain), chain
    monkeypatch.setenv("LLM_ROUTER_BLOCK_PROVIDERS", "ollama,codex,anthropic")
    assert await _chain(TaskType.RESEARCH, RoutingProfile.PREMIUM, cfg) == []
    with pytest.raises(router.NoRoutableModelsError) as ei:
        await router.route_and_call(TaskType.RESEARCH, "explain x", profile=RoutingProfile.PREMIUM)
    assert (ei.value.task_type, ei.value.profile) == (TaskType.RESEARCH, RoutingProfile.PREMIUM)
    assert "research/premium" in str(ei.value)


@pytest.mark.asyncio
async def test_repo_block_providers_and_policy_win_over_the_floor(machine, monkeypatch):
    from llm_router import router

    cfg = machine(codex=True, ollama=True, subscription=True)
    repo = SimpleNamespace(block_providers=["ollama", "codex"], block_models=[], allow_models=[],
                           model_override=lambda t: None, provider_override=lambda t: None,
                           agentic_model=None)
    monkeypatch.setattr(router, "get_repo_config", lambda: repo)
    chain = await _chain(TaskType.RESEARCH, RoutingProfile.PREMIUM, cfg)
    assert chain and all(m.startswith("anthropic/") for m in chain), chain
    repo.block_providers = []
    repo.block_models = [f"ollama/{LOCAL}"]  # policy path: a blocked model
    chain = await _chain(TaskType.RESEARCH, RoutingProfile.PREMIUM, cfg)
    assert chain and f"ollama/{LOCAL}" not in chain, chain


@pytest.mark.asyncio
async def test_resource_map_disconnected_provider_wins_over_the_floor(machine, monkeypatch):
    from llm_router import map_reader

    cfg = machine(codex=True, ollama=True, subscription=True)
    real = map_reader.apply_to_chain

    def _drop_ollama(chain, *a, **k):
        return real([m for m in chain if not str(m).startswith("ollama/")], *a, **k)

    monkeypatch.setattr(map_reader, "apply_to_chain", _drop_ollama)
    chain = await _chain(TaskType.RESEARCH, RoutingProfile.PREMIUM, cfg)
    assert chain and not any(m.startswith("ollama/") for m in chain), chain


@pytest.mark.asyncio
async def test_unvalidated_local_model_is_absent_from_a_critical_floor(machine, monkeypatch):
    """P0.15: on a critical chain (premium) a local model the gate has not validated is dropped."""
    from llm_router import router

    cfg = machine(codex=False, ollama=True, subscription=False)
    monkeypatch.setattr(router, "order_for_chain",
                        lambda models, task, **kw: ([], [] if kw["critical"] else list(models)))
    assert await _chain(TaskType.RESEARCH, RoutingProfile.PREMIUM, cfg) == []


@pytest.mark.asyncio
async def test_model_override_is_not_a_floor_path(machine):
    from llm_router import router

    cfg = machine(codex=True, ollama=True, subscription=True)
    out = await router._build_and_filter_chain(TaskType.RESEARCH, RoutingProfile.PREMIUM, "openai/gpt-4o", None,
                                               Complexity.MODERATE, cfg)
    assert out == ["openai/gpt-4o"]


# -- warning + recorded floor tier --

@pytest.mark.asyncio
async def test_floor_warns_once_per_chain_and_records_the_tier(machine, monkeypatch):
    from llm_router import router

    cfg = machine(codex=True, ollama=True, subscription=True)
    log = MagicMock()
    monkeypatch.setattr(router, "log", log)
    guard, tok = call_identity.open_ledger_guard()
    try:
        for _ in range(3):
            await _chain(TaskType.RESEARCH, RoutingProfile.PREMIUM, cfg)
        assert guard.floor_tier == "local"
    finally:
        call_identity.close_ledger_guard(tok)
    hits = [c for c in log.warning.call_args_list if "last-resort floor" in c.args[0]]
    assert len(hits) == 1, log.warning.call_args_list
    assert hits[0].args[1:3] == ("premium", "research")
    assert router._FLOOR_TIERS[("premium", "research")] == "local"
    await _chain(TaskType.RESEARCH, RoutingProfile.REASONING, cfg)  # another chain warns again
    assert len([c for c in log.warning.call_args_list if "last-resort floor" in c.args[0]]) == 2


@pytest.mark.asyncio
async def test_non_floor_chain_records_and_warns_nothing(machine, monkeypatch):
    from llm_router import router

    cfg = machine(codex=True, ollama=True, subscription=True)
    log = MagicMock()
    monkeypatch.setattr(router, "log", log)
    guard, tok = call_identity.open_ledger_guard()
    try:
        await _chain(TaskType.QUERY, RoutingProfile.BALANCED, cfg)
        assert guard.floor_tier is None
    finally:
        call_identity.close_ledger_guard(tok)
    assert not [c for c in log.warning.call_args_list if "last-resort floor" in c.args[0]]


@pytest.mark.asyncio
@pytest.mark.parametrize("codex,ollama,sub,tier", [(True, False, False, "cli"), (False, False, True, "claude_cli"),
                                                    (False, True, False, "local")])
async def test_floor_tier_names_the_first_tier(machine, codex, ollama, sub, tier):
    from llm_router import router

    cfg = machine(codex=codex, ollama=ollama, subscription=sub)
    await _chain(TaskType.RESEARCH, RoutingProfile.PREMIUM, cfg)
    assert router._FLOOR_TIERS[("premium", "research")] == tier


@pytest.mark.asyncio
async def test_proxy_chain_builder_opts_out_of_the_floor(machine, monkeypatch):
    from llm_router import router
    from llm_router.proxy import backends

    seen: list = []

    async def _spy(*a, **k):
        seen.append(k)
        return []

    machine(codex=True, ollama=True, subscription=True)
    monkeypatch.setattr(router, "_build_and_filter_chain", _spy)
    backends._chain_cache.clear()
    await backends.policy_chain("explain why the sky is blue")
    assert seen and seen[-1].get("last_resort_floor") is False, seen
    backends._chain_cache.clear()


# -- end to end: the floor answers, the ledger names the real profile --

async def _research(prompt, calls):
    from llm_router import router
    from llm_router.tools import consolidated

    def fake_call_llm(model, messages, **kw):
        calls.append(model)
        return LLMResponse(content="Local answer about the topic.", model=model, input_tokens=11,
                           output_tokens=7, cost_usd=0.0, latency_ms=5.0, provider=model.split("/", 1)[0])

    tok = call_identity.bind("toolu_f26")
    try:
        with patch("llm_router.router.providers.call_llm", new_callable=AsyncMock, side_effect=fake_call_llm), \
             patch("llm_router.quality_feedback.should_skip_model", return_value=False):
            out = await consolidated.llm(prompt, _Ctx(), task="research")
            await router.drain_bg_tasks(3.0)
    finally:
        call_identity.reset(tok)
    return out


@pytest.mark.asyncio
async def test_long_research_prompt_on_the_incident_machine_is_answered_by_the_floor(machine, monkeypatch):
    """D-179: >2000 chars resolves to premium; premium/research is empty on a subscription machine without
    Perplexity and Ollama is not injected for premium. Before F26 that was REFUSED
    (refused_no_research_provider). Now the floor answers, local first, and the row names the real profile."""
    import sqlite3

    from llm_router import router

    machine(codex=False, ollama=True, subscription=True)
    resolved: list = []
    real = router._resolve_profile

    def spy(*a, **k):
        out = real(*a, **k)
        resolved.append(out[0])
        return out

    monkeypatch.setattr(router, "_resolve_profile", spy)
    prompt = "Compare the 2026 EU AI Act guidance across member states and summarise the differences. " * 30
    assert len(prompt) > 2000
    calls: list = []
    out = await _research(prompt, calls)

    assert calls == [f"ollama/{LOCAL}"], (calls, out)
    assert not out.startswith("[llm_router] research refused"), out
    assert "UNVERIFIED — not web-grounded" in out
    assert resolved == [RoutingProfile.PREMIUM], resolved  # the chain that used to be empty
    con = sqlite3.connect(str(machine.db))
    try:
        rows = con.execute("SELECT profile, final_model, reason_code FROM routing_decisions "
                           "WHERE task_type='research'").fetchall()
    finally:
        con.close()
    assert rows and rows[-1][:2] == ("premium", f"ollama/{LOCAL}") and rows[-1][2] != "error_exception", rows


@pytest.mark.asyncio
async def test_nothing_reachable_still_raises_naming_the_chain(machine):
    from llm_router import router

    machine(codex=False, ollama=False, subscription=False)
    with pytest.raises(router.NoRoutableModelsError) as ei:
        await router.route_and_call(TaskType.ANALYZE, "analyse x", profile=RoutingProfile.PREMIUM)
    assert (ei.value.task_type, ei.value.profile) == (TaskType.ANALYZE, RoutingProfile.PREMIUM)
    assert "analyze/premium" in str(ei.value)
