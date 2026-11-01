"""UN-09b S2: in Claude subscription mode, a request that NAMES a Claude model is never served by Claude.

PR #795 (UN-09, 937274c0) made ``route_and_call`` treat a subscription-blocked ``anthropic/*`` name as an
un-named call and drop every ``anthropic/*`` entry right after the chain build. Its review found that no test
pinned the drop: removing it left all 66 tests of ``tests/test_w12_ineligible_replay.py`` and
``tests/test_mcp_qa_no_local.py`` green, because none of their chains carried a Claude entry the router could
serve. Every chain here does, so each test sees a Claude call when the block is gone. The chains hold a second
Claude model besides the named one, so a block that dropped only the exact name would be caught too.

Entry points:

* ``route_and_call`` (the replay's ``router`` door) and ``gateway._route`` (its ``gateway`` door), driven through the
  replay's real chain builder, map and transport stubs (``tests/test_w12_ineligible_replay.py``);
* the MCP ``llm_query`` tool (``model=``) and the MCP ``llm`` tool (``haiku:`` prefix), same world;
* ``route_and_call`` with a stubbed chain per task type (``tests/test_mcp_qa_no_local._run``).

Each door has a positive control: with subscription mode off the same named request IS served by Claude.

UN-09b S1 (owner decision 2026-11-01, "make the message true"): the emergency BUDGET fallback chain and a policy
specialist drop ``anthropic/*`` for such a request too, and the semantic cache is not consulted for it (a stored
answer may be Claude's). When nothing in the emergency chain serves (only Claude was in it, or its other entries
fail), the request ends in the base "All models failed" RuntimeError (502 at the HTTP doors, no provider text to
the client, ``error_all_models_failed`` in the ledger) with one fixed clause saying anthropic/* was not tried; a
model was tried and failed, so it is not the no-provider refusal. Each step has an un-named control in which it
does serve Claude.
"""
from __future__ import annotations

import contextlib
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import tests.test_mcp_qa_no_local as qa
import tests.test_w12_ineligible_replay as w12
from llm_router import router as router_module
from llm_router.cost import REASON_ERROR_ALL_FAILED
from llm_router.router import NoRoutableModelsError
from llm_router.types import RoutingProfile, TaskType
from tests import w12_replay_fixture as fx

CLAUDE = w12.SUBSCRIPTION_MODEL        # "anthropic/claude-haiku-4-5", what an Anthropic-format client names
#: A second Claude model in every chain the block is tested on. The block drops the whole provider, not just the
#: named model: a drop of the exact name alone (``m != named``) would leave this one to serve (review S1, mutant B).
SONNET = "anthropic/claude-sonnet-4-6"
PROMPT = "summarise the retry policy in two lines"


class _ClaudeCalls:
    """Every route to a Claude model, whichever transport the router picks for it: the LiteLLM call
    (``providers.call_llm``) or the Claude CLI (``run_claude``, subscription offload)."""

    def __init__(self) -> None:
        self.llm: list[str] = []
        self.cli: list[str] = []
        self.fail: set[str] = set()     # models whose LiteLLM call raises (real-chain controls only)

    @property
    def claude(self) -> list[str]:
        return [m for m in self.llm if m.startswith("anthropic/")] + [f"anthropic/{m}" for m in self.cli]


@pytest.fixture
def calls(monkeypatch) -> _ClaudeCalls:
    """The Claude CLI is never started: ``offload_available`` is False, so ``anthropic/*`` goes through
    ``providers.call_llm`` (stubbed by the harnesses), and a ``run_claude`` call is recorded and fails."""
    rec = _ClaudeCalls()

    async def no_cli(prompt, model=None, **k):
        rec.cli.append(model or "?")
        return SimpleNamespace(success=False, exit_code=127, content="not started (UN-09b)", duration_sec=0.0)

    monkeypatch.setattr(router_module, "claude_offload_available", lambda config: False)
    monkeypatch.setattr(router_module, "run_claude", no_cli)
    return rec


def _record_llm_calls(monkeypatch, rec: _ClaudeCalls) -> None:
    """Wrap the replay's ``providers.call_llm`` stub so every model it is asked for is recorded."""
    from llm_router import providers

    inner = providers.call_llm

    async def recording(model, messages, **kw):
        rec.llm.append(model)
        if model in rec.fail:
            raise RuntimeError(f"stub failure for {model} (UN-09b)")
        return await inner(model, messages, **kw)

    monkeypatch.setattr(providers, "call_llm", recording)


def _state(*chain: str, status: str = "connected") -> fx.MapState:
    """Every mapped provider has *status*. ``anthropic`` has no map row, so the map filter leaves it alone;
    whether it survives is decided by the router alone. A complex task: in a Claude Code subscription session
    ``_reorder_for_agent_context`` puts Claude FIRST for complex work (simple / moderate put it last, where only
    a failing chain reaches it, and a failing chain walks the emergency BUDGET chain, covered at the end of
    this file)."""
    return fx.MapState(statuses={p: status for p in fx.PROVIDERS}, base_chain=chain, task="query",
                       complexity="complex")


#: Two Claude models lead the built chain, so a Claude entry that survives the build is the first one served.
CLAUDE_FIRST = _state(CLAUDE, SONNET, "gemini/gemini-2.5-flash", "openai/gpt-4o-mini")
#: Every mapped provider disconnected: the two Claude models (no map row) are the only entries the build keeps.
CLAUDE_ONLY = _state(CLAUDE, SONNET, "gemini/gemini-2.5-flash", "openai/gpt-4o-mini", status="disconnected")


@pytest.fixture
def world(tmp_path, monkeypatch, calls):
    """The W1-2 replay world (``_world_env`` + ``_stub_transport``: no network, no Ollama, throwaway HOME / db)
    with the Claude-first chain installed. Returns a ``mode(subscription)`` switch."""
    import llm_router.config as config_module

    w12._world_env(monkeypatch, tmp_path)
    w12._stub_transport(monkeypatch)
    _record_llm_calls(monkeypatch, calls)
    w12._use_db(tmp_path / "un09b.db", monkeypatch)
    doc = w12._install(CLAUDE_FIRST, monkeypatch)

    def install(state: fx.MapState) -> None:
        w12._install(state, monkeypatch)

    def mode(subscription: bool) -> None:
        if subscription:   # subscription mode never exports an Anthropic API key
            monkeypatch.setenv("LLM_ROUTER_CLAUDE_SUBSCRIPTION", "true")
            monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        else:              # the block is lifted: an API-key user may name Claude
            monkeypatch.setenv("LLM_ROUTER_CLAUDE_SUBSCRIPTION", "false")
            monkeypatch.setenv("ANTHROPIC_API_KEY", "k-replay")
        config_module._config = None

    yield SimpleNamespace(mode=mode, install=install, doc=doc, root=tmp_path)
    config_module._config = None


def _serve(world, door: str, model: str | None) -> str | None:
    run = w12.DoorRun(door, world.root / "un09b.db", world.root / "proxy_calls.jsonl")
    w12._DRIVERS[door](run, CLAUDE_FIRST, PROMPT, world.doc, model=model)
    return run.served[0][0] if run.served else None


# ── replay doors: route_and_call (router) and gateway._route (gateway) ───────────────────────────

ROUTE_DOORS = ["router", "gateway"]


@pytest.mark.parametrize("door", ROUTE_DOORS)
def test_a_named_claude_model_is_never_served_in_subscription_mode(world, calls, door):
    world.mode(subscription=True)
    served = _serve(world, door, CLAUDE)
    assert served is not None and not served.startswith("anthropic/"), served
    assert calls.claude == [], calls.claude
    assert calls.llm, "no provider was called at all: the test proved nothing"


@pytest.mark.parametrize("door", ROUTE_DOORS)
def test_control_the_unnamed_subscription_chain_does_serve_claude(world, calls, door):
    """Non-vacuity: without a model name the same subscription-mode chain IS served by Claude (it leads the
    chain and the subscription covers it). So the named request's non-Claude answer is the block's work, not a
    chain that never held a servable Claude entry."""
    world.mode(subscription=True)
    assert _serve(world, door, None) == CLAUDE
    assert calls.claude == [CLAUDE], calls.llm


@pytest.mark.parametrize("door", ROUTE_DOORS)
def test_control_the_second_claude_model_is_servable_too(world, calls, door):
    """Non-vacuity for the second Claude model: un-named, with the first Claude model failing, the subscription
    chain is served by ``SONNET`` before any other provider. A block that dropped only the named model would let
    it serve the named request."""
    world.mode(subscription=True)
    calls.fail = {CLAUDE}
    assert _serve(world, door, None) == SONNET
    assert calls.claude == [CLAUDE, SONNET], calls.llm


@pytest.mark.parametrize("door", ROUTE_DOORS)
def test_control_with_the_block_lifted_the_named_claude_model_is_served(world, calls, door):
    """Positive control: subscription mode off, an Anthropic key present, the same named request is a pin and
    Claude serves it."""
    world.mode(subscription=False)
    assert _serve(world, door, CLAUDE) == CLAUDE
    assert calls.claude == [CLAUDE], calls.llm


def test_route_and_call_says_why_the_name_was_not_served(world, monkeypatch):
    """The notice is router.py's own warning, verbatim, naming the model and the dropped provider."""
    world.mode(subscription=True)
    spy = MagicMock()
    monkeypatch.setattr(router_module, "log", spy)
    _serve(world, "router", CLAUDE)
    notices = [c.args for c in spy.warning.call_args_list
               if c.args and "blocked in subscription mode" in str(c.args[0])]
    assert notices == [(
        "model_override %r blocked in subscription mode — routing as an un-named call without %s",
        CLAUDE, "anthropic/",
    )], spy.warning.call_args_list


def test_route_and_call_refuses_when_only_the_named_claude_model_is_left(world, calls):
    """The map disconnects every other provider, so the built chain is Claude alone. The named request is refused
    with router.py's own message (verbatim substring) and nothing is called; the un-named request is served by
    Claude (non-vacuity: the chain really holds a servable Claude entry)."""
    from llm_router.router import route_and_call
    from llm_router.types import Complexity

    world.mode(subscription=True)
    world.install(CLAUDE_ONLY)
    with pytest.raises(NoRoutableModelsError) as exc:
        w12._run(route_and_call(TaskType.QUERY, PROMPT, complexity_hint=Complexity.COMPLEX, model_override=CLAUDE))
    assert (f"{CLAUDE} is not served in subscription mode (every anthropic/* model is dropped from a named "
            "request), and no other provider is reachable.") in str(exc.value)
    assert calls.llm == [] and calls.cli == [], (calls.llm, calls.cli)
    served = w12._run(route_and_call(TaskType.QUERY, PROMPT, complexity_hint=Complexity.COMPLEX))
    assert served.model == CLAUDE and calls.claude == [CLAUDE], (served.model, calls.llm)
    calls.fail = {CLAUDE}
    served = w12._run(route_and_call(TaskType.QUERY, PROMPT, complexity_hint=Complexity.COMPLEX))
    assert served.model == SONNET, (served.model, calls.llm)


# ── MCP doors: llm_query(model=...) and llm("haiku: ...") ───────────────────────────────────────

def _mcp(coro):
    return w12._run(coro)


def test_mcp_llm_query_named_claude_is_never_served_in_subscription_mode(world, calls):
    from llm_router.tools import text

    world.mode(subscription=True)
    _mcp(text.llm_query(prompt=PROMPT, ctx=MagicMock(), complexity="complex", model=CLAUDE))
    assert calls.llm and calls.claude == [], calls.llm


def test_control_mcp_llm_query_unnamed_subscription_call_serves_claude(world, calls):
    """Non-vacuity for the MCP door: the same call without a model name is served by Claude."""
    from llm_router.tools import text

    world.mode(subscription=True)
    _mcp(text.llm_query(prompt=PROMPT, ctx=MagicMock(), complexity="complex"))
    assert calls.claude == [CLAUDE], calls.llm


def test_control_mcp_llm_query_named_claude_is_served_with_the_block_lifted(world, calls):
    from llm_router.tools import text

    world.mode(subscription=False)
    _mcp(text.llm_query(prompt=PROMPT, ctx=MagicMock(), complexity="complex", model=CLAUDE))
    assert calls.claude == [CLAUDE], calls.llm


def test_mcp_llm_claude_prefix_is_refused_in_subscription_mode(world, calls):
    """The MCP ``llm`` tool names Claude by prefix. In subscription mode it answers with its refusal and calls
    no model at all."""
    from llm_router.tools import consolidated

    world.mode(subscription=True)
    out = _mcp(consolidated.llm(prompt=f"haiku: {PROMPT}", ctx=MagicMock()))
    assert out == ("[llm_router] `haiku:` forces Claude haiku: not routed "
                   "through the API door; handle this request yourself at that tier."), out
    assert calls.llm == [] and calls.cli == [], (calls.llm, calls.cli)


def test_control_mcp_llm_claude_prefix_is_served_with_the_block_lifted(world, calls):
    from llm_router.proxy.tiers import tier_model
    from llm_router.tools import consolidated

    world.mode(subscription=False)
    _mcp(consolidated.llm(prompt=f"haiku: {PROMPT}", ctx=MagicMock()))
    assert tier_model("haiku"), "the bundled tier policy has no haiku tier: the control proves nothing"
    assert calls.claude == [f"anthropic/{tier_model('haiku')}"], calls.llm


# ── route_and_call with a stubbed chain, per task type (tests/test_mcp_qa_no_local._run) ──────────

TASKS = [*qa.MCP_QA, TaskType.CODE]
STUB_CHAIN = [CLAUDE, SONNET, "openai/gpt-4o"]


def _qa_run(**kw):
    return w12._run(qa._run(**kw))


@pytest.mark.parametrize("task_type", TASKS)
def test_stubbed_chain_named_claude_is_never_served_in_subscription_mode(calls, task_type):
    called = _qa_run(task_type=task_type, chain=STUB_CHAIN, model_override=CLAUDE, subscription=True)
    assert not [m for m in called if m.startswith("anthropic/")], called
    assert called == ["openai/gpt-4o"], called
    assert calls.cli == []


@pytest.mark.parametrize("task_type", TASKS)
def test_control_stubbed_chain_unnamed_subscription_call_serves_claude(calls, task_type):
    called = _qa_run(task_type=task_type, chain=STUB_CHAIN, subscription=True)
    assert called == [CLAUDE], called
    called = _qa_run(task_type=task_type, chain=STUB_CHAIN, subscription=True, fail={CLAUDE})
    assert called == [CLAUDE, SONNET], called      # the second Claude model is servable too


@pytest.mark.parametrize("task_type", TASKS)
def test_control_stubbed_chain_named_claude_is_served_with_the_block_lifted(calls, task_type):
    called = _qa_run(task_type=task_type, chain=STUB_CHAIN, model_override=CLAUDE, subscription=False)
    assert called == [CLAUDE], called


def test_a_claude_only_chain_refuses_with_the_subscription_notice(calls):
    """Nothing but Claude is left: the router refuses with its own message instead of serving Claude."""
    with pytest.raises(NoRoutableModelsError) as exc:
        _qa_run(task_type=TaskType.QUERY, chain=[CLAUDE, SONNET], model_override=CLAUDE, subscription=True)
    assert (f"{CLAUDE} is not served in subscription mode (every anthropic/* model is dropped from a named "
            "request), and no other provider is reachable.") in str(exc.value)
    assert calls.cli == []


# ── UN-09b S1: the emergency BUDGET chain, a policy specialist, the semantic cache (owner decision 2026-11-01) ─

MINI = "openai/gpt-4o-mini"
#: Provider exception text can echo the prompt (F76/F87/F91): it must never reach an HTTP client body.
SEED = "SEEDED-UN09B-PROVIDER-ECHO-do-not-return"
NOT_TRIED = " anthropic/* was not tried: the named model is blocked in subscription mode."


@contextlib.contextmanager
def _stubbed(chain, emergency=None, fail=(), subscription=True, cached=None):
    """``tests/test_mcp_qa_no_local._run``'s patches as a context, so any door can drive route_and_call: a stubbed
    chain build (``emergency`` = the BUDGET chain), a stubbed provider call (a model in ``fail`` raises with SEED in
    its text), a semantic cache that returns ``cached``, and a recorded route-error ledger write."""
    rec = SimpleNamespace(called=[], cache_checks=0, route_error=AsyncMock())

    async def fake_call_llm(model, *a, **k):
        rec.called.append(model)
        if model in fail:
            raise RuntimeError(f"stub failure for {model}: upstream echoed {SEED}")
        return qa._response(model)

    async def fake_build(task_type, profile, *a, **k):
        if profile == RoutingProfile.BUDGET and emergency is not None:
            return list(emergency)
        return list(chain)

    async def fake_check(*a, **k):
        rec.cache_checks += 1
        return cached

    mock_log = MagicMock()
    mock_log.bind.return_value = MagicMock()
    tracker = MagicMock()
    tracker.is_healthy.return_value = True
    with contextlib.ExitStack() as es:
        p = es.enter_context
        p(patch.dict(os.environ, {"LLM_ROUTER_ENFORCE": "off", "LLM_ROUTER_BANDIT": "off"}))
        p(patch("llm_router.router.get_config", return_value=qa._Cfg(subscription)))
        p(patch("llm_router.router.get_tracker", return_value=tracker))
        p(patch("llm_router.router.log", mock_log))
        p(patch("llm_router.router._native_notify", lambda *a, **k: None))
        for fn in ("get_monthly_spend", "get_daily_spend", "get_daily_spend_by_task_type"):
            p(patch(f"llm_router.router.cost.{fn}", new_callable=AsyncMock, return_value=0.0))
        p(patch("llm_router.router.cost.log_usage", new_callable=AsyncMock))
        p(patch("llm_router.router.cost.log_route_error", rec.route_error))
        p(patch("llm_router.policy.load_org_policy", return_value=None))
        p(patch("llm_router.policy.get_active_policy", return_value=None))
        p(patch("llm_router.router.reserve_envelope", new_callable=AsyncMock, return_value=(None, True, "k")))
        p(patch("llm_router.router.commit_envelope", new_callable=AsyncMock))
        p(patch("llm_router.router.release_envelope", new_callable=AsyncMock))
        p(patch("llm_router.semantic_cache.check", side_effect=fake_check))
        p(patch("llm_router.semantic_cache.store", new_callable=AsyncMock))
        p(patch("llm_router.router._build_and_filter_chain", side_effect=fake_build))
        p(patch("llm_router.router.providers.call_llm", side_effect=fake_call_llm))
        yield rec


def _route(model_override=None):
    async def go():
        try:
            return await router_module.route_and_call(
                TaskType.QUERY, PROMPT, profile=RoutingProfile.BALANCED, complexity_hint="moderate",
                model_override=model_override)
        finally:
            await router_module.drain_bg_tasks(2.0)
    return w12._run(go())


def test_an_emergency_chain_of_only_claude_models_ends_in_the_base_failure(calls):
    """The primary chain fails, so route_and_call walks the emergency BUDGET chain (router.py, "Emergency
    fallback"), built un-named. It holds a Claude model other than the named one (a drop of the exact name only
    would leave it to serve). Nothing is left after the drop: the base RuntimeError with the fixed clause (a model
    was tried and failed, so not the no-provider refusal), and the base ledger reason."""
    with _stubbed(STUB_CHAIN, emergency=[SONNET], fail={"openai/gpt-4o"}) as rec:
        with pytest.raises(RuntimeError) as exc:
            _route(CLAUDE)
    assert type(exc.value) is RuntimeError, type(exc.value)
    assert str(exc.value).startswith(f"All models failed for query/balanced.{NOT_TRIED} Last error: "), str(exc.value)
    assert [c.kwargs["reason"] for c in rec.route_error.await_args_list] == [REASON_ERROR_ALL_FAILED]
    assert rec.called == ["openai/gpt-4o"], rec.called
    assert calls.cli == []


def test_an_emergency_chain_whose_other_entries_fail_ends_in_the_base_failure(calls):
    """Claude models dropped, the remaining entry fails too: the base RuntimeError ("All models failed"), with one
    fixed clause saying why anthropic/* was not tried. The ledger reason is the base one."""
    with _stubbed(STUB_CHAIN, emergency=[CLAUDE, SONNET, MINI], fail={"openai/gpt-4o", MINI}) as rec:
        with pytest.raises(RuntimeError) as exc:
            _route(CLAUDE)
    assert type(exc.value) is RuntimeError, type(exc.value)
    msg = str(exc.value)
    assert msg.startswith(f"All models failed for query/balanced.{NOT_TRIED} Last error: "), msg
    assert router_module._route_error_reason(exc.value) == REASON_ERROR_ALL_FAILED
    assert [c.kwargs["reason"] for c in rec.route_error.await_args_list] == [REASON_ERROR_ALL_FAILED]
    assert rec.called == ["openai/gpt-4o", MINI], rec.called


def test_a_blocked_name_whose_emergency_chain_holds_no_claude_gets_no_clause(calls):
    """The clause says the block removed something. A BUDGET chain with no Claude model loses nothing to the block:
    the base text, unchanged, for the blocked named request too."""
    with _stubbed(STUB_CHAIN, emergency=[MINI], fail={"openai/gpt-4o", MINI}) as rec:
        with pytest.raises(RuntimeError) as exc:
            _route(CLAUDE)
    assert str(exc.value).startswith("All models failed for query/balanced. Last error: "), str(exc.value)
    assert rec.called == ["openai/gpt-4o", MINI], rec.called


def test_control_an_unnamed_failure_has_no_subscription_clause(calls):
    """The clause is the block's: an un-named request whose chains all fail gets the base text unchanged."""
    with _stubbed(["openai/gpt-4o"], emergency=[MINI], fail={"openai/gpt-4o", MINI}):
        with pytest.raises(RuntimeError) as exc:
            _route(None)
    assert str(exc.value).startswith("All models failed for query/balanced. Last error: "), str(exc.value)


def test_the_emergency_budget_chain_still_serves_its_non_claude_entries_for_a_blocked_name(calls):
    """Only the blocked provider leaves the emergency chain: its other entries still serve the named request."""
    with _stubbed(STUB_CHAIN, emergency=[CLAUDE, SONNET, MINI], fail={"openai/gpt-4o"}) as rec:
        served = _route(CLAUDE)
    assert served.model == MINI and rec.called == ["openai/gpt-4o", MINI], (served.model, rec.called)


def test_control_the_unnamed_emergency_budget_chain_serves_claude_in_subscription_mode(calls):
    """Non-vacuity: un-named, the same emergency chain IS served by Claude in subscription mode (existing
    behaviour, unchanged)."""
    with _stubbed(["openai/gpt-4o"], emergency=[SONNET], fail={"openai/gpt-4o"}) as rec:
        served = _route(None)
    assert served.model == SONNET and rec.called == ["openai/gpt-4o", SONNET], (served.model, rec.called)


# The HTTP doors (F76/F87/F91): a RouterInputError's text goes to the client as a 400, anything else is a 502 with
# the class and a fixed message. The emergency path must keep the provider's text out of both.

def _gateway_route(model):
    from fastapi.testclient import TestClient

    from llm_router.gateway import app
    r = TestClient(app, base_url="http://127.0.0.1").post(
        "/route", json={"prompt": PROMPT, "model": model, "task_type": "query", "complexity": "moderate"})
    return r.status_code, r.text


def _route_server_route(model):
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer

    import llm_router.route_server as route_server
    srv = ThreadingHTTPServer(("127.0.0.1", 0), route_server.make_handler())
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        body = json.dumps({"prompt": PROMPT, "model": model, "task_type": "query",
                           "complexity": "moderate"}).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}/route", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)


HTTP_DOORS = {"gateway": _gateway_route, "route_server": _route_server_route}


@pytest.mark.parametrize("door", sorted(HTTP_DOORS))
def test_http_door_a_failed_emergency_chain_is_a_502_without_provider_text(calls, door):
    pytest.importorskip("fastapi")
    with _stubbed(STUB_CHAIN, emergency=[CLAUDE, SONNET, MINI], fail={"openai/gpt-4o", MINI}) as rec:
        status, body = HTTP_DOORS[door](CLAUDE)
    assert status == 502 and SEED not in body and "RuntimeError: routing failed" in body, (status, body)
    assert rec.called == ["openai/gpt-4o", MINI], rec.called
    assert [c.kwargs["reason"] for c in rec.route_error.await_args_list] == [REASON_ERROR_ALL_FAILED]


@pytest.mark.parametrize("door", sorted(HTTP_DOORS))
def test_http_door_an_all_claude_emergency_chain_is_a_502_without_provider_text(calls, door):
    """Nothing is left after the drop, but a model was tried and failed: a retryable 502, as on base."""
    pytest.importorskip("fastapi")
    with _stubbed(STUB_CHAIN, emergency=[SONNET], fail={"openai/gpt-4o"}) as rec:
        status, body = HTTP_DOORS[door](CLAUDE)
    assert status == 502 and SEED not in body and "RuntimeError: routing failed" in body, (status, body)
    assert rec.called == ["openai/gpt-4o"], rec.called
    assert [c.kwargs["reason"] for c in rec.route_error.await_args_list] == [REASON_ERROR_ALL_FAILED]


# ── the semantic cache (review S4) ──────────────────────────────────────────────────────────────

def test_a_blocked_named_request_is_not_served_a_cached_claude_answer(calls):
    """The cache holds a Claude answer for this prompt. After the block the request is un-named, so without the skip
    the cache lookup runs and returns Claude's answer (semantic_cache.check does not filter by model)."""
    with _stubbed(["openai/gpt-4o"], cached=qa._response(CLAUDE)) as rec:
        served = _route(CLAUDE)
    assert served.model == "openai/gpt-4o" and rec.cache_checks == 0, (served.model, rec.cache_checks)


def test_control_an_unnamed_request_is_served_the_cached_claude_answer(calls):
    with _stubbed(["openai/gpt-4o"], cached=qa._response(CLAUDE)) as rec:
        served = _route(None)
    assert served.model == CLAUDE and rec.called == [] and rec.cache_checks == 1, (served.model, rec.called)


# ── a policy specialist ─────────────────────────────────────────────────────────────────────────

def test_a_policy_specialist_naming_claude_is_not_fronted_for_a_blocked_name(calls):
    """A policy specialist fronted after the chain build is vetted by ``_front_allowed``, which now also knows the
    request named a subscription-blocked model: a specialist naming ``anthropic/*`` is not fronted."""
    called = _qa_run(task_type=TaskType.CODE, chain=STUB_CHAIN, model_override=CLAUDE, subscription=True,
                     policy=qa._specialist_policy("general", SONNET),
                     classification_data={"subject": "general"})
    assert called == ["openai/gpt-4o"], called


def test_control_an_unnamed_call_fronts_the_claude_specialist_in_subscription_mode(calls):
    """Non-vacuity: un-named, the same policy specialist IS fronted and serves (existing behaviour)."""
    called = _qa_run(task_type=TaskType.CODE, chain=["openai/gpt-4o"], subscription=True,
                     policy=qa._specialist_policy("general", SONNET),
                     classification_data={"subject": "general"})
    assert called == [SONNET], called


# ── QHYG3: the main-chain refusal leaves a readable route-error row ─────────────────────────────────

def test_the_main_chain_refusal_of_a_blocked_named_model_writes_a_dedicated_ledger_reason(calls):
    from llm_router.cost import REASON_ERROR_NAMED_MODEL_BLOCKED

    assert REASON_ERROR_NAMED_MODEL_BLOCKED == "error_named_model_blocked"
    clock = iter([100.0])      # the first read is the call start; every later read is 100 s on (a start taken at the refusal gives 0)
    with _stubbed([CLAUDE, SONNET]) as rec:
        with patch("llm_router.router.time.monotonic", side_effect=lambda: next(clock, 200.0)):
            with pytest.raises(NoRoutableModelsError):
                _route(CLAUDE)
    assert rec.called == [], rec.called
    latency = rec.route_error.await_args.kwargs["latency_ms"]
    assert latency == pytest.approx(100_000.0), latency
    assert [c.kwargs["reason"] for c in rec.route_error.await_args_list] == [REASON_ERROR_NAMED_MODEL_BLOCKED]
    assert rec.route_error.await_args.kwargs["attempted_model"] is None


def test_control_an_unblocked_empty_chain_refusal_writes_no_blocked_name_row(calls):
    from llm_router.cost import REASON_ERROR_NAMED_MODEL_BLOCKED

    with _stubbed([], subscription=False) as rec:
        with pytest.raises(NoRoutableModelsError):
            _route(None)
    assert REASON_ERROR_NAMED_MODEL_BLOCKED not in [c.kwargs["reason"] for c in rec.route_error.await_args_list]
