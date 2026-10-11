"""P1.8 task 6: the routing readers (router, hook, proxy, dynamic table) follow the map.

Gate P1.8-a: a map mutation changes the MCP, hook and proxy chains (3/3); a
missing or stale map file leaves today's chain untouched.
"""
from __future__ import annotations

import json
import os
import time

import pytest

from llm_router import map_reader, resource_map as rm
from llm_router.types import Complexity, RoutingProfile, TaskType

GEM, OAI, LOCAL = "gemini/gemini-2.5-flash", "openai/gpt-4o-mini", "ollama/qwen3:8b"
BASE = [LOCAL, GEM, OAI]


def make_map(*rows, age_s=0.0):
    resources = [
        {"resource": cid, "kind": kind, "chain_id": cid, "status": status,
         "effective_priority": prio, "capabilities": ["generate"], "quota": {}}
        for cid, kind, status, prio in rows
    ]
    gen = time.time() - age_s
    return {"schema": rm.SCHEMA, "policy": "balanced", "resources": resources,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(gen))}


def put(data):
    rm.write_map(data)
    # same-size rewrites inside one mtime tick must still look changed
    p = rm.map_path()
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))


def all_connected():
    return make_map((LOCAL, "local", "connected", 1),
                    ("gemini/api", "api", "connected", 2),
                    ("openai/api", "api", "connected", 3))


# ── the reader itself ────────────────────────────────────────────────────────

def test_no_map_file_is_a_noop():
    assert not rm.map_path().exists()
    assert map_reader.load_view() is None
    assert map_reader.apply_to_chain(BASE) == BASE
    assert map_reader.token() is None


@pytest.mark.parametrize("bad", ["not json", json.dumps({"schema": 99, "resources": []}),
                                 json.dumps({"schema": 1}), json.dumps([1, 2]),
                                 json.dumps({"schema": 1, "generated_at": "x", "resources": [{}]})])
def test_garbage_or_wrong_shape_map_is_a_noop(bad):
    rm.map_path().parent.mkdir(parents=True, exist_ok=True)
    rm.map_path().write_text(bad)
    assert map_reader.apply_to_chain(BASE) == BASE


def test_stale_map_is_a_noop():
    put(make_map((LOCAL, "local", "absent", None), age_s=map_reader.MAX_AGE_S + 60))
    assert map_reader.apply_to_chain(BASE) == BASE
    put(make_map((LOCAL, "local", "absent", None), age_s=60))
    assert map_reader.apply_to_chain(BASE) == [GEM, OAI]


def test_drops_non_connected_and_orders_by_priority():
    put(make_map((LOCAL, "local", "connected", 3),
                 ("gemini/api", "api", "connected", 1),
                 ("openai/api", "api", "absent", None)))
    assert map_reader.apply_to_chain(BASE) == [GEM, LOCAL]


def test_unmapped_provider_untouched_and_keeps_its_slot():
    put(make_map((LOCAL, "local", "connected", 2), ("gemini/api", "api", "connected", 1)))
    chain = [LOCAL, "mistral/m", GEM]
    assert map_reader.apply_to_chain(chain) == [GEM, "mistral/m", LOCAL]


def test_never_empties_a_chain():
    put(make_map((LOCAL, "local", "absent", None), ("gemini/api", "api", "absent", None),
                 ("openai/api", "api", "absent", None)))
    assert map_reader.apply_to_chain(BASE) == BASE


def test_rereads_on_mtime_change_only(monkeypatch):
    put(all_connected())
    calls = []
    real = rm.load_map
    monkeypatch.setattr(rm, "load_map", lambda p=None: calls.append(1) or real(p))
    for _ in range(3):
        assert map_reader.load_view() is not None
    assert len(calls) == 1
    put(make_map((LOCAL, "local", "absent", None), ("gemini/api", "api", "connected", 1)))
    assert map_reader.apply_to_chain(BASE) == [GEM, OAI]
    assert len(calls) == 2


# ── gate P1.8-a: one mutation, three chains ──────────────────────────────────

@pytest.fixture
def three_paths(monkeypatch):
    """The router, hook and proxy chains over one fixed set of candidates."""
    import llm_router.dynamic_routing as dr
    from llm_router.hooks import chain_builder as hook_cb
    from llm_router.hooks.direct_executor import ModelSpec
    from llm_router.proxy import backends

    monkeypatch.setattr(dr, "get_dynamic_model_chain", lambda profile, task: list(BASE))
    monkeypatch.setattr(hook_cb, "_ollama_models", lambda: [ModelSpec("ollama", "qwen3:8b")])
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setattr(backends, "_chain_cache", {})
    from llm_router.config import RouterConfig
    monkeypatch.setattr(RouterConfig, "all_ollama_models", lambda self: [LOCAL])

    async def router_chain():
        from llm_router.config import get_config
        from llm_router.router import _build_and_filter_chain
        return await _build_and_filter_chain(
            TaskType.CODE, RoutingProfile.BALANCED, None, "simple", Complexity.SIMPLE, get_config())

    def hook_chain():
        return [f"{m.provider}/{m.model}" for m in hook_cb.build_chain("simple", "green", "code")]

    async def proxy_chain():
        return (await backends.policy_chain("write a python function that reverses a list"))[2]

    return router_chain, hook_chain, proxy_chain


def provs(chain):
    return [m.split("/")[0] for m in chain]


async def test_map_mutation_changes_mcp_hook_and_proxy_chains(three_paths):
    router_chain, hook_chain, proxy_chain = three_paths

    async def snap():
        return provs(await router_chain()), provs(hook_chain()), provs(await proxy_chain())

    before = await snap()                                  # no map file: today's chains
    assert all("ollama" in c and "gemini" in c and "openai" in c for c in before), before

    put(make_map((LOCAL, "local", "absent", None), ("gemini/api", "api", "connected", 1),
                  ("openai/api", "api", "connected", 2)))          # mutation 1: local gone
    after = await snap()
    assert all("ollama" not in c for c in after), after
    assert all(c != b for c, b in zip(after, before)), (before, after)   # 3/3 changed

    put(make_map((LOCAL, "local", "connected", 3), ("gemini/api", "api", "connected", 2),
                 ("openai/api", "api", "connected", 1)))           # mutation 2: priority flips
    flipped = await snap()
    assert [c[0] for c in flipped] == ["openai"] * 3, flipped

    os.unlink(rm.map_path())                                         # map removed: back to today
    assert await snap() == before


async def test_missing_and_stale_map_leave_all_three_chains_as_today(three_paths):
    router_chain, hook_chain, proxy_chain = three_paths
    base = (await router_chain(), hook_chain(), await proxy_chain())
    rm.map_path().parent.mkdir(parents=True, exist_ok=True)
    rm.map_path().write_text("{ truncated")
    assert (await router_chain(), hook_chain(), await proxy_chain()) == base
    put(make_map((LOCAL, "local", "absent", None), age_s=map_reader.MAX_AGE_S * 2))
    assert (await router_chain(), hook_chain(), await proxy_chain()) == base
    assert all(base), "empty chains would pass trivially"


# ── dynamic routing: tiers and MCP re-read ──────────────────────────────────

def test_available_tiers_follow_the_map(monkeypatch):
    import llm_router.dynamic_routing as dr

    prof = {"routing": {"model_priority": [
        {"tier": "free_local", "models": ["a"]}, {"tier": "cheap_apis", "models": ["b"]}]}}
    monkeypatch.setattr(dr, "_load_user_profile", lambda: prof)
    assert dr._detect_available_tiers() == {"free_local": True, "cheap_apis": True}
    put(make_map((LOCAL, "local", "absent", None), ("gemini/api", "api", "connected", 1)))
    assert dr._detect_available_tiers() == {"free_local": False, "cheap_apis": True}


def test_dynamic_table_rebuilds_when_map_mtime_changes(monkeypatch):
    import llm_router.dynamic_routing as dr

    built = []
    monkeypatch.setattr(dr, "_get_available_providers_fast", lambda: {"ollama", "gemini"})
    monkeypatch.setattr(dr, "build_dynamic_routing_table",
                        lambda provs: built.append(set(provs)) or {})
    dr.reset_dynamic_routing()
    try:
        dr.initialize_dynamic_routing()
        dr.get_dynamic_model_chain(RoutingProfile.BALANCED, TaskType.QUERY)
        assert built == [{"ollama", "gemini"}]               # no map: built once, not again
        put(make_map((LOCAL, "local", "absent", None), ("gemini/api", "api", "connected", 1)))
        dr.get_dynamic_model_chain(RoutingProfile.BALANCED, TaskType.QUERY)
        dr.get_dynamic_model_chain(RoutingProfile.BALANCED, TaskType.QUERY)
        assert built == [{"ollama", "gemini"}, {"gemini"}]   # re-read once on the mtime change
    finally:
        dr.reset_dynamic_routing()
