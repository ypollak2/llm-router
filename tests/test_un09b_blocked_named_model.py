"""UN-09b S2: in Claude subscription mode, a request that NAMES a Claude model is never served by Claude.

PR #795 (UN-09, 937274c0) made ``route_and_call`` treat a subscription-blocked ``anthropic/*`` name as an
un-named call and drop every ``anthropic/*`` entry right after the chain build. Its review found that no test
pinned the drop: removing it left all 66 tests of ``tests/test_w12_ineligible_replay.py`` and
``tests/test_mcp_qa_no_local.py`` green, because none of their chains carried a Claude entry the router could
serve. Every chain here does, so each test sees a Claude call when the block is gone.

Entry points:

* ``route_and_call`` (the replay's ``router`` door) and ``gateway._route`` (its ``gateway`` door), driven through the
  replay's real chain builder, map and transport stubs (``tests/test_w12_ineligible_replay.py``);
* the MCP ``llm_query`` tool (``model=``) and the MCP ``llm`` tool (``haiku:`` prefix), same world;
* ``route_and_call`` with a stubbed chain per task type (``tests/test_mcp_qa_no_local._run``).

Each door has a positive control: with subscription mode off the same named request IS served by Claude.

Known open limits, NOT closed here (owner decision, UN-09b S1): the emergency BUDGET fallback chain and a policy
specialist naming ``anthropic/*`` can still serve Claude. Both are pinned below as ``xfail(strict=True)``, so the
day one of them is fixed the xfail turns into an XPASS failure and must be promoted to a plain test.
"""
from __future__ import annotations

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import tests.test_mcp_qa_no_local as qa
import tests.test_w12_ineligible_replay as w12
from llm_router import router as router_module
from llm_router.router import NoRoutableModelsError
from llm_router.types import TaskType
from tests import w12_replay_fixture as fx

CLAUDE = w12.SUBSCRIPTION_MODEL        # "anthropic/claude-haiku-4-5", what an Anthropic-format client names
PROMPT = "summarise the retry policy in two lines"


class _ClaudeCalls:
    """Every route to a Claude model, whichever transport the router picks for it: the LiteLLM call
    (``providers.call_llm``) or the Claude CLI (``run_claude``, subscription offload)."""

    def __init__(self) -> None:
        self.llm: list[str] = []
        self.cli: list[str] = []

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
        return await inner(model, messages, **kw)

    monkeypatch.setattr(providers, "call_llm", recording)


def _state(*chain: str, status: str = "connected") -> fx.MapState:
    """Every mapped provider has *status*. ``anthropic`` has no map row, so the map filter leaves it alone;
    whether it survives is decided by the router alone. A complex task: in a Claude Code subscription session
    ``_reorder_for_agent_context`` puts Claude FIRST for complex work (simple / moderate put it last, where only
    a failing chain reaches it, and a failing chain walks the emergency BUDGET chain, a known open limit)."""
    return fx.MapState(statuses={p: status for p in fx.PROVIDERS}, base_chain=chain, task="query",
                       complexity="complex")


#: Claude leads the built chain, so a Claude entry that survives the build is the first one served.
CLAUDE_FIRST = _state(CLAUDE, "gemini/gemini-2.5-flash", "openai/gpt-4o-mini")
#: Every mapped provider disconnected: Claude (no map row) is the only entry the build keeps.
CLAUDE_ONLY = _state(CLAUDE, "gemini/gemini-2.5-flash", "openai/gpt-4o-mini", status="disconnected")


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
STUB_CHAIN = [CLAUDE, "openai/gpt-4o"]


def _qa_run(**kw):
    return w12._run(qa._run(**kw))


@pytest.mark.parametrize("task_type", TASKS)
def test_stubbed_chain_named_claude_is_never_served_in_subscription_mode(calls, task_type):
    called = _qa_run(task_type=task_type, chain=STUB_CHAIN, model_override=CLAUDE, subscription=True)
    assert called == ["openai/gpt-4o"], called
    assert calls.cli == []


@pytest.mark.parametrize("task_type", TASKS)
def test_control_stubbed_chain_unnamed_subscription_call_serves_claude(calls, task_type):
    called = _qa_run(task_type=task_type, chain=STUB_CHAIN, subscription=True)
    assert called == [CLAUDE], called


@pytest.mark.parametrize("task_type", TASKS)
def test_control_stubbed_chain_named_claude_is_served_with_the_block_lifted(calls, task_type):
    called = _qa_run(task_type=task_type, chain=STUB_CHAIN, model_override=CLAUDE, subscription=False)
    assert called == [CLAUDE], called


def test_a_claude_only_chain_refuses_with_the_subscription_notice(calls):
    """Nothing but Claude is left: the router refuses with its own message instead of serving Claude."""
    with pytest.raises(NoRoutableModelsError) as exc:
        _qa_run(task_type=TaskType.QUERY, chain=[CLAUDE], model_override=CLAUDE, subscription=True)
    assert (f"{CLAUDE} is not served in subscription mode (every anthropic/* model is dropped from a named "
            "request), and no other provider is reachable.") in str(exc.value)
    assert calls.cli == []


# ── known open limits (UN-09b S1: owner decision) ─────────────────────────────────────────────────

@pytest.mark.xfail(strict=True, reason="UN-09b S1: owner decision")
def test_known_limit_the_emergency_budget_chain_can_serve_claude(calls):
    """The primary chain fails, so route_and_call walks the emergency BUDGET chain (router.py, "Emergency
    fallback"). That chain is built un-named and the subscription block is not applied to it: Claude serves."""
    called = _qa_run(task_type=TaskType.QUERY, chain=STUB_CHAIN, model_override=CLAUDE, subscription=True,
                     emergency=[CLAUDE], fail={"openai/gpt-4o"})
    assert not [m for m in called if m.startswith("anthropic/")], called


@pytest.mark.xfail(strict=True, reason="UN-09b S1: owner decision")
def test_known_limit_a_policy_specialist_naming_claude_can_serve_it(calls):
    """A policy specialist fronted after the chain build is vetted by ``_front_allowed``, which does not know the
    request named a subscription-blocked model: a specialist naming ``anthropic/*`` serves Claude."""
    called = _qa_run(task_type=TaskType.CODE, chain=STUB_CHAIN, model_override=CLAUDE, subscription=True,
                     policy=qa._specialist_policy("general", "anthropic/claude-sonnet-4-6"),
                     classification_data={"subject": "general"})
    assert not [m for m in called if m.startswith("anthropic/")], called
