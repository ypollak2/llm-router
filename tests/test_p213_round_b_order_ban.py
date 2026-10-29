"""P2.13-a round B (UN-01): stored `prefer` order and ban rules steer the chains.

Doors: MCP/router (`_build_and_filter_chain`), hook (`hooks.chain_builder.build_chain`),
proxy non-Claude (`proxy.backends.policy_chain` / `choose_model`). The rules are stored
through the real grammar + store in a tmp `routing.yaml` (LLM_ROUTER_HOME is a tmp dir).
"""
from __future__ import annotations

import pytest

from llm_router import map_reader, prefer_grammar, user_rules
from llm_router.types import Complexity, RoutingProfile, TaskType

GEM, OAI, LOCAL = "gemini/gemini-2.5-flash", "openai/gpt-4o-mini", "ollama/qwen3:8b"
BASE = [LOCAL, GEM, OAI]
PROMPT = "write a python function that reverses a list"


def store(*sentences: str) -> None:
    for s in sentences:
        parsed = prefer_grammar.parse(s)
        assert parsed.refusal is None, parsed.refusal
        user_rules.add_rules(parsed.rules, s)


def provs(chain):
    return [m.split("/")[0] for m in chain]


@pytest.fixture
def doors(monkeypatch):
    import llm_router.dynamic_routing as dr
    from llm_router.config import RouterConfig
    from llm_router.hooks import chain_builder as hook_cb
    from llm_router.hooks.direct_executor import ModelSpec
    from llm_router.proxy import backends

    monkeypatch.setattr(dr, "get_dynamic_model_chain", lambda profile, task: list(BASE))
    monkeypatch.setattr(hook_cb, "_ollama_models", lambda *a, **k: ([ModelSpec("ollama", "qwen3:8b")], []))
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setattr(backends, "_chain_cache", {})
    monkeypatch.setattr(RouterConfig, "all_ollama_models", lambda self: [LOCAL])
    monkeypatch.setenv("OLLAMA_BUDGET_MODELS", "qwen3:8b")

    async def router_chain():
        from llm_router.config import get_config
        from llm_router.router import _build_and_filter_chain
        return await _build_and_filter_chain(
            TaskType.CODE, RoutingProfile.BALANCED, None, "simple", Complexity.SIMPLE, get_config())

    def hook_chain():
        return [f"{m.provider}/{m.model}" for m in hook_cb.build_chain("simple", "green", "code")]

    async def proxy_chain():
        return (await backends.policy_chain(PROMPT))[2]

    return router_chain, hook_chain, proxy_chain


async def test_order_rule_changes_the_chain_head_at_every_door(doors):
    router_chain, hook_chain, proxy_chain = doors
    base = [await router_chain(), hook_chain(), await proxy_chain()]
    assert all(provs(c)[0] == "ollama" and "openai" in provs(c) for c in base), base
    store("openai before ollama")
    after = [await router_chain(), hook_chain(), await proxy_chain()]
    assert [provs(c)[0] for c in after] == ["openai"] * 3, after
    assert all(sorted(a) == sorted(b) for a, b in zip(after, base))   # reorder only, nothing lost


async def test_ban_rule_removes_the_provider_at_every_door(doors):
    router_chain, hook_chain, proxy_chain = doors
    store("no gemini")
    for chain in (await router_chain(), hook_chain(), await proxy_chain()):
        assert "gemini" not in provs(chain), chain
        assert "ollama" in provs(chain) and "openai" in provs(chain), chain


async def test_proxy_choose_model_follows_the_rules(doors):
    from llm_router.proxy import backends

    before = await backends.choose_model(PROMPT, None)
    store("no openai")
    after = await backends.choose_model(PROMPT, None)
    assert "openai" in provs(before["chain_head"]), before
    assert "openai" not in provs(after["chain_head"]), after
    assert after["model"] is None or not after["model"].startswith("openai/")


async def test_proxy_chain_cache_rebuilds_when_only_the_rules_change(doors):
    """Same map (none), same prompt: a new rule must not be served the old cached chain."""
    _, _, proxy_chain = doors
    assert map_reader.token() is None
    first = await proxy_chain()
    store("no gemini")
    assert map_reader.token() is None
    second = await proxy_chain()
    assert "gemini" in provs(first) and "gemini" not in provs(second)
    user_rules.write_rules([])
    assert await proxy_chain() == first          # rule removed: back to the unruled chain


async def test_removing_a_rule_restores_the_chain_at_the_router_door(doors):
    router_chain, _, _ = doors
    base = await router_chain()
    store("no gemini")
    assert "gemini" not in provs(await router_chain())
    user_rules.write_rules([])
    assert await router_chain() == base


def test_user_order_beats_the_map_rank(doors):
    """A fresh map ranks local first (D-125: the user's order still wins)."""
    import time

    from llm_router import resource_map as rm
    rows = [{"resource": c, "kind": k, "chain_id": c, "status": "connected", "effective_priority": p,
             "capabilities": ["generate"], "quota": {}}
            for c, k, p in ((LOCAL, "local", 1), ("gemini/api", "api", 2), ("openai/api", "api", 3))]
    rm.write_map({"schema": rm.SCHEMA, "policy": "local-first", "resources": rows,
                  "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())})
    assert map_reader.apply_to_chain(BASE)[0] == LOCAL
    store("openai before ollama")
    assert map_reader.apply_to_chain(BASE)[0] == OAI


def test_pinned_call_keeps_its_order_but_bans_still_apply():
    store("openai before ollama", "no gemini")
    assert map_reader.apply_to_chain(BASE, reorder=False) == [LOCAL, OAI]
    assert map_reader.apply_to_chain(BASE) == [OAI, LOCAL]


def test_model_glob_ban_and_chained_order():
    chain = ["ollama/q", "codex/gpt", "anthropic/claude-opus-4-5", "anthropic/claude-sonnet-4-5"]
    store("never claude opus")
    assert map_reader.apply_to_chain(chain) == ["ollama/q", "codex/gpt", "anthropic/claude-sonnet-4-5"]
    store("codex before claude")
    assert map_reader.apply_to_chain(["anthropic/claude-sonnet-4-5", "ollama/q", "codex/gpt"]) \
        == ["codex/gpt", "anthropic/claude-sonnet-4-5", "ollama/q"]


def test_chained_order_rules_settle():
    rules = [{"kind": "order", "first": {"providers": ["codex"], "model": None},
              "then": {"providers": ["anthropic"], "model": None}},
             {"kind": "order", "first": {"providers": ["ollama"], "model": None},
              "then": {"providers": ["codex"], "model": None}}]
    out = user_rules.apply_rules(["anthropic/a", "codex/c", "ollama/o"], lambda m: m, rules)
    assert out == ["ollama/o", "codex/c", "anthropic/a"]


def test_damaged_rules_file_is_fail_open(monkeypatch):
    user_rules.routing_path().parent.mkdir(parents=True, exist_ok=True)
    user_rules.routing_path().write_text(user_rules.MARK_BEGIN + "\npreferences: [oops\n" + user_rules.MARK_END + "\n")
    assert map_reader.apply_to_chain(BASE) == BASE


def _put_local_first_map():
    """A fresh local-first map built the way the writer builds it (``_prioritise``), so any
    rule baked into ``effective_priority`` at build time would show up in the file."""
    import time

    from llm_router import resource_map as rm
    rows = [{"resource": c, "kind": k, "chain_id": c, "status": "connected", "capabilities": ["generate"],
             "quota": {}} for c, k in ((LOCAL, "local"), ("gemini/api", "api"), ("openai/api", "api"))]
    rm._prioritise(rows, "local-first", 0.0)
    rm.write_map({"schema": rm.SCHEMA, "policy": "local-first", "resources": rows,
                  "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())})


def test_rule_removal_under_a_non_balanced_fresh_map_restores_the_order():
    store("openai before ollama")
    _put_local_first_map()                          # map built WHILE the rule exists
    assert map_reader.apply_to_chain(BASE)[0] == OAI
    user_rules.write_rules([])                      # map file NOT rebuilt
    assert map_reader.apply_to_chain(BASE) == [LOCAL, GEM, OAI]


async def test_ban_that_empties_the_chain_gives_the_no_route_result(doors):
    router_chain, hook_chain, proxy_chain = doors
    store("no ollama", "no gemini", "no openai")
    assert await router_chain() == []
    assert await proxy_chain() == []
    assert hook_chain() == []


def test_ban_rule_with_missing_keys_is_fail_open_and_recorded(monkeypatch):
    from llm_router import failopen

    seen = []
    monkeypatch.setattr(failopen, "record", lambda code, exc=None, **kw: seen.append(code))
    user_rules.write_rules([{"id": "p1", "kind": "ban", "text": "hand edited"}])   # no "target"
    assert map_reader.apply_to_chain(BASE) == BASE
    assert "CHZ-FO-USER-RULES-READ" in seen


@pytest.mark.xfail(strict=True, reason="proxy/tiers.py — owned by the lane-A/tiers session: a Claude-model "
                   "ban or cap at the proxy door is a tier decision made there, which this PR does not touch")
def test_proxy_tier_decision_honours_a_claude_ban():
    from llm_router.proxy import tiers

    policy = tiers.ClaudeTierPolicy.load(None)
    opus = next(t for t in policy.by_name.values() if "opus" in (t.model or ""))
    assert tiers.tier_model(opus.name) == opus.model     # control: the tier resolves with no rule
    store("never claude opus")
    assert tiers.tier_model(opus.name) is None           # a banned model must not be served as a tier
