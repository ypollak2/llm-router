"""M3.0 (PLAN D-14 = A): a Q&A task type is never served by a local provider.

Evidence behind the rule: real Q&A prompts, local qwen 4/37 acceptable vs Sonnet 34/37
(PLAN 0.3 [RX]); 65 local Q&A answers in 7 days through MCP ``llm()`` [U]. Code task
types are unchanged: a ``code`` task with the same text can still go local.

Layers pinned:
  1. ``_strip_local_for_qa``: the pure filter.
  2. ``_build_and_filter_chain`` (shared with the proxy): NOT stripped, so the proxy's
     routing is unchanged.
  3. ``route_and_call``: the primary chain is stripped before dispatch (a specialist or
     the bandit can re-add a local model), and so is the emergency BUDGET chain; an
     explicit ``model_override`` is the caller's own pin and is left alone.
"""

from __future__ import annotations

import os
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import tests.test_tq007_daily_cap_downgrade as t
from llm_router import router as router_module
from llm_router.config import RouterConfig, get_config
from llm_router.northstar import QA_TASK_TYPES
from llm_router.router import _QA_STRIP_PROVIDERS, _build_and_filter_chain, _strip_local_for_qa
from llm_router.types import LOCAL_PROVIDERS, Complexity, LLMResponse, RoutingProfile, TaskType

OLLAMA = "ollama/qwen3.5:latest"
# The five task types MCP llm() / llm_* tools can route. Each is Q&A except code.
MCP_QA = [TaskType.QUERY, TaskType.RESEARCH, TaskType.GENERATE, TaskType.ANALYZE]


def test_the_mcp_task_types_match_the_northstar_qa_set():
    """Guard the guard: if QA_TASK_TYPES loses one of these, the loops below prove nothing."""
    assert {tt.value for tt in MCP_QA} <= QA_TASK_TYPES
    assert TaskType.CODE.value not in QA_TASK_TYPES


@pytest.mark.parametrize("task_type", MCP_QA)
def test_strip_removes_ollama_and_keeps_order_for_qa(task_type):
    chain = [OLLAMA, "codex/gpt-5.5", "ollama/hermes3:8b", "openai/gpt-4o"]
    assert _strip_local_for_qa(chain, task_type) == ["codex/gpt-5.5", "openai/gpt-4o"]


def test_strip_removes_openai_compat_local_servers():
    chain = ["openai_compat/llama", "openai/gpt-4o"]
    assert _strip_local_for_qa(chain, TaskType.QUERY) == ["openai/gpt-4o"]


def test_the_strip_set_is_derived_from_types_local_providers():
    # One source of truth: every provider in types.LOCAL_PROVIDERS is stripped, plus
    # openai_compat (local inference server, absent from LOCAL_PROVIDERS). A provider
    # added to types.LOCAL_PROVIDERS must be stripped without touching router.py.
    assert _QA_STRIP_PROVIDERS == LOCAL_PROVIDERS | {"openai_compat"}
    assert not hasattr(router_module, "_LOCAL_PROVIDERS")
    for provider in sorted(LOCAL_PROVIDERS):
        chain = [f"{provider}/m", "openai/gpt-4o"]
        assert _strip_local_for_qa(chain, TaskType.QUERY) == ["openai/gpt-4o"], provider


def test_strip_leaves_code_untouched():
    chain = [OLLAMA, "openai/gpt-4o"]
    assert _strip_local_for_qa(chain, TaskType.CODE) == chain


def test_strip_accepts_a_plain_string_task_type():
    assert _strip_local_for_qa([OLLAMA, "openai/gpt-4o"], "query") == ["openai/gpt-4o"]
    assert _strip_local_for_qa([OLLAMA, "openai/gpt-4o"], "code") == [OLLAMA, "openai/gpt-4o"]


def test_strip_keeps_local_when_it_is_the_only_provider():
    """An Ollama-only install must not get an empty chain ("install Ollama")."""
    assert _strip_local_for_qa([OLLAMA], TaskType.QUERY) == [OLLAMA]


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", [RoutingProfile.BUDGET, RoutingProfile.BALANCED])
async def test_shared_chain_builder_is_unchanged_for_qa(mock_env, profile):
    """The proxy shares ``_build_and_filter_chain`` (proxy/backends.py policy_chain). M3.0
    must not change what it returns: a Q&A chain still holds the local model, exactly as
    at merge base aceb366 (probe: 'what is the capital of France?' got ollama first).
    Fail-before: with the strip inside the builder this assertion is red."""
    with patch.object(RouterConfig, "all_ollama_models", return_value=[OLLAMA]):
        for tt in [*MCP_QA, TaskType.CODE]:
            chain = await _build_and_filter_chain(
                tt, profile, None, "simple", Complexity.SIMPLE, get_config())
            assert OLLAMA in chain, f"{tt.value}/{profile.value}: builder must not strip local: {chain}"


def _response(model: str) -> LLMResponse:
    return LLMResponse(
        content=f"ok from {model}", model=model, input_tokens=7, output_tokens=3,
        cost_usd=0.0, latency_ms=5.0, provider=model.split("/", 1)[0],
    )


async def _run(task_type, chain, model_override=None, emergency=None, fail=(), policy=None,
               classification_data=None):
    """route_and_call with a stubbed chain, so only the post-build stages are under test.

    ``policy`` (a RoutingPolicy) and ``classification_data`` feed the real subject-specialist
    step, which runs after the chain build."""
    called: list[str] = []

    async def fake_call_llm(model, *a, **k):
        called.append(model)
        if model in fail:
            raise RuntimeError(f"stub failure for {model}")
        return _response(model)

    async def fake_build(task_type, profile, *a, **k):
        if profile == RoutingProfile.BUDGET and emergency is not None:
            return list(emergency)
        return list(chain)

    mock_log = MagicMock()
    mock_log.bind.return_value = MagicMock()
    tracker = MagicMock()
    tracker.is_healthy.return_value = True
    env = {"LLM_ROUTER_ENFORCE": "off", "LLM_ROUTER_BANDIT": "off"}

    with ExitStack() as es:
        p = es.enter_context
        p(patch.dict(os.environ, env))
        p(patch("llm_router.router.get_config", return_value=t._Cfg()))
        p(patch("llm_router.router.get_tracker", return_value=tracker))
        p(patch("llm_router.router.log", mock_log))
        p(patch("llm_router.router._native_notify", lambda *a, **k: None))
        for fn in ("get_monthly_spend", "get_daily_spend", "get_daily_spend_by_task_type"):
            p(patch(f"llm_router.router.cost.{fn}", new_callable=AsyncMock, return_value=0.0))
        p(patch("llm_router.router.cost.log_usage", new_callable=AsyncMock))
        p(patch("llm_router.policy.load_org_policy", return_value=None))
        p(patch("llm_router.policy.get_active_policy", return_value=policy))
        p(patch("llm_router.router.reserve_envelope", new_callable=AsyncMock, return_value=(None, True, "k")))
        p(patch("llm_router.router.commit_envelope", new_callable=AsyncMock))
        p(patch("llm_router.router.release_envelope", new_callable=AsyncMock))
        p(patch("llm_router.semantic_cache.check", new_callable=AsyncMock, return_value=None))
        p(patch("llm_router.semantic_cache.store", new_callable=AsyncMock))
        p(patch("llm_router.router._build_and_filter_chain", side_effect=fake_build))
        p(patch("llm_router.router.providers.call_llm", side_effect=fake_call_llm))
        try:
            await router_module.route_and_call(
                task_type, "hello", profile=RoutingProfile.BALANCED,
                complexity_hint="moderate", model_override=model_override,
                classification_data=classification_data,
            )
        finally:
            await router_module.drain_bg_tasks(2.0)
    return called


@pytest.mark.asyncio
@pytest.mark.parametrize("task_type", MCP_QA)
async def test_dispatch_never_calls_a_local_model_for_qa(task_type):
    """The chain handed to dispatch holds a local model first (as a specialist or the
    bandit could leave it). It is never called, and the next provider serves."""
    called = await _run(task_type, [OLLAMA, "openai/gpt-4o"])
    assert called == ["openai/gpt-4o"], called


@pytest.mark.asyncio
async def test_dispatch_still_calls_local_for_code():
    called = await _run(TaskType.CODE, [OLLAMA, "openai/gpt-4o"])
    assert called[:1] == [OLLAMA], called


@pytest.mark.asyncio
async def test_an_explicit_model_override_is_not_rerouted():
    """model_override is the caller's own pin, not routing: it is honored for Q&A too."""
    called = await _run(TaskType.QUERY, [OLLAMA, "openai/gpt-4o"], model_override=OLLAMA)
    assert called[:1] == [OLLAMA], called


def _specialist_policy(subject: str, model: str):
    from llm_router.policy import RoutingPolicy

    return RoutingPolicy(name="m30_specialist", description="test", specialists={subject: model})


@pytest.mark.asyncio
@pytest.mark.parametrize("task_type", MCP_QA)
async def test_a_policy_specialist_cannot_put_a_local_model_back_for_qa(task_type):
    """Position pin. The strip has to run AFTER the subject specialist, because the
    specialist prepends its model to the chain and can name an ``ollama/*`` one. The
    chain builder is stubbed with a clean cloud-only chain, so a strip placed right after
    the build (before the specialist) sees nothing to remove and the specialist then
    puts ollama first: this test is red for that placement (mutation M-e).
    The control test below proves the stub is not hiding the step: the specialist really fires."""
    called = await _run(
        task_type, ["openai/gpt-4o", "openai/gpt-4o-mini"],
        policy=_specialist_policy("general", OLLAMA),
        classification_data={"subject": "general"},
    )
    assert called == ["openai/gpt-4o"], called


@pytest.mark.asyncio
async def test_the_specialist_step_really_puts_a_local_model_first_for_code():
    """Control for the test above: same policy, same stub, a ``code`` task. The specialist
    is applied (ollama is called first), so the Q&A result above is the strip's work and
    not an inert specialist step."""
    called = await _run(
        TaskType.CODE, ["openai/gpt-4o", "openai/gpt-4o-mini"],
        policy=_specialist_policy("general", OLLAMA),
        classification_data={"subject": "general"},
    )
    assert called[:1] == [OLLAMA], called


@pytest.mark.asyncio
async def test_the_emergency_budget_chain_is_stripped_for_qa():
    """The shared builder does not strip, so the emergency BUDGET build strips for itself:
    when the primary chain fails, a Q&A call must not fall back to a local model."""
    called = await _run(
        TaskType.QUERY, ["openai/gpt-4o"], emergency=[OLLAMA, "openai/gpt-4o-mini"],
        fail={"openai/gpt-4o"},
    )
    assert called == ["openai/gpt-4o", "openai/gpt-4o-mini"], called


@pytest.mark.asyncio
async def test_the_emergency_budget_chain_may_still_go_local_for_code():
    called = await _run(
        TaskType.CODE, ["openai/gpt-4o"], emergency=[OLLAMA, "openai/gpt-4o-mini"],
        fail={"openai/gpt-4o"},
    )
    assert called[:2] == ["openai/gpt-4o", OLLAMA], called


@pytest.mark.asyncio
async def test_the_proxy_still_routes_a_qa_step_to_the_local_model(mock_env):
    """Proxy blast radius: ``choose_model`` -> ``policy_chain`` -> the shared builder.
    At merge base aceb366 a Q&A step ('what is the capital of France?') got
    ``ollama/qwen3-coder:30b``; with the strip inside the builder it got ``model=None``.
    M3.0 is MCP-side only, so the proxy must keep the base answer."""
    from llm_router.proxy import backends

    backends._chain_cache.clear()
    try:
        with patch.object(RouterConfig, "all_ollama_models", return_value=["ollama/qwen3-coder:30b"]):
            out = await backends.choose_model("what is the capital of France?", None)
        assert out["task_type"] in QA_TASK_TYPES, out
        assert out["model"] is not None and out["model"].startswith("ollama/"), out
    finally:
        backends._chain_cache.clear()


# --- strip-before-cap order (M3.0 x TQ-007) -------------------------------------------
# The cap step downgrades to {ollama, codex, gemini_cli}. If the Q&A strip ran AFTER it,
# a capped Q&A call would be served by Ollama, which D-14 = A forbids. Driven through the
# TQ-007 harness (tests/test_tq007_daily_cap_downgrade._run) with TaskType.QUERY.
CAP_CHAIN = ["openai/gpt-4o", "ollama/qwen2.5:7b"]


@pytest.mark.asyncio
async def test_a_capped_qa_call_is_not_served_by_ollama_hard_blocks():
    """Cap hit, chain [openai, ollama], Q&A, enforce=hard: the strip removes ollama first,
    so the cap step finds no free provider and blocks. Mutant (strip moved after the cap
    step): the cap step keeps ollama as the free fallback and serves the call -> red."""
    with pytest.raises(t.BudgetExceededError):
        await t._run(CAP_CHAIN, task_cap=0.0001, enforce="hard", task_type=TaskType.QUERY)


@pytest.mark.asyncio
async def test_a_capped_qa_call_is_not_served_by_ollama_smart_blocks_without_claude():
    """smart/soft fall through to Claude only when an anthropic model is in the chain.
    Here there is none, so the capped Q&A call blocks; it is not handed to ollama or openai."""
    with pytest.raises(t.BudgetExceededError):
        await t._run(CAP_CHAIN, task_cap=0.0001, enforce="smart", task_type=TaskType.QUERY)


@pytest.mark.asyncio
async def test_a_capped_qa_call_falls_through_to_claude_not_ollama_when_claude_is_in_chain():
    resp = await t._run(
        ["openai/gpt-4o", "ollama/qwen2.5:7b", "anthropic/claude-sonnet-4-6"],
        task_cap=0.0001, enforce="smart", task_type=TaskType.QUERY,
    )
    assert resp.provider == "anthropic", resp.model


@pytest.mark.asyncio
async def test_the_same_capped_chain_for_code_is_still_served_by_ollama():
    """Control: same chain, same cap, task type CODE. Ollama is the free fallback, so the
    Q&A blocks above are the strip's work, not an inert cap step."""
    resp = await t._run(CAP_CHAIN, task_cap=0.0001, enforce="hard", task_type=TaskType.CODE)
    assert resp.provider == "ollama", resp.model
