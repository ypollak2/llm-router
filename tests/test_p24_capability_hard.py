"""P2.4-a: the capability filter is hard at four doors (tools, vision, window), default on.

12 door cases = {router (MCP), hook + SDK, proxy, gateway} x {tools, vision, window}, each
over one mixed chain (see CHAIN). For each case the models the door ACTUALLY dispatches or
attempts are compared with a literal expected list: every model known to lack the capability
is absent, every capable model stays in its original relative order, an unknown-capability
model stays, and with ``LLM_ROUTER_CAPABILITY_ROUTING=0`` the chain is unchanged.

Mutants F1, F2, F3, F5, F7 are monkeypatch negative controls: each makes a named test fail
(a helper counts violations; it is 0 normally and > 0 under the mutant).
"""
from __future__ import annotations

import asyncio
import os
import time
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from llm_router import capability_filter as cf
from llm_router import failopen, map_reader, model_registry, resource_map as rm
from llm_router.capability_filter import Needs
from llm_router.hooks.direct_executor import ModelSpec
from llm_router.lineage import Tier
from llm_router.model_registry import ModelMetadata, ModelRegistry
from llm_router.repo_config import RepoConfig
from llm_router.types import Complexity, LLMResponse, RoutingProfile, TaskType
from tests import test_tq007_daily_cap_downgrade as tq

TEXTONLY = "ollama/textonly:1"     # map: caps [generate], num_ctx 4096
TOOLY = "ollama/tooly:1"           # map: tools, no vision, max_context 32768
CODEX = "codex/gpt-5.5"            # seat: tools, no vision
PERPLEXITY = "perplexity/sonar"    # registry: ("web-grounded", "citations"): known to lack tools and vision
OPENAI = "openai/gpt-4o-mini"      # registry: function-calling + vision, 128k; map row is API-key: unknown
GEMINI = "gemini_cli/gemini-2.5-pro"   # seat: tools + vision
CLAUDE = "anthropic/claude-sonnet-4-6"  # seat: tools + vision
MYSTERY = "mystery/model-x"        # nothing known anywhere
CHAIN = [TEXTONLY, TOOLY, CODEX, PERPLEXITY, OPENAI, GEMINI, CLAUDE, MYSTERY]

TOOLS_PROMPT = "read the file src/app.py and explain what it does"
VISION_PROMPT = "describe what is in this picture [Image #1]"
LONG_PROMPT = "word " * 32000         # 160000 chars: 40000 estimated tokens, no tool or vision trigger

#: What each capability must leave, written out by hand (not derived from the filter).
EXPECTED = {
    "tools": [TOOLY, CODEX, OPENAI, GEMINI, CLAUDE, MYSTERY],
    "vision": [OPENAI, GEMINI, CLAUDE, MYSTERY],
    "window": [CODEX, PERPLEXITY, OPENAI, GEMINI, CLAUDE, MYSTERY],   # 40000 tokens: 4096 and 32768 windows drop
}
EXCLUDED = {cap: [m for m in CHAIN if m not in EXPECTED[cap]] for cap in EXPECTED}
CAPS = tuple(EXPECTED)
DOORS = ("router", "hook", "proxy", "gateway")


# ── world: a fresh map with per-model capabilities, a registry, an empty bandit ──

def _row(resource, kind, chain_id, caps, limits=None):
    return rm._row(resource, kind, chain_id, status="connected", capabilities=list(caps),
                   limits=limits or {}, effective_priority=1)


def map_rows():
    return [
        _row("ollama/textonly:1", "local", TEXTONLY, ["generate"], {"num_ctx": 4096, "max_context": None}),
        _row("ollama/tooly:1", "local", TOOLY, ["generate", "tools"], {"num_ctx": 8192, "max_context": 32768}),
        _row("claude", "subscription", "anthropic/claude-subscription", ["generate", "tools", "vision", "thinking"]),
        _row("codex", "subscription", "codex/codex-subscription", ["generate", "tools"]),
        _row("gemini_cli", "subscription", "gemini_cli/gemini-subscription", ["generate", "tools", "vision"]),
        _row("api/OPENAI_API_KEY", "api", "openai/api", ["generate"]),
    ]


def put_map(rows=None, *, age_s=0.0, schema=None):
    data = {"schema": rm.SCHEMA if schema is None else schema, "policy": "balanced",
            "resources": map_rows() if rows is None else rows,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - age_s))}
    rm.write_map(data)
    p = rm.map_path()
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))


def _registry():
    def meta(id_, caps, window):
        return ModelMetadata(id=id_, provider=id_.split("/")[0], tier=Tier.MID if hasattr(Tier, "MID") else list(Tier)[0],
                             quality_score=0.7, price_per_1m_input_usd=0.1, price_per_1m_output_usd=0.4,
                             context_window=window, capabilities=caps)

    return ModelRegistry.from_models([
        meta("openai/gpt-4o-mini", ("function-calling", "vision", "json"), 128000),
        meta("perplexity/sonar", ("web-grounded", "citations"), 128000),
    ])


@pytest.fixture(autouse=True)
def world(monkeypatch):
    monkeypatch.delenv("LLM_ROUTER_CAPABILITY_ROUTING", raising=False)
    monkeypatch.setenv("LLM_ROUTER_BANDIT", "off")
    monkeypatch.setattr(model_registry, "_DEFAULT_REGISTRY", _registry())
    put_map()


def _kill_switch(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_CAPABILITY_ROUTING", "0")


# ── door 1: router (route_and_call), 4: gateway behind it ────────────────────

def _response(model):
    return LLMResponse(content=f"ok from {model}", model=model, input_tokens=7, output_tokens=3,
                       cost_usd=0.0, latency_ms=15.0, provider=model.split("/", 1)[0])


def route(chain, prompt, *, task=TaskType.CODE, messages=None, system=None, override=None,
          bandit=None, extra=None, via=None):
    """Drive route_and_call over *chain* with the dispatch loop replaced by a recorder.

    Returns the chain handed to ``_dispatch_model_loop`` (what the door would try), or the
    exception raised. ``via`` is a callable taking the route coroutine function, for the HTTP door."""
    captured: list = []
    caps = {}
    repo_cfg = RepoConfig(daily_caps=caps, enforce="hard")
    route_log = MagicMock()
    mock_log = MagicMock()
    mock_log.bind.return_value = route_log
    tracker = MagicMock()
    tracker.is_healthy.return_value = True

    async def dispatch(**kw):
        captured.append(list(kw["models_to_try"]))
        return tq._response(kw["models_to_try"][0])

    with ExitStack() as es:
        p = es.enter_context
        p(patch.dict(os.environ, {"LLM_ROUTER_ENFORCE": "hard"}))
        p(patch("llm_router.router.get_config", return_value=tq._Cfg()))
        p(patch("llm_router.router.get_tracker", return_value=tracker))
        p(patch("llm_router.router.log", mock_log))
        p(patch("llm_router.router._native_notify", lambda *a, **k: None))
        p(patch("llm_router.repo_config.effective_config", return_value=repo_cfg))
        p(patch("llm_router.router.cost.get_monthly_spend", new_callable=AsyncMock, return_value=0.0))
        p(patch("llm_router.router.cost.get_daily_spend", new_callable=AsyncMock, return_value=0.0))
        p(patch("llm_router.router.cost.get_daily_spend_by_task_type", new_callable=AsyncMock, return_value=0.0))
        p(patch("llm_router.policy.load_org_policy", return_value=None))
        p(patch("llm_router.policy.get_active_policy", return_value=None))
        p(patch("llm_router.router.cost.log_usage", new_callable=AsyncMock))
        p(patch("llm_router.router.reserve_envelope", new_callable=AsyncMock, return_value=(None, True, None)))
        p(patch("llm_router.router.commit_envelope", new_callable=AsyncMock))
        p(patch("llm_router.router.release_envelope", new_callable=AsyncMock))
        p(patch("llm_router.semantic_cache.check", new_callable=AsyncMock, return_value=None))
        p(patch("llm_router.semantic_cache.store", new_callable=AsyncMock))
        p(patch("llm_router.router._build_and_filter_chain", new_callable=AsyncMock, return_value=list(chain)))
        p(patch("llm_router.router.sink_unvalidated", lambda c, *a, **k: c))
        p(patch("llm_router.router._dispatch_model_loop", dispatch))
        if bandit is not None:
            p(patch("llm_router.bandit.EpsilonGreedyBandit.reorder", bandit))
            p(patch.dict(os.environ, {"LLM_ROUTER_BANDIT": "on"}))
        for target, value in (extra or {}).items():
            p(patch(target, value))
        from llm_router.router import route_and_call

        async def go():
            return await route_and_call(task, prompt, profile=RoutingProfile.BALANCED,
                                        messages=messages, system_prompt=system)
        try:
            if via is not None:
                via()
            else:
                asyncio.run(go())
        except Exception as exc:  # noqa: BLE001 - the caller asserts on it
            return exc
    assert len(captured) == 1, captured
    return captured[0]


def _spec_for_cap(cap):
    if cap == "tools":
        return dict(prompt=TOOLS_PROMPT)
    if cap == "vision":
        return dict(prompt="describe the picture", messages=[
            {"role": "user", "content": [{"type": "text", "text": "look"},
                                         {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}]}])
    return dict(prompt=LONG_PROMPT)


def door_router(cap):
    return route(CHAIN, **_spec_for_cap(cap))


def door_gateway(cap):
    """The gateway door: HTTP request in, dispatch chain out. Tools and window ride the HTTP
    text path; vision rides route_and_call(messages=) because the gateway refuses image parts."""
    if cap == "vision":
        return route(CHAIN, **_spec_for_cap(cap))
    pytest.importorskip("fastapi.testclient")
    from fastapi.testclient import TestClient

    from llm_router.gateway import app

    client = TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    prompt = TOOLS_PROMPT if cap == "tools" else LONG_PROMPT
    def via():
        r = client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": prompt}], "task_type": "code"})
        assert r.status_code == 200, r.text[:200]

    out = route(CHAIN, "unused", via=via)
    assert not isinstance(out, Exception), out
    return out


# ── door 2: hook + SDK (execute_chain) ───────────────────────────────────────

class HookRig:
    def __init__(self, monkeypatch):
        from llm_router.hooks import direct_executor as de

        self.attempted: list[str] = []
        for name in {m.split("/")[0] for m in CHAIN} | {"openai", "gemini_cli", "codex"}:
            monkeypatch.setitem(de._PROVIDER_CALLS, name, self._call(name))
        monkeypatch.setattr(de, "available_ollama_models", lambda timeout=0.5: None)
        monkeypatch.setattr(de, "ollama_is_alive", lambda timeout=0.5: True)
        monkeypatch.setattr(de, "_paid_budget_exhausted", lambda p: False)
        monkeypatch.setattr(de, "_provider_reset_blocked", lambda p: False)
        monkeypatch.setattr(de, "_okf_inject", lambda prompt, **k: prompt)
        monkeypatch.setattr(de, "_okf_enrich", lambda *a, **k: None)
        monkeypatch.setattr("llm_router.ollama_watchdog.gate", lambda *a, **k: None)

    def _call(self, provider):
        def call(prompt, model, timeout, history, system):
            self.attempted.append(f"{provider}/{model}")
            return None, {}
        return call


def specs(chain):
    return [ModelSpec("claude" if m.startswith("anthropic/") else m.split("/")[0], m.split("/", 1)[1]) for m in chain]


def door_hook(cap, monkeypatch):
    from llm_router.hooks.direct_executor import execute_chain

    rig = HookRig(monkeypatch)
    prompt = {"tools": TOOLS_PROMPT, "vision": VISION_PROMPT, "window": LONG_PROMPT}[cap]
    assert execute_chain(prompt, specs(CHAIN), "code") is None
    return rig.attempted


# ── door 3: proxy (choose_model, then the app) ───────────────────────────────

def _proxy_body(cap):
    from tests.test_proxy import _req

    body = _req()
    if cap == "tools":
        body["tools"] = body.get("tools") or [{"name": "Read", "input_schema": {"type": "object"}}]
    elif cap == "vision":
        body["messages"][0]["content"].append(
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA"}})
    else:
        body["tools"] = []
        last = [m for m in body["messages"] if m["role"] == "user"][-1]
        last["content"] = [{"type": "text", "text": LONG_PROMPT}]
    return body


def door_proxy(cap, monkeypatch):
    from llm_router.proxy import backends

    async def policy_chain(text):
        return "code", "moderate", list(CHAIN)

    monkeypatch.setattr(backends, "policy_chain", policy_chain)
    body = _proxy_body(cap)
    needs = cf.needs_from_anthropic_body(body) if cf.enabled() else None
    choice = asyncio.run(backends.choose_model("x", None, needs=needs))
    return choice


# ── the 12 door cases ────────────────────────────────────────────────────────

def _assert_case(seen, cap, *, attempted_only=None):
    expected = EXPECTED[cap] if attempted_only is None else attempted_only
    assert seen == expected, f"{cap}: dispatched {seen}, expected {expected}"
    for m in EXCLUDED[cap]:
        assert m not in seen, f"{m} lacks {cap} and was dispatched"
    assert len(EXCLUDED[cap]) >= 1, "a case that excludes nothing proves nothing"
    assert MYSTERY in seen, "an unknown-capability model must stay"
    ranks = [CHAIN.index(m) for m in seen]
    assert ranks == sorted(ranks), "survivors changed relative order"


@pytest.mark.parametrize("cap", CAPS)
def test_router_door(cap):
    _assert_case(door_router(cap), cap)


@pytest.mark.parametrize("cap", CAPS)
def test_gateway_door(cap):
    _assert_case(door_gateway(cap), cap)


@pytest.mark.parametrize("cap", CAPS)
def test_hook_door(cap, monkeypatch):
    attempted = door_hook(cap, monkeypatch)
    no_claude = [m for m in EXPECTED[cap] if m != CLAUDE]           # the hook cannot call claude
    _assert_case(attempted, cap, attempted_only=no_claude)


@pytest.mark.parametrize("cap", CAPS)
def test_proxy_door(cap, monkeypatch):
    choice = door_proxy(cap, monkeypatch)
    assert choice["chain_head"] == EXPECTED[cap][:4], choice
    served = next((m for m in EXPECTED[cap] if m.startswith("ollama/")), None)
    assert choice["model"] == served, choice            # the first tool-capable survivor, or None (stay on Claude)
    for m in EXCLUDED[cap]:
        assert m != choice["model"] and m not in choice["chain_head"]
    assert len(EXCLUDED[cap]) >= 1


def test_gateway_http_still_refuses_a_tools_field():
    pytest.importorskip("fastapi.testclient")
    from fastapi.testclient import TestClient

    from llm_router.gateway import _TOOLS_UNSUPPORTED, app

    r = TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False).post(
        "/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}],
                                      "tools": [{"type": "function", "function": {"name": "f"}}]})
    assert r.status_code == 400 and r.json()["detail"] == _TOOLS_UNSUPPORTED


@pytest.mark.parametrize("door", DOORS)
@pytest.mark.parametrize("cap", CAPS)
def test_kill_switch_leaves_every_door_unchanged(door, cap, monkeypatch):
    _kill_switch(monkeypatch)
    assert cf.enabled() is False
    if door == "router":
        assert door_router(cap) == CHAIN
    elif door == "gateway":
        assert door_gateway(cap) == CHAIN
    elif door == "hook":
        assert door_hook(cap, monkeypatch) == [m for m in CHAIN if m != CLAUDE]
    else:
        choice = door_proxy(cap, monkeypatch)
        assert choice["chain_head"] == CHAIN[:4] and choice["model"] == TEXTONLY


def test_sdk_door_attempts_the_same_survivors_as_execute_chain(monkeypatch):
    from llm_router import sdk
    from llm_router.hooks import chain_builder, savings_logger

    rig = HookRig(monkeypatch)
    monkeypatch.setattr(chain_builder, "get_current_pressure", lambda: ("green", 0.0))
    monkeypatch.setattr(chain_builder, "needs_claude_tools", lambda p, t: False)
    monkeypatch.setattr(chain_builder, "build_chain", lambda *a, **k: specs(CHAIN))
    monkeypatch.setattr(savings_logger, "log_direct_to_db", lambda **kw: None)
    monkeypatch.setattr(savings_logger, "log_direct_savings", lambda **kw: None)
    with pytest.raises(sdk.RoutingError):
        sdk.route(VISION_PROMPT, task_type="code", complexity="moderate", timeout=2)
    assert rig.attempted == [m for m in EXPECTED["vision"] if m != CLAUDE]


def test_proxy_app_serves_the_first_capable_model_and_the_kill_switch_restores_the_old_pick(tmp_path, monkeypatch):
    """Through try_serve: a request that carries tools never reaches a model known to lack tools."""
    from llm_router.proxy import backends
    from tests.test_proxy import FakeBackend, Upstream, _ollama, _post, _req, _rows
    from llm_router.proxy import server as ps

    async def policy_chain(text):
        return "code", "moderate", [TEXTONLY, TOOLY]

    monkeypatch.setattr(backends, "policy_chain", policy_chain)

    def serve(sub):
        d = tmp_path / sub
        d.mkdir()
        served = []
        cfg = ps.ProxyConfig(upstream="http://127.0.0.1:9", ledger_path=d / "proxy_calls.jsonl")
        client = httpx.AsyncClient(transport=httpx.MockTransport(Upstream()))
        backend = FakeBackend(_ollama("done"))
        app = ps.build_app(cfg, client=client, backend_factory=lambda model: served.append(model) or backend)
        asyncio.run(_post(app, _req()))
        return served, _rows(d)

    served, rows = serve("on")
    assert served == [TOOLY] and rows[0]["model"] == TOOLY
    _kill_switch(monkeypatch)
    served, rows = serve("off")
    assert served == [TEXTONLY] and rows[0]["model"] == TEXTONLY


# ── the order of application: the filter is the LAST transform ───────────────

def _reprepend_bandit(filter_first: bool):
    async def reorder(self, models, *, profile, subject):
        kept = cf.filter_chain(models, Needs(tools=True)) if filter_first else list(models)
        return [TEXTONLY] + [m for m in kept if m != TEXTONLY]
    return reorder


def _order_violations(*, mutant: bool) -> int:
    """How many excluded models a bandit that re-prepends one still gets dispatched."""
    extra = {"llm_router.capability_filter.filter_chain_report":
             (lambda chain, needs, **k: (list(chain), []))} if mutant else None
    out = route(CHAIN, TOOLS_PROMPT, bandit=_reprepend_bandit(filter_first=mutant), extra=extra)
    assert isinstance(out, list), out
    return sum(m in EXCLUDED["tools"] for m in out)


def test_filter_runs_after_the_bandit_re_prepends_an_excluded_model():
    assert _order_violations(mutant=False) == 0
    out = route(CHAIN, TOOLS_PROMPT, bandit=_reprepend_bandit(filter_first=False))
    assert out == EXPECTED["tools"]


def test_mutant_f3_filter_before_the_bandit_is_caught():
    assert _order_violations(mutant=True) >= 1


# ── D-116 interaction and the emergency chain ────────────────────────────────

def test_d116_qa_order_is_preserved_among_survivors(monkeypatch):
    from llm_router.router import _strip_local_for_qa

    monkeypatch.setenv("LLM_ROUTER_QA_ROUTING", "on")
    qa_chain = [TEXTONLY, OPENAI, CODEX, TOOLY, CLAUDE, MYSTERY, PERPLEXITY]
    stripped = _strip_local_for_qa(list(qa_chain), TaskType.QUERY)
    out = route(qa_chain, TOOLS_PROMPT, task=TaskType.QUERY)
    assert isinstance(out, list), out
    assert out == [m for m in stripped if m not in EXCLUDED["tools"]]
    assert len(out) < len(stripped), "nothing was excluded: the interaction is untested"
    assert TEXTONLY not in out


def _emergency_attempts() -> list[str]:
    """Models tried in the BUDGET emergency fallback after the primary chain fails."""
    from llm_router import router

    attempted: list[str] = []
    budget_chain = [TEXTONLY, TOOLY, GEMINI, OPENAI, MYSTERY]

    async def build(task, profile, *a, **k):
        return list(budget_chain) if profile == RoutingProfile.BUDGET else [CLAUDE]

    async def call_llm(model, messages, **kw):
        attempted.append(model)
        raise RuntimeError("provider down")

    needs = cf.needs_from_prompt(VISION_PROMPT, "code")
    assert needs.vision and not needs.tools
    tracker = MagicMock()
    tracker.is_healthy.return_value = True
    with ExitStack() as es:
        p = es.enter_context
        p(patch("llm_router.router.get_tracker", return_value=tracker))
        p(patch("llm_router.router._build_and_filter_chain", new_callable=AsyncMock, side_effect=build))
        p(patch("llm_router.router.providers.call_llm", new_callable=AsyncMock, side_effect=call_llm))
        p(patch("llm_router.router.cost.log_usage", new_callable=AsyncMock))
        p(patch("llm_router.router._native_notify", lambda *a, **k: None))
        p(patch("llm_router.router.commit_envelope", new_callable=AsyncMock))
        p(patch("llm_router.router.release_envelope", new_callable=AsyncMock))
        try:
            asyncio.run(router._dispatch_model_loop(
                models_to_try=[CLAUDE], task_type=TaskType.CODE, profile=RoutingProfile.BALANCED,
                prompt=VISION_PROMPT, system_prompt=None, temperature=None, max_tokens=None,
                media_params=None, ctx=None, classification_data=None, caller_context=None,
                use_thinking=False, correlation_id="c", complexity_hint=None, c=Complexity.SIMPLE,
                config=tq._Cfg(), route_span=MagicMock(), route_log=MagicMock(), _reservation=0.0,
                effective_complexity="simple", capability_needs=needs if cf.enabled() else None,
                suppress_ledger=True))
        except Exception:  # noqa: BLE001 - every provider fails by design
            pass
    return [m for m in attempted if m != CLAUDE]


def test_emergency_budget_chain_is_filtered_too(monkeypatch):
    assert _emergency_attempts() == [GEMINI, OPENAI, MYSTERY]
    _kill_switch(monkeypatch)
    assert _emergency_attempts() == [TEXTONLY, TOOLY, GEMINI, OPENAI, MYSTERY]   # teeth: it is the filter


def test_a_pin_is_not_exempt_and_the_error_names_the_capability():
    out = route(CHAIN, VISION_PROMPT, override=TEXTONLY, extra={
        "llm_router.router._build_and_filter_chain": AsyncMock(return_value=[TEXTONLY])})
    assert isinstance(out, ValueError), out
    assert "No providers available" in str(out) and f"{TEXTONLY}: lacks vision" in str(out)


# ── capability sources, needs, flag ──────────────────────────────────────────

def test_caps_of_reads_map_then_seat_table_then_registry_and_unknown_stays_unknown():
    caps = cf.caps_of
    assert caps(TEXTONLY) == cf.ModelCaps(False, False, 4096)
    assert caps(TOOLY) == cf.ModelCaps(True, False, 32768)
    assert caps(CODEX).tools is True and caps(CODEX).vision is False
    assert caps(GEMINI).vision is True and caps(CLAUDE).vision is True
    assert caps(OPENAI) == cf.ModelCaps(True, True, 128000)       # API-key row: unknown in the map, registry speaks
    assert caps(PERPLEXITY) == cf.ModelCaps(False, False, 128000)
    assert caps(MYSTERY) == cf.ModelCaps(None, None, None)


@pytest.mark.parametrize("state", ["missing", "stale", "other_schema"])
def test_no_usable_map_still_filters_from_static_and_registry_and_never_raises(state):
    if state == "missing":
        rm.map_path().unlink()
    elif state == "stale":
        put_map(age_s=map_reader.MAX_AGE_S * 2)
    else:
        put_map(schema=99)
    assert map_reader.load_view() is None
    chain = [CODEX, PERPLEXITY, MYSTERY, OPENAI, TEXTONLY]
    out = cf.filter_chain(chain, Needs(tools=True, vision=True))
    assert out == [MYSTERY, OPENAI, TEXTONLY]      # codex (no vision) and perplexity go; ollama is unknown without a map


def test_provider_with_no_map_row_keeps_its_slot_and_is_filtered_by_registry():
    put_map([r for r in map_rows() if r["chain_id"] != "openai/api"])
    assert cf.filter_chain([OPENAI, PERPLEXITY], Needs(tools=True)) == [OPENAI]


def test_fail_open_on_a_raising_registry_and_a_raising_caps_lookup(monkeypatch):
    recorded: list[str] = []
    monkeypatch.setattr(failopen, "record", lambda code, exc=None, **kw: recorded.append(code))

    def boom():
        raise RuntimeError("registry file is broken")

    monkeypatch.setattr(model_registry, "_default_registry", boom)
    assert cf.caps_of(OPENAI) == cf.ModelCaps(None, None, None)
    assert cf.filter_chain([OPENAI, PERPLEXITY], Needs(tools=True)) == [OPENAI, PERPLEXITY]   # unknown never excludes
    assert "CHZ-FO-CAPABILITY-FILTER" in recorded
    recorded.clear()
    monkeypatch.setattr(cf, "caps_of", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert cf.filter_chain(CHAIN, Needs(tools=True)) == CHAIN
    assert recorded == ["CHZ-FO-CAPABILITY-FILTER"]


def test_needs_from_prompt_messages_and_body():
    n = cf.needs_from_prompt(TOOLS_PROMPT, "code", "sys")
    assert n.tools and not n.vision and n.tokens == (len("sys") + len(TOOLS_PROMPT)) // 4
    assert cf.needs_from_prompt(VISION_PROMPT, "code").vision
    plain = cf.needs_from_prompt("hello there", "query")
    assert not plain.tools and not plain.vision
    history = [{"role": "user", "content": "h" * 4000}]
    m = cf.needs_from_messages(history, "short", "")
    assert m.tokens == max(1, len("short") // 4), "history must not count"
    body = {"system": "s" * 40, "tools": [{"name": "Read"}],
            "messages": [{"role": "user", "content": [{"type": "image", "source": {}}]},
                         {"role": "assistant", "content": "ok"},
                         {"role": "user", "content": [{"type": "text", "text": "t" * 400}]}]}
    b = cf.needs_from_anthropic_body(body)
    assert b.tools and b.vision and 110 <= b.tokens <= 130, b       # 40 + 400 chars + the tools json, history excluded
    assert not cf.needs_from_anthropic_body({"messages": [{"role": "user", "content": "hi"}]}).tools


def test_flag_is_on_by_default_and_the_kill_switch_values():
    assert cf.enabled() is True
    for off in ("0", "off", "False", "no", " OFF "):
        with patch.dict(os.environ, {"LLM_ROUTER_CAPABILITY_ROUTING": off}):
            assert cf.enabled() is False, off
    for on in ("1", "on", "true", "yes", ""):
        with patch.dict(os.environ, {"LLM_ROUTER_CAPABILITY_ROUTING": on}):
            assert cf.enabled() is True, on


LEGACY_VS_VECTOR = [
    "who calls `load_view` anywhere",
    "list the usages of `parse_config()`",
    "rebase my branch onto main and show the diff",
    "find all callers of the retry helper",
]


@pytest.mark.parametrize("prompt", LEGACY_VS_VECTOR)
def test_default_on_widens_needs_claude_tools_documented_consequence(prompt, monkeypatch):
    """The hook falls through to Claude more often by default: the 8-bit vector sees what the
    legacy boolean does not. Four fixed prompts; the kill switch restores the legacy answer."""
    from llm_router.hooks.chain_builder import needs_claude_tools

    assert needs_claude_tools(prompt, "code") is True
    _kill_switch(monkeypatch)
    assert needs_claude_tools(prompt, "code") is False


def test_provider_caps_equal_what_the_map_builds_for_fixture_seats(tmp_path):
    from tests.test_resource_map import make_inputs, row

    data = rm.build(make_inputs(tmp_path))
    for resource, provider in (("claude", "anthropic"), ("codex", "codex"), ("gemini_cli", "gemini_cli")):
        caps = row(data, resource)["capabilities"]
        assert cf.PROVIDER_CAPS[provider] == ("tools" in caps, "vision" in caps), (resource, caps)
    view = map_reader._index(data)
    for provider, pair in cf.PROVIDER_CAPS.items():
        assert view.caps_by_provider[provider] == pair


# ── mutants: each makes a named test fail ────────────────────────────────────

def _excluded_dispatched() -> int:
    """Models known to lack the capability that the router still dispatched, over the 3 caps."""
    n = 0
    for cap in CAPS:
        out = door_router(cap)
        assert isinstance(out, list), out
        n += sum(m in EXCLUDED[cap] for m in out)
    return n


def test_mutant_f1_identity_filter_is_caught(monkeypatch):
    assert _excluded_dispatched() == 0
    monkeypatch.setattr(cf, "filter_chain_report", lambda chain, needs, **k: (list(chain), []))
    assert _excluded_dispatched() == sum(len(v) for v in EXCLUDED.values())


def _unknown_removed() -> int:
    """Unknown-capability models the router dropped (they must all stay)."""
    out = door_router("tools")
    return sum(m not in out for m in (MYSTERY, OPENAI))


def test_mutant_f2_unknown_treated_as_lacking_is_caught(monkeypatch):
    assert _unknown_removed() == 0
    real = cf.caps_of

    def strict(model_id, view=None):
        c = real(model_id, view)
        return cf.ModelCaps(bool(c.tools), bool(c.vision), c.window)

    monkeypatch.setattr(cf, "caps_of", strict)
    assert _unknown_removed() >= 1


def test_mutant_f5_default_off_is_caught(monkeypatch):
    from llm_router import capabilities

    def violations() -> int:
        return int(not cf.enabled()) + int(door_router("tools") == CHAIN)

    assert violations() == 0

    def old_default():
        return os.environ.get("LLM_ROUTER_CAPABILITY_ROUTING", "").strip().lower() in ("1", "on", "true", "yes")

    monkeypatch.setattr(capabilities, "capability_routing_enabled", old_default)
    assert violations() == 2


def _history_exclusions() -> int:
    """A short ask after a very long history: nothing may be excluded on window grounds."""
    history = [{"role": "user", "content": LONG_PROMPT}, {"role": "assistant", "content": "ok"}]
    out = route(CHAIN, "hello", messages=history)
    assert isinstance(out, list), out
    return len(CHAIN) - len(out)


def test_mutant_f7_whole_conversation_window_is_caught(monkeypatch):
    assert _history_exclusions() == 0

    def whole(messages, prompt="", system=""):
        text = (system or "") + (prompt or "") + "".join(
            str(m.get("content", "")) for m in messages or [] if isinstance(m, dict))
        return Needs(vision=False, tokens=cf._estimate(text))

    monkeypatch.setattr(cf, "needs_from_messages", whole)
    assert _history_exclusions() >= 2          # the 4096 and 32768 windows
