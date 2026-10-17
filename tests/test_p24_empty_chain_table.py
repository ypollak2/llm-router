"""P2.4 ext-a (P2.4-d): the documented empty-chain table equals the behaviour.

``docs/empty-chain-behaviour.md`` says what each caller does when every chain entry
is a provider the fresh resource map knows and has no connected row for. This file
PARSES that table (it is the test input), drives the router, hook, proxy, SDK and
gateway callers over 8 all-disconnected fixtures (8 x 5 = 40 observations) and fails
when an observed behaviour differs from its documented token. Three guard fixtures
(provider with no map row, stale map, other-schema map) must NOT empty the chain.

Also here: the gateway refuses image parts (HTTP 400) on every completion route
instead of silently dropping them, in the same discovery style as
tests/test_r10_refuse_what_cannot_be_served.py.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from llm_router import map_reader, resource_map as rm
from llm_router.types import Complexity, RoutingProfile, TaskType

fastapi_testclient = pytest.importorskip("fastapi.testclient")

DOC = Path(__file__).resolve().parents[1] / "docs" / "empty-chain-behaviour.md"
CALLERS = ("router", "hook", "proxy", "sdk", "gateway")
TOKENS = {"raises_no_providers", "falls_through_to_claude", "forwarded_policy_kept",
          "routing_error", "http_400"}
GUARDS = {"provider with no map row", "stale map", "other-schema map"}

CODE_PROMPT = "write a python function that reverses a list"
QA_PROMPT = "what is the capital of France?"


# ── the documented table (the test input) ────────────────────────────────────

def parse_doc(path: Path = DOC) -> tuple[dict[str, str], dict[str, str]]:
    """``({caller: token}, {situation: behaviour})`` from the two tables of the doc."""
    callers: dict[str, str] = {}
    guards: dict[str, str] = {}
    mode = None
    for line in path.read_text().splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")] if line.strip().startswith("|") else None
        if cells is None:
            continue
        if cells == ["caller", "trigger", "behaviour"]:
            mode = "callers"
        elif cells == ["situation", "behaviour"]:
            mode = "guards"
        elif set(cells[0]) <= {"-", " "}:
            continue
        elif mode == "callers" and len(cells) == 3:
            callers[cells[0]] = cells[2]
        elif mode == "guards" and len(cells) == 2:
            guards[cells[0]] = cells[1]
    return callers, guards


def test_the_doc_table_is_complete_and_uses_known_tokens():
    callers, guards = parse_doc()
    assert set(callers) == set(CALLERS), callers
    assert set(callers.values()) <= TOKENS, callers
    assert callers == {"router": "raises_no_providers", "hook": "falls_through_to_claude",
                       "proxy": "forwarded_policy_kept", "sdk": "routing_error", "gateway": "http_400"}
    assert set(guards) == GUARDS, guards
    assert guards["provider with no map row"] == "keeps its slot"
    assert guards["stale map"] == guards["other-schema map"] == "chain unchanged"


# ── fixtures: map rows, environment, task ────────────────────────────────────

def _r(resource, kind, chain_id, status, caps=("generate",)):
    return rm._row(resource, kind, chain_id, status=status, capabilities=list(caps),
                   effective_priority=1 if status == "connected" else None)


def rows_all(status="absent", seat_status=None):
    """A row for every provider a chain can hold, none connected."""
    seat = seat_status or status
    return [
        _r("claude", "subscription", "anthropic/claude-subscription", seat, ("generate", "tools", "vision")),
        _r("codex", "subscription", "codex/codex-subscription", seat, ("generate", "tools")),
        _r("gemini_cli", "subscription", "gemini_cli/gemini-subscription", seat, ("generate", "tools", "vision")),
        _r("ollama", "local", "ollama", seat),
        _r("ollama/qwen3:8b", "local", "ollama/qwen3:8b", seat),
        _r("api/GEMINI_API_KEY", "api", "gemini/api", "absent"),
        _r("api/OPENAI_API_KEY", "api", "openai/api", "absent"),
    ]


def map_doc(rows, *, age_s=0.0, schema=None):
    return {"schema": rm.SCHEMA if schema is None else schema, "policy": "balanced", "resources": rows,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - age_s))}


def put(data):
    rm.write_map(data)
    p = rm.map_path()
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))


class Fx:
    def __init__(self, name, rows, *, prompt=CODE_PROMPT, task=TaskType.CODE, keys=("GEMINI_API_KEY", "OPENAI_API_KEY"),
                 ollama=True, claude=True):
        self.name, self.rows, self.prompt, self.task = name, rows, prompt, task
        self.keys, self.ollama, self.claude = keys, ollama, claude

    def __repr__(self):
        return self.name


FIXTURES = [
    Fx("all_absent", rows_all("absent")),
    Fx("installed_not_connected", rows_all("absent", seat_status="installed_not_connected")),
    Fx("local_only", rows_all("absent"), keys=(), claude=False),
    Fx("claude_codex", rows_all("absent"), keys=(), ollama=False),
    Fx("api_only", rows_all("absent"), ollama=False, claude=False),
    Fx("gemini_cli_openai", rows_all("absent"), keys=("OPENAI_API_KEY",), ollama=False, claude=False),
    Fx("qa_task", rows_all("absent"), prompt=QA_PROMPT, task=TaskType.QUERY),
    Fx("code_task", rows_all("absent"), prompt=CODE_PROMPT, task=TaskType.CODE),
]


@pytest.fixture
def world(monkeypatch):
    """The configuration under which one fixture's chains are built."""
    import llm_router.dynamic_routing as dr
    from llm_router import router
    from llm_router.config import RouterConfig
    from llm_router.hooks import chain_builder as hook_cb
    from llm_router.hooks.direct_executor import ModelSpec
    from llm_router.proxy import backends

    monkeypatch.setenv("LLM_ROUTER_BANDIT", "off")
    for leaked in ("LLM_ROUTER_DISABLE_SUBPROCESS_BACKENDS", "LLM_ROUTER_BLOCK_PROVIDERS",
                   "GEMINI_API_KEY", "OPENAI_API_KEY", "LLM_ROUTER_CLAUDE_SUBSCRIPTION"):
        monkeypatch.delenv(leaked, raising=False)
    monkeypatch.setattr(dr, "get_dynamic_model_chain", lambda *a, **k: None)
    monkeypatch.setattr(router, "is_codex_available", lambda: True)
    monkeypatch.setattr(router, "is_gemini_cli_available", lambda: True)
    monkeypatch.setattr(backends, "_chain_cache", {})

    def configure(fx: Fx):
        for k in fx.keys:
            monkeypatch.setenv(k, "x")
        if fx.claude:
            monkeypatch.setenv("LLM_ROUTER_CLAUDE_SUBSCRIPTION", "true")
        models = ["ollama/qwen3:8b"] if fx.ollama else []
        monkeypatch.setattr(RouterConfig, "all_ollama_models", lambda self: list(models))
        if fx.ollama:
            monkeypatch.setenv("OLLAMA_BUDGET_MODELS", "qwen3:8b")
        else:
            monkeypatch.delenv("OLLAMA_BUDGET_MODELS", raising=False)
        monkeypatch.setattr(hook_cb, "_ollama_models",
                            lambda *a, **k: ([ModelSpec("ollama", "qwen3:8b")] if fx.ollama else [], []))
        monkeypatch.setattr(hook_cb, "get_current_pressure", lambda: ("green", 0.0), raising=False)
        backends._chain_cache.clear()

    return configure


def _router_chain(fx):
    import asyncio

    from llm_router.config import get_config
    from llm_router.router import _build_and_filter_chain

    return asyncio.run(_build_and_filter_chain(
        fx.task, RoutingProfile.BALANCED, None, "simple", Complexity.SIMPLE, get_config()))


def _hook_chain(fx):
    from llm_router.hooks import chain_builder as hook_cb

    return hook_cb.build_chain("moderate", "green", fx.task.value)


# ── the five callers, each reduced to one token ──────────────────────────────

class Spy:
    """Every provider entry point; any call is a failure of "never an excluded entry"."""

    def __init__(self, monkeypatch):
        from llm_router import providers
        from llm_router.hooks import direct_executor as de

        self.called: list = []

        async def call_llm(model, *a, **k):
            self.called.append(model)
            raise AssertionError(f"a disconnected provider was tried: {model}")

        def hook_call(prompt, model, *a, **k):
            self.called.append(model)
            raise AssertionError(f"a disconnected provider was tried: {model}")

        monkeypatch.setattr(providers, "call_llm", call_llm)
        for name in list(de._PROVIDER_CALLS):
            monkeypatch.setitem(de._PROVIDER_CALLS, name, hook_call)


def observe_router(fx, spy, monkeypatch):
    import asyncio

    from llm_router.router import route_and_call

    try:
        asyncio.run(route_and_call(fx.task, fx.prompt, profile=RoutingProfile.BALANCED))
    except ValueError as exc:
        return "raises_no_providers" if "No providers available" in str(exc) else f"ValueError:{exc}"[:120]
    except Exception as exc:  # noqa: BLE001 - any other outcome is a different token
        return type(exc).__name__
    return "returned"


def observe_hook(fx, spy, monkeypatch):
    from llm_router.hooks.direct_executor import execute_chain

    chain = _hook_chain(fx)
    result = execute_chain(fx.prompt, chain, fx.task.value)
    if chain == [] and result is None:
        return "falls_through_to_claude"
    return "ran" if result is not None else f"none_with_chain:{len(chain)}"


def observe_proxy(fx, spy, monkeypatch):
    import asyncio

    from llm_router.proxy import backends
    from tests.test_proxy import FakeBackend, Upstream, _app, _ollama, _post, _req, _rows

    choice = asyncio.run(backends.choose_model(fx.prompt, None))
    if choice["model"] is not None or choice["chain_head"] != []:
        return f"served:{choice['model']}"
    body = _req()
    for block in body["messages"][0]["content"]:
        if isinstance(block, dict) and block.get("type") == "text":
            block["text"] = fx.prompt
            break
    tmp = Path(os.environ["LLM_ROUTER_HOME"]).parent / "proxy-obs"
    tmp.mkdir(parents=True, exist_ok=True)
    ledger_file = tmp / "proxy_calls.jsonl"
    if ledger_file.exists():
        ledger_file.unlink()
    up, backend = Upstream(), FakeBackend(_ollama("never"))
    asyncio.run(_post(_app(tmp, up, backend), body))
    rows = _rows(tmp)
    if backend.calls:
        return "served"
    if len(rows) == 1 and rows[0].get("decision") == "forwarded" and rows[0].get("reason") == "policy_kept":
        return "forwarded_policy_kept"
    return f"row:{[(r.get('decision'), r.get('reason')) for r in rows]}"


def observe_sdk(fx, spy, monkeypatch):
    from llm_router import sdk
    from llm_router.hooks import savings_logger

    monkeypatch.setattr(savings_logger, "log_direct_to_db", lambda **kw: None)
    monkeypatch.setattr(savings_logger, "log_direct_savings", lambda **kw: None)
    try:
        sdk.route(fx.prompt, task_type=fx.task.value, complexity="moderate", timeout=2)
    except sdk.RoutingError:
        return "routing_error"
    except Exception as exc:  # noqa: BLE001
        return type(exc).__name__
    return "returned"


def observe_gateway(fx, spy, monkeypatch):
    from llm_router.gateway import app

    client = fastapi_testclient.TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    r = client.post("/v1/chat/completions",
                    json={"messages": [{"role": "user", "content": fx.prompt}], "task_type": fx.task.value})
    detail = r.json().get("detail", "") if r.headers.get("content-type", "").startswith("application/json") else ""
    if r.status_code == 400 and str(detail).startswith("No providers available"):
        return "http_400"
    return f"http_{r.status_code}:{str(detail)[:80]}"


OBSERVERS = {"router": observe_router, "hook": observe_hook, "proxy": observe_proxy,
             "sdk": observe_sdk, "gateway": observe_gateway}

OBSERVED: list[tuple[str, str, str]] = []


@pytest.mark.parametrize("fx", FIXTURES, ids=repr)
@pytest.mark.parametrize("caller", CALLERS)
def test_observed_behaviour_equals_documented_token(caller, fx, world, monkeypatch):
    documented, _ = parse_doc()
    world(fx)
    # Anti-vacuity: with no map the router and hook chains for this fixture are not empty,
    # so an empty chain below is the map's doing and not an empty configuration.
    assert not rm.map_path().exists()
    assert _router_chain(fx), f"{fx} has an empty router chain without a map"
    assert _hook_chain(fx), f"{fx} has an empty hook chain without a map"
    put(map_doc(fx.rows))
    assert _router_chain(fx) == [] and _hook_chain(fx) == []
    spy = Spy(monkeypatch)
    token = OBSERVERS[caller](fx, spy, monkeypatch)
    OBSERVED.append((caller, fx.name, token))
    assert token == documented[caller], f"{caller} on {fx}: observed {token!r}, documented {documented[caller]!r}"
    assert spy.called == [], f"{caller} on {fx} tried {spy.called}: never an excluded entry"


def test_forty_observations_were_made():
    """Anti-vacuity for the parametrisation: 8 fixtures x 5 callers, each seen once."""
    assert len(FIXTURES) == 8
    if len(OBSERVED) < 40:
        pytest.skip("run together with the observation tests (pytest -k table) to count them")
    assert len({(c, f) for c, f, _ in OBSERVED}) == 40 == len(OBSERVED)


# ── guard fixtures: the map must not empty these chains ──────────────────────

def _guard_cases():
    base = Fx("guard", rows_all("absent"))
    no_row = [r for r in rows_all("absent") if not r["chain_id"].startswith("anthropic/")]
    return {
        "provider with no map row": map_doc(no_row),
        "stale map": map_doc(rows_all("absent"), age_s=map_reader.MAX_AGE_S * 2),
        "other-schema map": map_doc(rows_all("absent"), schema=99),
    }, base


@pytest.mark.parametrize("situation", sorted(GUARDS))
def test_guard_fixtures_do_not_trigger_the_empty_chain_behaviour(situation, world, monkeypatch):
    cases, fx = _guard_cases()
    world(fx)
    unmapped = _router_chain(fx)
    assert unmapped, "empty configuration would pass vacuously"
    put(cases[situation])
    chain = _router_chain(fx)
    assert chain, f"{situation}: the chain was emptied"
    if situation == "provider with no map row":
        assert chain and all(m.startswith("anthropic/") for m in chain), chain   # only the unmapped provider keeps its slot
        assert any(m.startswith("anthropic/") for m in unmapped)
    else:
        assert chain == unmapped
    hook = _hook_chain(fx)
    assert hook, f"{situation}: the hook chain was emptied"
    if situation == "provider with no map row":
        assert {m.provider for m in hook} == {"claude"}, hook      # the unmapped provider keeps its slot


# ── mutants (named negative controls) ────────────────────────────────────────

def _mismatches(world, monkeypatch, callers) -> tuple[int, int]:
    """``(observations, mismatches)`` of *callers* against the documented table, over the 8 fixtures."""
    documented, _ = parse_doc()
    seen = bad = 0
    for fx in FIXTURES:
        world(fx)
        put(map_doc(fx.rows))
        for caller in callers:
            token = OBSERVERS[caller](fx, Spy(monkeypatch), monkeypatch)
            seen += 1
            bad += token != documented[caller]
    return seen, bad


def test_mutant_f4_pre_423_empty_handling_is_caught(world, monkeypatch):
    """apply_to_chain handing back the original chain when everything is filtered (pre-#423)."""
    assert _mismatches(world, monkeypatch, ("router", "hook")) == (16, 0)
    real = map_reader.apply_to_chain

    def pre423(chain, key=lambda m: m, *, reorder=True, view=None):
        out = real(chain, key, reorder=reorder, view=view)
        return out if out else list(chain)

    monkeypatch.setattr(map_reader, "apply_to_chain", pre423)
    seen, bad = _mismatches(world, monkeypatch, ("router", "hook"))
    assert seen == 16 and bad == 16, (seen, bad)


def test_mutant_f8_a_wrong_doc_token_is_caught(tmp_path, world, monkeypatch):
    """The doc table saying http_503 for the gateway must turn the comparison red."""
    text = DOC.read_text().replace("| gateway | every chain entry map-known and disconnected | http_400 |",
                                   "| gateway | every chain entry map-known and disconnected | http_503 |")
    assert text != DOC.read_text()
    broken = tmp_path / "broken.md"
    broken.write_text(text)
    callers, _ = parse_doc(broken)
    assert callers["gateway"] == "http_503"
    fx = FIXTURES[0]
    world(fx)
    put(map_doc(fx.rows))
    observed = observe_gateway(fx, Spy(monkeypatch), monkeypatch)
    assert observed == "http_400" and observed != callers["gateway"]


# ── gateway: image parts are refused, not dropped ────────────────────────────

@pytest.fixture(scope="module")
def client():
    from llm_router.gateway import app

    return fastapi_testclient.TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)


IMG = "iVBORw0KGgo="
IMAGE_BODIES = {
    "/v1/chat/completions": {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "what is this"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + IMG}}]}]},
    "/v1/messages": {"max_tokens": 16, "messages": [{"role": "user", "content": [
        {"type": "text", "text": "what is this"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": IMG}}]}]},
    "/v1/responses": {"input": [{"role": "user", "content": [
        {"type": "input_text", "text": "what is this"},
        {"type": "input_image", "image_url": "data:image/png;base64," + IMG}]}]},
    "/api/chat": {"messages": [{"role": "user", "content": "what is this", "images": [IMG]}]},
    "/api/generate": {"prompt": "what is this", "images": [IMG]},
}


def _completion_routes():
    from llm_router.gateway import app
    from tests.test_r10_refuse_what_cannot_be_served import COMPLETION_ENDPOINTS, NON_COMPLETION_POSTS

    found = sorted({r.path for r in app.routes if "POST" in (getattr(r, "methods", set()) or set())}
                   - set(NON_COMPLETION_POSTS))
    assert found == sorted(COMPLETION_ENDPOINTS), found
    return found


def test_every_completion_route_has_an_image_body():
    routes = _completion_routes()
    assert len(routes) >= 5 and set(routes) == set(IMAGE_BODIES), routes


@pytest.fixture
def router_never_called(monkeypatch):
    calls = []

    async def boom(*a, **k):
        calls.append(a)
        raise AssertionError("route_and_call was reached for an image request")

    monkeypatch.setattr("llm_router.router.route_and_call", boom)
    return calls


@pytest.mark.parametrize("path", sorted(IMAGE_BODIES))
def test_an_image_request_is_refused_not_silently_dropped(path, client, router_never_called):
    from llm_router.gateway import _IMAGES_UNSUPPORTED

    resp = client.post(path, json=dict(IMAGE_BODIES[path]))
    assert resp.status_code == 400, (path, resp.status_code, resp.text[:200])
    assert resp.json()["detail"] == _IMAGES_UNSUPPORTED
    assert router_never_called == []


@pytest.mark.parametrize("path", sorted(IMAGE_BODIES))
def test_a_text_only_request_is_not_refused_as_an_image(path, client, monkeypatch):
    """The other half: text bodies reach the router (stubbed here, no model call)."""
    seen = []

    async def fake(payload):
        seen.append(payload)
        raise ValueError("No providers available (stub)")

    monkeypatch.setattr("llm_router.gateway.route_payload_async", fake, raising=False)
    monkeypatch.setattr("llm_router.route_server.route_payload_async", fake)
    text_body = {
        "/v1/chat/completions": {"messages": [{"role": "user", "content": "hi"}]},
        "/v1/messages": {"max_tokens": 16, "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]},
        "/v1/responses": {"input": "hi"},
        "/api/chat": {"messages": [{"role": "user", "content": "hi"}]},
        "/api/generate": {"prompt": "hi"},
    }[path]
    resp = client.post(path, json=text_body)
    assert resp.status_code == 400 and "No providers available" in resp.json()["detail"]
    assert seen, "the text request never reached the router"


def test_mutant_f6_removing_the_image_refusal_is_caught(client, router_never_called, monkeypatch):
    from llm_router import gateway

    def noop(*values):
        return None

    monkeypatch.setattr(gateway, "_refuse_images_if_present", noop)
    for path in sorted(IMAGE_BODIES):
        client.post(path, json=dict(IMAGE_BODIES[path]))
    assert len(router_never_called) == len(IMAGE_BODIES), (
        "with the refusal removed every image request must reach the router", router_never_called)


# ── D-165 (4): only the NEWEST user turn is refused; history images are flattened ──

_IMG_PART = {
    "/v1/chat/completions": {"type": "image_url", "image_url": {"url": "data:image/png;base64," + IMG}},
    "/v1/messages": {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": IMG}},
    "/v1/responses": {"type": "input_image", "image_url": "data:image/png;base64," + IMG},
}


def _turns(path, *, image_in):
    """A three-turn body (user, assistant, user) with an image in the ``history`` turn or the
    ``newest`` one; the other user turn is plain text."""
    def user(with_image, text):
        if path == "/api/chat":
            return {"role": "user", "content": text, **({"images": [IMG]} if with_image else {})}
        kind = "input_text" if path == "/v1/responses" else "text"
        return {"role": "user", "content": [{"type": kind, "text": text}] + ([_IMG_PART[path]] if with_image else [])}

    turns = [user(image_in == "history", "what is in this picture"),
             {"role": "assistant", "content": "a cat"},
             user(image_in == "newest", "and what colour is it")]
    if path == "/v1/responses":
        return {"input": turns}
    return {"messages": turns, **({"max_tokens": 16} if path == "/v1/messages" else {})}


HISTORY_ROUTES = ("/api/chat", "/v1/chat/completions", "/v1/messages", "/v1/responses")


@pytest.mark.parametrize("path", HISTORY_ROUTES)
def test_an_image_only_in_history_is_flattened_not_refused(path, client, monkeypatch):
    seen = []

    async def fake(payload):
        seen.append(payload)
        raise ValueError("No providers available (stub)")

    monkeypatch.setattr("llm_router.route_server.route_payload_async", fake)
    resp = client.post(path, json=_turns(path, image_in="history"))
    assert resp.status_code == 400 and "No providers available" in resp.json()["detail"], resp.text[:200]
    assert len(seen) == 1, "the request never reached the router"
    assert seen[0]["prompt"] == "and what colour is it"          # the newest turn is the request


@pytest.mark.parametrize("path", HISTORY_ROUTES)
def test_an_image_in_the_newest_turn_after_a_text_history_is_refused(path, client, router_never_called):
    from llm_router.gateway import _IMAGES_UNSUPPORTED

    resp = client.post(path, json=_turns(path, image_in="newest"))
    assert resp.status_code == 400 and resp.json()["detail"] == _IMAGES_UNSUPPORTED, resp.text[:200]
    assert router_never_called == []


def test_a_bare_responses_part_list_is_one_turn_whatever_the_order(client, router_never_called):
    """Role-less parts at the top of a Responses ``input`` are one user turn: an image before
    the text is still in the request."""
    from llm_router.gateway import _IMAGES_UNSUPPORTED

    body = {"input": [_IMG_PART["/v1/responses"], {"type": "input_text", "text": "what is this"}]}
    resp = client.post("/v1/responses", json=body)
    assert resp.status_code == 400 and resp.json()["detail"] == _IMAGES_UNSUPPORTED
    assert router_never_called == []
