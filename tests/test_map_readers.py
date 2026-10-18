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


def make_map(*rows, age_s=0.0, policy="local-first"):
    resources = [
        {"resource": cid, "kind": kind, "chain_id": cid, "status": status,
         "effective_priority": prio, "capabilities": ["generate"], "quota": {}}
        for cid, kind, status, prio in rows
    ]
    gen = time.time() - age_s
    return {"schema": rm.SCHEMA, "policy": policy, "resources": resources,
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


def test_balanced_policy_filters_but_never_reorders():
    """Under balanced the ranks are row order only; reordering would flip cheap-first
    to paid-first. Same ranks under local-first do reorder (previous test)."""
    rows = [(LOCAL, "local", "connected", 3), ("gemini/api", "api", "connected", 1),
            ("openai/api", "api", "absent", None)]
    put(make_map(*rows, policy="balanced"))
    assert map_reader.apply_to_chain(BASE) == [LOCAL, GEM]      # dropped, order kept
    put(make_map(*rows, policy="no-such-policy"))
    assert map_reader.apply_to_chain(BASE) == [LOCAL, GEM]


def test_unmapped_provider_untouched_and_keeps_its_slot():
    put(make_map((LOCAL, "local", "connected", 2), ("gemini/api", "api", "connected", 1)))
    chain = [LOCAL, "mistral/m", GEM]
    assert map_reader.apply_to_chain(chain) == [GEM, "mistral/m", LOCAL]


def test_all_disconnected_gives_empty_chain_but_unmapped_keep_their_slots():
    put(make_map((LOCAL, "local", "absent", None), ("gemini/api", "api", "absent", None),
                 ("openai/api", "api", "absent", None)))
    assert map_reader.apply_to_chain(BASE) == []
    assert map_reader.apply_to_chain(BASE + ["mistral/m"]) == ["mistral/m"]
    assert map_reader.filter_providers({"ollama", "gemini", "openai"}) == set()


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
    monkeypatch.setattr(hook_cb, "_ollama_models", lambda *a, **k: ([ModelSpec("ollama", "qwen3:8b")], []))
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setattr(backends, "_chain_cache", {})
    from llm_router.config import RouterConfig
    monkeypatch.setattr(RouterConfig, "all_ollama_models", lambda self: [LOCAL])
    monkeypatch.setenv("OLLAMA_BUDGET_MODELS", "qwen3:8b")   # P0.15: configured, so the promote gate keeps it in place

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


# ── the two measured scenarios: cheap-first survives a balanced map ──────────

CLAUDE = "anthropic/claude-sonnet-4-6"
HAIKU = "anthropic/claude-haiku-4-5-20251001"


def seat_rows():
    # the map's real row order: claude seat first, then local
    return [("anthropic/claude-subscription", "subscription", "connected", 1),
            (LOCAL, "local", "connected", 2), ("ollama/qwen3:32b", "local", "connected", 3),
            ("openai/api", "api", "connected", 4)]


@pytest.mark.parametrize("chain", [
    [LOCAL, "ollama/qwen3:32b", "openai/gpt-4o", CLAUDE, HAIKU],   # code/BALANCED/MODERATE
    [LOCAL, "ollama/qwen3:32b", HAIKU],                            # code/BUDGET/SIMPLE
])
def test_balanced_map_keeps_cheap_first_local_first_reorders(chain):
    put(make_map(*seat_rows(), policy="balanced"))
    assert map_reader.apply_to_chain(chain) == chain
    put(make_map(*seat_rows(), policy="local-first"))
    out = map_reader.apply_to_chain(chain)
    assert sorted(out) == sorted(chain)
    assert out[0].startswith("anthropic/"), "local-first map orders by its ranks (claude seat ranked 1)"


# ── stale or deleted map must stop steering a long-lived process ─────────────

@pytest.fixture
def clock(monkeypatch):
    offset = {"s": 0.0}
    real = rm.age_seconds
    monkeypatch.setattr(rm, "age_seconds", lambda d, now=None: real(d, now) + offset["s"])
    return offset


async def test_proxy_chain_returns_to_no_map_when_map_ages_out_or_is_deleted(three_paths, clock):
    _, _, proxy_chain = three_paths
    base = await proxy_chain()
    put(make_map((LOCAL, "local", "absent", None), ("gemini/api", "api", "connected", 1),
                 ("openai/api", "api", "connected", 2)))
    steered = await proxy_chain()
    assert steered != base and not any(m.startswith("ollama/") for m in steered)
    clock["s"] = map_reader.MAX_AGE_S + 1                     # (a) ages past MAX_AGE_S
    assert await proxy_chain() == base
    clock["s"] = 0
    assert await proxy_chain() == steered                      # fresh again
    os.unlink(rm.map_path())                                   # (b) deleted after a build
    assert await proxy_chain() == base


async def test_proxy_chain_cache_is_cleared_on_token_change(three_paths):
    from llm_router.proxy import backends

    _, _, proxy_chain = three_paths
    await proxy_chain()
    for i in range(5):
        put(make_map((LOCAL, "local", "connected", 1), ("gemini/api", "api", "connected", 2),
                     ("openai/api", "api", "connected", 3)))
        os.utime(rm.map_path(), ns=(1, 1_000_000_000 * (i + 10)))   # distinct mtime each time
        await proxy_chain()
    assert len(backends._chain_cache) == 1, backends._chain_cache.keys()


def test_dynamic_table_returns_to_no_map_when_map_ages_out_or_is_deleted(monkeypatch, clock):
    import llm_router.dynamic_routing as dr

    built = []
    monkeypatch.setattr(dr, "_get_available_providers_fast", lambda: {"ollama", "gemini"})
    monkeypatch.setattr(dr, "build_dynamic_routing_table",
                        lambda provs: built.append(set(provs)) or {})
    full = {"ollama", "gemini"}
    put(make_map((LOCAL, "local", "absent", None), ("gemini/api", "api", "connected", 1)))
    dr.reset_dynamic_routing()
    try:
        dr.initialize_dynamic_routing()
        assert built == [{"gemini"}]
        clock["s"] = map_reader.MAX_AGE_S + 1
        dr.get_dynamic_model_chain(RoutingProfile.BALANCED, TaskType.QUERY)
        assert built == [{"gemini"}, full]                      # (a) aged out: unfiltered again
        clock["s"] = 0
        dr.get_dynamic_model_chain(RoutingProfile.BALANCED, TaskType.QUERY)
        assert built[-1] == {"gemini"}
        os.unlink(rm.map_path())
        dr.get_dynamic_model_chain(RoutingProfile.BALANCED, TaskType.QUERY)
        assert built[-1] == full                                # (b) deleted
    finally:
        dr.reset_dynamic_routing()


def test_dynamic_rebuild_failure_is_retried(monkeypatch):
    import llm_router.dynamic_routing as dr

    monkeypatch.setattr(dr, "_get_available_providers_fast", lambda: {"ollama"})
    monkeypatch.setattr(dr, "build_dynamic_routing_table", lambda provs: {})
    dr.reset_dynamic_routing()
    try:
        dr.initialize_dynamic_routing()
        put(all_connected())
        boom = {"on": True}
        ok = lambda provs: {}                                   # noqa: E731

        def flaky(provs):
            if boom["on"]:
                raise RuntimeError("discovery down")
            return ok(provs)
        monkeypatch.setattr(dr, "build_dynamic_routing_table", flaky)
        dr.get_dynamic_model_chain(RoutingProfile.BALANCED, TaskType.QUERY)   # fails
        assert dr._built_map_token is None, "token must not advance on a failed build"
        boom["on"] = False
        dr.get_dynamic_model_chain(RoutingProfile.BALANCED, TaskType.QUERY)   # retried
        assert dr._built_map_token == map_reader.token() is not None
    finally:
        dr.reset_dynamic_routing()


def test_hook_chain_builder_survives_a_missing_map_reader(monkeypatch):
    import sys

    from llm_router.hooks import chain_builder as hook_cb
    from llm_router.hooks.direct_executor import ModelSpec

    monkeypatch.setattr(hook_cb, "_ollama_models", lambda *a, **k: ([ModelSpec("ollama", "qwen3:8b")], []))
    monkeypatch.setitem(sys.modules, "llm_router.map_reader", None)   # import raises
    monkeypatch.delattr("llm_router.map_reader", raising=False)
    chain = hook_cb.build_chain("simple", "green", "code")
    assert chain and f"{chain[0].provider}/{chain[0].model}" == LOCAL


# ── every map-known provider disconnected: no external route, host answers ───

def all_gone():
    return make_map((LOCAL, "local", "absent", None), ("ollama", "local", "absent", None),
                    ("gemini/api", "api", "absent", None), ("openai/api", "api", "absent", None))


async def test_all_disconnected_every_door_has_no_route_and_nothing_is_called(three_paths, monkeypatch):
    from llm_router import providers
    from llm_router.hooks import direct_executor
    from llm_router.proxy import backends
    from llm_router.router import route_and_call

    router_chain, hook_chain, proxy_chain = three_paths
    called = []

    async def spy(*a, **k):
        called.append(a)
        raise AssertionError("a disconnected provider was tried")
    monkeypatch.setattr(providers, "call_llm", spy)

    put(all_gone())
    assert await router_chain() == []                 # MCP: route_and_call raises "No providers"
    with pytest.raises(ValueError, match="No providers available"):
        await route_and_call(TaskType.CODE, "write a python function that reverses a list")
    assert hook_chain() == []                         # hook: execute_chain([]) -> None -> host answers
    from llm_router.hooks.chain_builder import build_chain
    assert direct_executor.execute_chain("hi", build_chain("simple", "green", "code"), "code") is None
    assert await proxy_chain() == []                  # proxy: model None -> forwarded upstream
    choice = await backends.choose_model("write a python function that reverses a list", None)
    assert choice["model"] is None and choice["chain_head"] == []
    assert not called


async def test_all_disconnected_stale_or_missing_map_keeps_todays_chains(three_paths):
    router_chain, hook_chain, proxy_chain = three_paths
    base = (await router_chain(), hook_chain(), await proxy_chain())
    assert all(base)
    data = all_gone()
    data["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S+00:00",
                                         time.gmtime(time.time() - map_reader.MAX_AGE_S * 2))
    put(data)
    assert (await router_chain(), hook_chain(), await proxy_chain()) == base


def test_all_disconnected_dynamic_table_has_no_disconnected_provider(monkeypatch):
    import llm_router.dynamic_routing as dr

    monkeypatch.setattr(dr, "_get_quota_pressure", lambda: {})
    # D-205: the bundled policy names no local model, so the static table holds an Ollama
    # entry only when the user has one. Give every chain the user's own model (as a
    # discovered/set local model resolves into it) so "ollama" is really in play.
    monkeypatch.setattr(dr, "ROUTING_TABLE", {k: ["ollama/usermodel:1b", *c] for k, c in dr.ROUTING_TABLE.items()})
    put(all_gone())
    provs = map_reader.filter_providers({"ollama", "gemini", "openai"})
    table = dr.build_dynamic_routing_table(provs)
    seen = {m.split("/")[0] for chain in table.values() for m in chain}
    assert not seen & {"ollama", "gemini", "openai"}, seen
    assert table, "empty table would pass trivially"
    # with no map the always-allowed local tiers are present again
    os.unlink(rm.map_path())
    seen = {m.split("/")[0] for chain in dr.build_dynamic_routing_table(set()).values() for m in chain}
    assert "ollama" in seen


def test_available_tiers_all_disconnected(monkeypatch):
    import llm_router.dynamic_routing as dr

    prof = {"routing": {"model_priority": [{"tier": "free_local", "models": ["a"]},
                                           {"tier": "cheap_apis", "models": ["b"]}]}}
    monkeypatch.setattr(dr, "_load_user_profile", lambda: prof)
    put(all_gone())
    assert dr._detect_available_tiers() == {"free_local": False, "cheap_apis": False}


@pytest.mark.parametrize("policy", ["balanced", "local-first"])
async def test_subscription_local_with_nothing_connected_returns_empty(policy, monkeypatch):
    import llm_router.subscription_local_routing as slr
    from llm_router import chain_builder

    monkeypatch.setattr(slr, "is_subscription_local_active", lambda profile: True)
    put(make_map((LOCAL, "local", "absent", None), ("gemini/api", "api", "absent", None),
                 ("openai/api", "api", "absent", None), policy=policy))
    assert await chain_builder._apply_subscription_local(BASE, "moderate", RoutingProfile.BALANCED) == []
    os.unlink(rm.map_path())
    assert await chain_builder._apply_subscription_local([], "moderate", RoutingProfile.BALANCED) == []


def test_token_is_a_tuple_so_other_state_can_join_the_cache_key():
    assert map_reader.token() is None
    put(all_connected())
    tok = map_reader.token()
    assert isinstance(tok, tuple) and len(tok) == 1
