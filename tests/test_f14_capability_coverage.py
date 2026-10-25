"""F14: the P2.4 capability filter on the doors #533 left out, and the filter-emptied
behaviour of every door pinned against docs/empty-chain-behaviour.md.

New doors: ``route_and_stream`` (vision marker, window), ``execute_agent`` (the agent loop
NEEDS tools: an explicit caller flag, hard) and so subagent delegation and the SDK agent
path, the native ``/route`` image refusal (newest turn only, as on the other gateway
routes) and the proxy ``--serve local-agent`` pinned path (a pin is held to the filter,
as in ``choose_model``).

Needs rules are the ones of P2.4: strong signals only. The prompt-text tools regex stays
shadow (a tools-regex prompt at ``route_and_stream`` keeps the whole chain). An unknown
capability never excludes.

Every door that filters also has a mutant: with the filter replaced by the identity the
same observation turns red.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from llm_router import capability_filter as cf
from llm_router.hooks.direct_executor import ModelSpec
from llm_router.types import RoutingProfile, TaskType
from tests.test_p24_capability_hard import (  # noqa: F401 - fixtures: autouse world, traced
    CHAIN, CODEX, EXPECTED, GEMINI, LONG_PROMPT, MYSTERY, PERPLEXITY, TEXTONLY, TOOLY,
    TOOLS_PROMPT, VISION_PROMPT, HookRig, _kill_switch,
    gateway_client, put_map, route, specs, traced, world,
)

DOC = Path(__file__).resolve().parents[1] / "docs" / "empty-chain-behaviour.md"
IMG = "iVBORw0KGgo="

#: Every entry of these chains is KNOWN to lack the capability named (map or registry).
NO_VISION = [TEXTONLY, TOOLY, CODEX, PERPLEXITY]
NO_TOOLS = [TEXTONLY, PERPLEXITY]


def identity_filter(monkeypatch):
    """The mutant: the filter keeps every entry."""
    monkeypatch.setattr(cf, "filter_chain_report", lambda chain, needs, **k: (list(chain), []))
    monkeypatch.setattr(cf, "filter_chain", lambda chain, needs, **k: list(chain))


# ── the documented filter-emptied table (the test input) ─────────────────────

def parse_filter_table(path: Path = DOC) -> dict[str, str]:
    out: dict[str, str] = {}
    on = False
    for line in path.read_text().splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if cells == ["door", "trigger", "behaviour"]:
            on = True
        elif on and set(cells[0]) <= {"-", " "}:
            continue
        elif on and len(cells) == 3:
            out[cells[0]] = cells[2]
    return out


DOORS = ("router", "stream", "hook", "agent", "subagent", "sdk", "gateway", "route", "proxy", "local_mode")
TOKENS = {"raises_no_providers", "raises_value_error", "falls_through_to_claude", "returns_none",
          "real_spawn", "routing_error", "http_400", "forwarded_policy_kept"}


def test_the_filter_emptied_table_is_complete_and_uses_known_tokens():
    table = parse_filter_table()
    assert set(table) == set(DOORS), table
    assert set(table.values()) <= TOKENS, table
    assert table == {"router": "raises_no_providers", "stream": "raises_value_error",
                     "hook": "falls_through_to_claude", "agent": "returns_none", "subagent": "real_spawn",
                     "sdk": "routing_error", "gateway": "http_400", "route": "http_400",
                     "proxy": "forwarded_policy_kept", "local_mode": "forwarded_policy_kept"}


# ── door observers: each runs a filter-emptied request and reduces it to one token ──

def _stream_events(chain, prompt, monkeypatch, *, fail_providers=True):
    """``(attempted models, raised exception or None)`` of ``route_and_stream`` over *chain*."""
    from llm_router import providers, router

    attempted: list[str] = []

    async def fake_stream(*, model, messages, **kw):
        attempted.append(model)
        raise RuntimeError("provider down")
        yield  # pragma: no cover - makes this an async generator

    async def fake_ctx(**kw):
        return [{"role": "user", "content": kw.get("caller_context") or ""}]

    monkeypatch.setattr(router, "get_model_chain", lambda *a, **k: list(chain))
    monkeypatch.setattr(providers, "call_llm_stream_events", fake_stream)
    monkeypatch.setattr(router, "build_context_messages", fake_ctx)

    events: list[dict] = []

    async def go():
        async for ev in router.route_and_stream(TaskType.CODE, prompt, profile=RoutingProfile.BALANCED):
            events.append(ev)

    try:
        asyncio.run(go())
    except Exception as exc:  # noqa: BLE001 - the caller asserts on it
        # The filter-emptied chain raises before any event. (An exhausted chain no longer
        # raises after its ``route.aborted`` event: F33.)
        return attempted, exc, events
    return attempted, None, events


def observe_router(monkeypatch):
    out = route(NO_VISION, VISION_PROMPT)
    ok = isinstance(out, ValueError) and "Capability filter excluded" in str(out)
    return "raises_no_providers" if ok else f"other:{out!r}"[:100]


def observe_stream(monkeypatch):
    attempted, exc, _ = _stream_events(NO_VISION, VISION_PROMPT, monkeypatch)
    if attempted:
        return f"attempted:{attempted}"
    return "raises_value_error" if isinstance(exc, ValueError) and "Capability filter excluded" in str(exc) else f"other:{exc!r}"[:100]


def observe_hook(monkeypatch):
    from llm_router.hooks.direct_executor import execute_chain

    rig = HookRig(monkeypatch)
    result = execute_chain(VISION_PROMPT, specs(NO_VISION), "code")
    if rig.attempted:
        return f"attempted:{rig.attempted}"
    return "falls_through_to_claude" if result is None else "ran"


class AgentRig:
    """Replaces the agent loop with a recorder that fails every model."""

    def __init__(self, monkeypatch):
        import llm_router.hooks.agent_loop as al

        self.attempted: list[str] = []

        def loop(*, prompt, model, **kw):
            self.attempted.append(model)
            return None

        monkeypatch.setattr(al, "run_agent_loop", loop)


def ospecs(*models):
    return [ModelSpec("ollama", m.split("/", 1)[1]) for m in models]


OLLAMA_NO_TOOLS = ospecs(TEXTONLY)       # map: [generate] reported, so known to lack tools


def observe_agent(monkeypatch):
    from llm_router.hooks.direct_executor import execute_agent

    rig = AgentRig(monkeypatch)
    result = execute_agent(TOOLS_PROMPT, list(OLLAMA_NO_TOOLS))
    if rig.attempted:
        return f"attempted:{rig.attempted}"
    return "returns_none" if result is None else "ran"


def observe_subagent(monkeypatch):
    import llm_router.hooks.chain_builder as cb
    from tests.test_agent_route_hook import _load_hook_module

    rig = AgentRig(monkeypatch)
    monkeypatch.setattr(cb, "build_chain", lambda *a, **k: list(OLLAMA_NO_TOOLS))
    monkeypatch.setattr(cb, "get_current_pressure", lambda: ("green", 0.0))
    monkeypatch.setattr(cb, "needs_claude_tools", lambda *a, **k: True)
    monkeypatch.setenv("LLM_ROUTER_SUBAGENT_DIRECT", "on")
    mod = _load_hook_module()
    text = mod._try_direct_subagent(TOOLS_PROMPT, "code", "simple", "s1")
    if rig.attempted:
        return f"attempted:{rig.attempted}"
    return "real_spawn" if text is None else "ran"


def observe_sdk(monkeypatch):
    from llm_router import sdk
    from llm_router.hooks import chain_builder, savings_logger

    rig = HookRig(monkeypatch)
    monkeypatch.setattr(chain_builder, "get_current_pressure", lambda: ("green", 0.0))
    monkeypatch.setattr(chain_builder, "needs_claude_tools", lambda p, t: False)
    monkeypatch.setattr(chain_builder, "build_chain", lambda *a, **k: specs(NO_VISION))
    monkeypatch.setattr(savings_logger, "log_direct_to_db", lambda **kw: None)
    monkeypatch.setattr(savings_logger, "log_direct_savings", lambda **kw: None)
    try:
        sdk.route(VISION_PROMPT, task_type="code", complexity="moderate", timeout=2)
    except sdk.RoutingError:
        return f"attempted:{rig.attempted}" if rig.attempted else "routing_error"
    return "returned"


class _Seen(Exception):
    def __init__(self, resp):
        self.resp = resp


def _http_via(path, body):
    client = gateway_client()

    def via():
        raise _Seen(client.post(path, json=body))

    return via


def _http_token(out):
    if not isinstance(out, _Seen):
        return f"other:{out!r}"[:100]
    r = out.resp
    detail = str(r.json().get("detail", ""))
    if r.status_code == 400 and detail.startswith("No providers available"):
        return "http_400"
    return f"http_{r.status_code}:{detail[:60]}"


def observe_gateway(monkeypatch):
    out = route(NO_VISION, "unused", via=_http_via(
        "/v1/chat/completions", {"messages": [{"role": "user", "content": VISION_PROMPT}], "task_type": "code"}))
    return _http_token(out)


def observe_route(monkeypatch):
    out = route(NO_VISION, "unused", via=_http_via("/route", {"prompt": VISION_PROMPT, "task_type": "code"}))
    return _http_token(out)


def _proxy_tools_body():
    from tests.test_proxy import _req

    body = _req()
    body["tools"] = body.get("tools") or [{"name": "Read", "input_schema": {"type": "object"}}]
    return body


def observe_proxy(monkeypatch, tmp_path=None):
    from llm_router.proxy import backends

    async def policy_chain(text):
        return "code", "moderate", list(NO_TOOLS)

    monkeypatch.setattr(backends, "policy_chain", policy_chain)
    choice = asyncio.run(backends.choose_model("x", None, needs=cf.needs_from_anthropic_body(_proxy_tools_body())))
    if choice["model"] is not None:
        return f"served:{choice['model']}"
    return _proxy_app_token(monkeypatch, tmp_path)


def _proxy_app_token(monkeypatch, tmp_path):
    """The proxy app over the same chain: the token of what its ledger says."""
    from llm_router.proxy import ledger, server as ps
    from tests.test_proxy import FakeBackend, Upstream, _ollama, _post, _rows

    d = tmp_path / "proxy-app"
    d.mkdir(exist_ok=True)
    cfg = ps.ProxyConfig(upstream="http://127.0.0.1:9", ledger_path=d / "proxy_calls.jsonl")
    backend = FakeBackend(_ollama("never"))
    app = ps.build_app(cfg, client=httpx.AsyncClient(transport=httpx.MockTransport(Upstream())),
                       backend_factory=lambda m: backend)
    asyncio.run(_post(app, _proxy_tools_body()))
    rows = _rows(d)
    if backend.calls:
        return "served"
    if len(rows) == 1 and rows[0].get("decision") == ledger.DECISION_FORWARDED and rows[0].get("reason") == "policy_kept":
        return "forwarded_policy_kept"
    return f"row:{[(r.get('decision'), r.get('reason'), r.get('detail')) for r in rows]}"


def observe_local_mode(monkeypatch, tmp_path=None):
    from llm_router.proxy import server as ps
    from tests import test_proxy_local_mode as tl

    async def boom(*a, **k):
        raise AssertionError("local-agent mode must not consult the routing policy")

    monkeypatch.setattr(ps, "choose_model", boom)

    rows, backend = _local_mode_run(tmp_path, TEXTONLY, tl._first_call())
    if backend.calls:
        return "served"
    if len(rows) == 1 and rows[0].get("decision") == "forwarded" and rows[0].get("reason") == "policy_kept":
        return "forwarded_policy_kept"
    return f"row:{[(r.get('decision'), r.get('reason'), r.get('detail')) for r in rows]}"


def _local_mode_run(tmp_path, pin, body):
    from llm_router.proxy import ledger, server as ps
    from tests import test_proxy_local_mode as tl

    d = tmp_path / f"lm-{abs(hash((pin, json.dumps(body, sort_keys=True))))}"
    d.mkdir(exist_ok=True)
    backend = tl.Backend(tl._ollama("", [tl._call("Bash", {"command": "ls"})]))
    cfg = ps.ProxyConfig(upstream="http://127.0.0.1:9", ledger_path=d / "proxy_calls.jsonl",
                         kill_switch=d / "kill", model=pin, trim="none", serve="local-agent",
                         hedge_s=None, warm_up=False)
    client = httpx.AsyncClient(transport=httpx.MockTransport(tl.Upstream()))
    app = ps.build_app(cfg, client=client, backend_factory=lambda m: backend, breaker_fn=tl.Breaker())
    asyncio.run(tl._post(app, body))
    return ledger.read_rows(d / "proxy_calls.jsonl"), backend


@pytest.fixture
def no_choose_model(monkeypatch):
    """Local mode never consults the policy (P2); a call to choose_model is a bug."""
    from llm_router.proxy import server as ps

    async def boom(*a, **k):
        raise AssertionError("local-agent mode must not consult the routing policy")

    monkeypatch.setattr(ps, "choose_model", boom)


OBSERVERS = {
    "router": observe_router, "stream": observe_stream, "hook": observe_hook, "agent": observe_agent,
    "subagent": observe_subagent, "sdk": observe_sdk, "gateway": observe_gateway, "route": observe_route,
    "proxy": observe_proxy, "local_mode": observe_local_mode,
}


def _observe(door, monkeypatch, tmp_path):
    fn = OBSERVERS[door]
    if door in ("proxy", "local_mode"):
        return fn(monkeypatch, tmp_path)
    return fn(monkeypatch)


@pytest.mark.parametrize("door", DOORS)
def test_filter_emptied_chain_behaviour_equals_the_documented_token(door, monkeypatch, tmp_path):
    documented = parse_filter_table()
    observed = _observe(door, monkeypatch, tmp_path)
    assert observed == documented[door], f"{door}: observed {observed!r}, documented {documented[door]!r}"


@pytest.mark.parametrize("door", DOORS)
def test_mutant_filter_removed_at_each_door_turns_it_red(door, monkeypatch, tmp_path):
    """Anti-vacuity: the unfiltered chain is attempted or served, which is NOT the documented token."""
    documented = parse_filter_table()
    assert _observe(door, monkeypatch, tmp_path) == documented[door]      # control: green with the filter
    identity_filter(monkeypatch)
    observed = _observe(door, monkeypatch, tmp_path)
    assert observed != documented[door], f"{door}: removing the filter changed nothing ({observed!r})"


# ── route_and_stream ─────────────────────────────────────────────────────────

def test_stream_vision_marker_keeps_only_vision_capable_models(monkeypatch):
    attempted, exc, events = _stream_events(CHAIN, VISION_PROMPT, monkeypatch)
    assert attempted == EXPECTED["vision"]
    started = [e["model"] for e in events if e["type"] == "attempt.started"]
    assert started == EXPECTED["vision"]
    assert [e for e in events if e["type"] == "route.started"][0]["candidate_count"] == len(EXPECTED["vision"])


def test_stream_window_drops_models_whose_window_is_too_small(monkeypatch):
    attempted, _, _ = _stream_events(CHAIN, LONG_PROMPT, monkeypatch)
    assert attempted == EXPECTED["window"]


def test_stream_tools_regex_prompt_is_shadow_only_and_keeps_the_chain(monkeypatch, traced):  # noqa: F811
    attempted, _, _ = _stream_events(CHAIN, TOOLS_PROMPT, monkeypatch)
    assert attempted == CHAIN
    assert [e for e, _ in traced if e == "capability.shadow"], "the regex verdict must still be traced"
    assert not [e for e, _ in traced if e == "capability.filter"]


def test_stream_ordinary_prompt_chain_is_untouched_and_nothing_is_traced(monkeypatch, traced):  # noqa: F811
    attempted, _, _ = _stream_events(CHAIN, "what is the capital of France?", monkeypatch)
    assert attempted == CHAIN
    assert [e for e, _ in traced if e.startswith("capability.")] == []


def test_stream_kill_switch_restores_the_unfiltered_chain(monkeypatch):
    _kill_switch(monkeypatch)
    attempted, _, _ = _stream_events(CHAIN, VISION_PROMPT, monkeypatch)
    assert attempted == CHAIN


def test_stream_emptied_chain_raises_before_any_event_and_names_the_models(monkeypatch):
    attempted, exc, events = _stream_events(NO_VISION, VISION_PROMPT, monkeypatch)
    assert attempted == [] and events == []
    assert isinstance(exc, ValueError) and "Capability filter excluded" in str(exc)
    assert "ollama/textonly:1: lacks vision" in str(exc)


def test_stream_mutant_filter_removed_attempts_the_excluded_models(monkeypatch):
    identity_filter(monkeypatch)
    attempted, _, _ = _stream_events(CHAIN, VISION_PROMPT, monkeypatch)
    assert attempted == CHAIN != EXPECTED["vision"]


# ── execute_agent: the agent loop needs tools ────────────────────────────────

AGENT_CHAIN = ospecs(TEXTONLY, TOOLY, "ollama/mystery:1")     # known no tools, tools, unknown


def test_execute_agent_never_attempts_a_model_known_to_lack_tools(monkeypatch):
    from llm_router.hooks.direct_executor import execute_agent

    rig = AgentRig(monkeypatch)
    # An ordinary prompt: the exclusion comes from the caller flag, not from the prompt text.
    assert execute_agent("summarize the notes", list(AGENT_CHAIN)) is None
    assert rig.attempted == ["tooly:1", "mystery:1"]


def test_execute_agent_keeps_unknown_capability_models(monkeypatch):
    from llm_router.hooks.direct_executor import execute_agent

    rig = AgentRig(monkeypatch)
    execute_agent("summarize the notes", ospecs("ollama/mystery:1"))
    assert rig.attempted == ["mystery:1"]


def test_execute_agent_window_and_vision_are_hard_too(monkeypatch):
    from llm_router.hooks.direct_executor import execute_agent

    rig = AgentRig(monkeypatch)
    execute_agent(LONG_PROMPT, list(AGENT_CHAIN))            # 40000 tokens: tooly (32768) is too small
    assert rig.attempted == ["mystery:1"]
    rig.attempted.clear()
    execute_agent(VISION_PROMPT, list(AGENT_CHAIN))          # tooly has no vision
    assert rig.attempted == ["mystery:1"]


def test_execute_agent_kill_switch_restores_the_old_attempts(monkeypatch):
    from llm_router.hooks.direct_executor import execute_agent

    _kill_switch(monkeypatch)
    rig = AgentRig(monkeypatch)
    execute_agent("summarize the notes", list(AGENT_CHAIN))
    assert sorted(rig.attempted) == ["mystery:1", "textonly:1", "tooly:1"]


def test_execute_agent_mutant_filter_removed_attempts_the_tool_less_model(monkeypatch):
    from llm_router.hooks.direct_executor import execute_agent

    identity_filter(monkeypatch)
    rig = AgentRig(monkeypatch)
    execute_agent("summarize the notes", list(AGENT_CHAIN))
    assert "textonly:1" in rig.attempted


def test_execute_agent_emptied_chain_prints_nothing_and_returns_none(monkeypatch, capsys):
    from llm_router.hooks.direct_executor import execute_agent

    rig = AgentRig(monkeypatch)
    assert execute_agent("summarize the notes", list(OLLAMA_NO_TOOLS)) is None
    assert rig.attempted == []


def test_sdk_agent_path_attempts_only_tool_capable_models(monkeypatch):
    """The SDK takes execute_agent for a tools prompt: same survivors, RoutingError when none."""
    from llm_router import sdk
    from llm_router.hooks import chain_builder, savings_logger

    rig = AgentRig(monkeypatch)
    monkeypatch.setattr(chain_builder, "get_current_pressure", lambda: ("green", 0.0))
    monkeypatch.setattr(chain_builder, "needs_claude_tools", lambda p, t: True)
    chain = [list(AGENT_CHAIN)]
    monkeypatch.setattr(chain_builder, "build_chain", lambda *a, **k: chain[0])
    monkeypatch.setattr(savings_logger, "log_direct_to_db", lambda **kw: None)
    monkeypatch.setattr(savings_logger, "log_direct_savings", lambda **kw: None)
    with pytest.raises(sdk.RoutingError):
        sdk.route(TOOLS_PROMPT, task_type="code", complexity="moderate", timeout=2)
    assert rig.attempted == ["tooly:1", "mystery:1"]
    rig.attempted.clear()
    chain[0] = list(OLLAMA_NO_TOOLS)
    with pytest.raises(sdk.RoutingError):
        sdk.route(TOOLS_PROMPT, task_type="code", complexity="moderate", timeout=2)
    assert rig.attempted == []


def test_subagent_delegation_attempts_only_tool_capable_models(monkeypatch):
    import llm_router.hooks.chain_builder as cb
    from tests.test_agent_route_hook import _load_hook_module

    rig = AgentRig(monkeypatch)
    monkeypatch.setattr(cb, "build_chain", lambda *a, **k: list(AGENT_CHAIN))
    monkeypatch.setattr(cb, "get_current_pressure", lambda: ("green", 0.0))
    monkeypatch.setattr(cb, "needs_claude_tools", lambda *a, **k: True)
    monkeypatch.setenv("LLM_ROUTER_SUBAGENT_DIRECT", "on")
    assert _load_hook_module()._try_direct_subagent(TOOLS_PROMPT, "code", "simple", "s1") is None
    assert rig.attempted == ["tooly:1", "mystery:1"]


# ── native /route: images ────────────────────────────────────────────────────

IMG_PART = {"type": "image_url", "image_url": {"url": "data:image/png;base64," + IMG}}


@pytest.fixture
def route_core(monkeypatch):
    seen: list[dict] = []

    async def fake(payload):
        seen.append(payload)
        return {"text": "ok"}

    monkeypatch.setattr("llm_router.route_server.route_payload_async", fake)
    return seen


@pytest.mark.parametrize("body", [
    {"prompt": [{"type": "text", "text": "what is this"}, IMG_PART]},
    {"prompt": [IMG_PART, {"type": "text", "text": "what is this"}]},
    {"prompt": "what is this", "images": [IMG]},
], ids=["parts-text-first", "parts-image-first", "images-field"])
def test_route_refuses_an_image_in_the_request(body, route_core):
    from llm_router.gateway import _IMAGES_UNSUPPORTED

    r = gateway_client().post("/route", json=body)
    assert r.status_code == 400 and r.json()["detail"] == _IMAGES_UNSUPPORTED, r.text[:200]
    assert route_core == []


def test_route_flattens_an_image_only_in_history(route_core):
    """D-165 (4): ``messages`` is the history before the request; an image there is not refused."""
    body = {"prompt": "and what colour is it", "messages": [
        {"role": "user", "content": [{"type": "text", "text": "what is this"}, IMG_PART]},
        {"role": "assistant", "content": "a cat"}]}
    r = gateway_client().post("/route", json=body)
    assert r.status_code == 200, r.text[:200]
    assert len(route_core) == 1 and route_core[0]["prompt"] == "and what colour is it"


def test_route_text_request_reaches_the_router(route_core):
    assert gateway_client().post("/route", json={"prompt": "hi"}).status_code == 200
    assert len(route_core) == 1


def test_route_mutant_removing_the_refusal_is_caught(route_core, monkeypatch):
    from llm_router import gateway

    monkeypatch.setattr(gateway, "_refuse_images_if_present", lambda *v: None)
    gateway_client().post("/route", json={"prompt": "what is this", "images": [IMG]})
    assert len(route_core) == 1, "with the refusal removed the image request must reach the router"


def test_route_vision_marker_goes_through_the_router_filter(monkeypatch, traced):  # noqa: F811
    """No image part, only the host's ``[Image #N]`` marker: not refused here; the router's
    vision filter excludes the models that cannot read it and the survivors are dispatched."""
    out = route(CHAIN, "unused", via=_http_via("/route", {"prompt": VISION_PROMPT, "task_type": "code"}))
    assert isinstance(out, _Seen) and out.resp.status_code == 200, out
    assert [m for e, f in traced if e == "capability.filter" for m in [x["model"] for x in f["excluded"]]] == [
        m for m in CHAIN if m not in EXPECTED["vision"]]


# ── proxy --serve local-agent: the pin is held to the filter ─────────────────

def _lm_map(extra_rows=()):
    from tests.test_p24_capability_hard import map_rows

    put_map(map_rows() + list(extra_rows))


def test_local_pin_known_to_lack_tools_keeps_the_call_on_claude(tmp_path, no_choose_model):
    from tests import test_proxy_local_mode as tl

    rows, backend = _local_mode_run(tmp_path, TEXTONLY, tl._first_call())
    assert backend.calls == []
    (row,) = rows
    assert row["decision"] == "forwarded" and row["reason"] == "policy_kept"
    assert row["model"] is None and row["chain_head"] == [] and row["task_type"] == "local_agent"


def test_local_pin_with_tools_is_still_served(tmp_path, no_choose_model):
    from tests import test_proxy_local_mode as tl

    rows, backend = _local_mode_run(tmp_path, TOOLY, tl._first_call())
    assert len(backend.calls) == 1 and rows[0]["decision"] == "served" and rows[0]["model"] == TOOLY


def test_local_pin_unknown_capability_is_served(tmp_path, no_choose_model):
    from tests import test_proxy_local_mode as tl

    rows, backend = _local_mode_run(tmp_path, "ollama/unmapped:1", tl._first_call())
    assert len(backend.calls) == 1 and rows[0]["decision"] == "served"


def test_local_pin_window_is_hard(tmp_path, no_choose_model):
    """A pin whose known window is smaller than the irreducible tokens is not served."""
    from tests import test_proxy_local_mode as tl
    from tests.test_p24_capability_hard import _row

    tiny = "ollama/tiny:1"
    _lm_map([_row("ollama/tiny:1", "local", tiny, ["generate", "tools"], {"num_ctx": 64, "max_context": 64})])
    rows, backend = _local_mode_run(tmp_path, tiny, tl._first_call())
    assert backend.calls == []
    assert rows[0]["reason"] == "policy_kept" and rows[0]["decision"] == "forwarded"


def test_local_pin_vision_media_still_goes_to_claude_via_the_media_gate(tmp_path, no_choose_model):
    """Vision on this door was already refused by local mode's own media gate; the filter agrees."""
    from tests import test_proxy_local_mode as tl

    rows, backend = _local_mode_run(tmp_path, TOOLY, tl._with_image(tl._first_call()))
    assert backend.calls == [] and rows[0]["decision"] == "forwarded"


def test_local_pin_kill_switch_restores_the_old_serve(tmp_path, monkeypatch, no_choose_model):
    from tests import test_proxy_local_mode as tl

    _kill_switch(monkeypatch)
    rows, backend = _local_mode_run(tmp_path, TEXTONLY, tl._first_call())
    assert len(backend.calls) == 1 and rows[0]["decision"] == "served"


def test_local_pin_mutant_filter_removed_serves_the_tool_less_pin(tmp_path, monkeypatch, no_choose_model):
    from tests import test_proxy_local_mode as tl

    identity_filter(monkeypatch)
    rows, backend = _local_mode_run(tmp_path, TEXTONLY, tl._first_call())
    assert len(backend.calls) == 1 and rows[0]["decision"] == "served"


# ── byte-identical when no need is detected ──────────────────────────────────

def test_no_need_detected_leaves_route_and_stream_untouched_and_the_filter_is_never_asked(monkeypatch):
    calls = []
    real = cf.filter_chain_report
    monkeypatch.setattr(cf, "filter_chain_report", lambda chain, needs, **k: (calls.append(needs), real(chain, needs, **k))[1])
    attempted, _, _ = _stream_events(CHAIN, "what is the capital of France?", monkeypatch)
    assert attempted == CHAIN
    assert calls and not (calls[0].tools or calls[0].vision), calls
