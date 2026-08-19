"""TQ-007 — daily spend caps DOWNGRADE to free-local, they do not hard-block.

Signed-off behavior (2026-07):
  cap hit → drop paid providers, keep {ollama, codex, gemini_cli} at $0.
    free-local available      → run free
    none available + hard     → block (BudgetExceededError)
    none available + smart/soft → fall through to Claude (original chain)
  Caps apply whenever configured, independent of enforce mode; enforce mode
  only governs the no-free-fallback branch.

Prior behavior (which this replaces): any daily cap hit raised
BudgetExceededError (warn only if enforce=soft).
"""

from __future__ import annotations

import os
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from llm_router.repo_config import RepoConfig
from llm_router.types import BudgetExceededError, LLMResponse, RoutingProfile, TaskType


class _Cfg:
    llm_router_profile = RoutingProfile.BALANCED
    llm_router_monthly_budget = 0.0
    llm_router_daily_spend_limit = 0.0
    llm_router_escalate_above = 0.0
    llm_router_hard_stop_above = 0.0
    llm_router_claude_subscription = False
    llm_router_gemini_subscription = False
    llm_router_claw_code = False
    llm_router_routing_policy = "balanced"
    llm_router_agentic_model = ""
    codex_daily_limit = 1000
    compaction_mode = "off"
    compaction_threshold = 4000
    prompt_cache_enabled = False
    prompt_cache_min_tokens = 1024
    context_enabled = False
    caveman_mode = "off"
    # All providers used in tests must be "available" — in production
    # _build_and_filter_chain only ever returns available providers, so a free
    # provider surviving the downgrade filter is guaranteed available.
    available_providers = {"openai", "gemini", "ollama", "codex", "gemini_cli", "anthropic"}


def _response(model: str) -> LLMResponse:
    return LLMResponse(
        content=f"ok from {model}", model=model, input_tokens=7, output_tokens=3,
        cost_usd=0.0 if model.split("/")[0] in {"ollama", "codex", "gemini_cli"} else 0.001,
        latency_ms=15.0, provider=model.split("/", 1)[0],
    )


async def _run(chain, *, task_cap=None, total_cap=None, spend=9999.0, enforce="hard",
               prompt="hello", org_specialists=None, classification_data=None):
    """Drive route_and_call with a given chain, cap, over-cap spend, enforce mode."""
    caps = {}
    if task_cap is not None:
        caps["code"] = task_cap
    if total_cap is not None:
        caps["_total"] = total_cap
    repo_cfg = RepoConfig(daily_caps=caps, enforce=enforce)

    active_policy = None
    if org_specialists:
        from llm_router.policy import RoutingPolicy
        active_policy = RoutingPolicy(
            name="test", description="test", specialists=dict(org_specialists)
        )

    route_log = MagicMock()
    mock_log = MagicMock()
    mock_log.bind.return_value = route_log
    tracker = MagicMock()
    tracker.is_healthy.return_value = True

    with ExitStack() as es:
        p = es.enter_context
        # effective_enforce() reads LLM_ROUTER_ENFORCE from the env BEFORE repo config,
        # so pin it here — otherwise an ambient LLM_ROUTER_ENFORCE (set by the dev's
        # llm_router hooks or a sibling test) leaks in and flips the no-free branch.
        p(patch.dict(os.environ, {"LLM_ROUTER_ENFORCE": enforce}))
        p(patch("llm_router.router.get_config", return_value=_Cfg()))
        p(patch("llm_router.router.get_tracker", return_value=tracker))
        p(patch("llm_router.router.log", mock_log))
        p(patch("llm_router.router._native_notify", lambda *a, **k: None))
        p(patch("llm_router.repo_config.effective_config", return_value=repo_cfg))
        p(patch("llm_router.router.cost.get_monthly_spend", new_callable=AsyncMock, return_value=0.0))
        p(patch("llm_router.router.cost.get_daily_spend", new_callable=AsyncMock, return_value=spend))
        p(patch("llm_router.router.cost.get_daily_spend_by_task_type", new_callable=AsyncMock, return_value=spend))
        p(patch("llm_router.policy.load_org_policy", return_value=None))
        p(patch("llm_router.policy.get_active_policy", return_value=active_policy))
        p(patch("llm_router.router.cost.log_usage", new_callable=AsyncMock))
        p(patch("llm_router.router.reserve_envelope", new_callable=AsyncMock, return_value=(None, True, None)))
        p(patch("llm_router.router.commit_envelope", new_callable=AsyncMock))
        p(patch("llm_router.router.release_envelope", new_callable=AsyncMock))
        p(patch("llm_router.semantic_cache.check", new_callable=AsyncMock, return_value=None))
        p(patch("llm_router.semantic_cache.store", new_callable=AsyncMock))
        p(patch("llm_router.router._build_and_filter_chain", new_callable=AsyncMock, return_value=list(chain)))
        p(patch("llm_router.router.providers.call_llm", new_callable=AsyncMock,
                side_effect=lambda model, messages, **kw: _response(model)))
        from llm_router.router import route_and_call
        return await route_and_call(
            TaskType.CODE, prompt, profile=RoutingProfile.BALANCED,
            classification_data=classification_data,
        )


@pytest.mark.asyncio
async def test_cap_hit_with_free_available_downgrades_to_free():
    # chain has both paid (openai) and free (ollama); cap exceeded → run free.
    resp = await _run(["openai/gpt-4o", "ollama/qwen2.5:7b"], task_cap=0.0001, enforce="hard")
    assert resp.provider == "ollama", f"expected downgrade to free, got {resp.model}"
    assert resp.cost_usd == 0.0


@pytest.mark.asyncio
async def test_cap_hit_no_free_hard_blocks():
    # chain is paid-only; cap exceeded + hard → BudgetExceededError.
    with pytest.raises(BudgetExceededError, match="daily limit|Daily spend"):
        await _run(["openai/gpt-4o"], task_cap=0.0001, enforce="hard")


@pytest.mark.asyncio
async def test_cap_hit_no_free_no_claude_smart_blocks():
    # Q-SMART-PAID (RED2-2-01): a paid-only NON-Claude chain (openai) with the cap
    # exceeded under smart must NOT silently call openai — there is no Claude to
    # fall through to, so it BLOCKS. (Was: wrongly asserted resp.provider=='openai'.)
    with pytest.raises(BudgetExceededError):
        await _run(["openai/gpt-4o"], task_cap=0.0001, enforce="smart")


@pytest.mark.asyncio
async def test_cap_hit_no_free_smart_falls_through_to_claude():
    # smart + cap + no free-local, but Claude IS in the chain → fall through to
    # Claude (anthropic), never the non-Claude paid provider.
    resp = await _run(
        ["openai/gpt-4o", "anthropic/claude-sonnet-4-6"], task_cap=0.0001, enforce="smart"
    )
    assert resp.provider == "anthropic", (
        f"smart no-free must fall through to Claude, not a paid non-Claude API: {resp.model}"
    )


@pytest.mark.asyncio
async def test_hard_block_releases_pending_spend_reservation():
    # Q-RESLEAK (RED1-2-02): a hard-block raise must not leak _pending_spend.
    from llm_router import router
    before = router._pending_spend
    with pytest.raises(BudgetExceededError):
        await _run(["openai/gpt-4o"], task_cap=0.0001, enforce="hard")
    assert abs(router._pending_spend - before) < 1e-9, (
        f"reservation leaked: _pending_spend {before} -> {router._pending_spend}"
    )
    # Compounding: 3 hard blocks must not accumulate.
    for _ in range(3):
        with pytest.raises(BudgetExceededError):
            await _run(["openai/gpt-4o"], task_cap=0.0001, enforce="hard")
    assert abs(router._pending_spend - before) < 1e-9, (
        f"reservation leaked across repeated blocks: {router._pending_spend}"
    )


@pytest.mark.asyncio
async def test_no_cap_allows_paid_provider():
    # No cap configured → the paid provider is NOT downgraded or blocked
    # (paid-only chain so the local-first policy can't mask the result).
    resp = await _run(["openai/gpt-4o"], task_cap=None, total_cap=None)
    assert resp.provider == "openai", "no cap must allow the paid provider"


@pytest.mark.asyncio
async def test_total_cap_also_downgrades():
    # ollama is used (not codex/gemini_cli) because those dispatch via a
    # subprocess backend not covered by the call_llm mock; ollama exercises the
    # same downgrade filter through the mocked provider path.
    resp = await _run(["openai/gpt-4o", "ollama/qwen2.5:7b"], total_cap=0.0001, enforce="hard")
    assert resp.provider == "ollama", f"expected downgrade to free ollama, got {resp.model}"


# ── RED1-01 / RED1-02: post-filter injection points must not re-add paid ──────

@pytest.mark.asyncio
async def test_cap_hit_precision_prompt_stays_free():
    """RED1-01: a precision-triggering prompt must NOT re-front openai/gpt-4o-mini
    after the cap-downgrade. The filter now runs last, so dispatch stays free."""
    resp = await _run(
        ["openai/gpt-4o", "ollama/qwen2.5:7b"],
        task_cap=0.0001, enforce="hard",
        prompt="What is 47 * 89? Reply with only the number.",
    )
    assert resp.provider in {"ollama", "codex", "gemini_cli"}, (
        f"RED1-01: precision-tier re-injected a paid provider under cap: {resp.model}"
    )
    assert resp.cost_usd == 0.0


@pytest.mark.asyncio
async def test_cap_hit_org_specialist_stays_free():
    """RED1-02: an org-policy subject specialist (paid) must NOT be re-injected
    ahead of the free-local chain after the cap-downgrade."""
    resp = await _run(
        ["ollama/qwen2.5:7b"],
        task_cap=0.0001, enforce="hard",
        org_specialists={"backend": "openai/gpt-4o"},
        classification_data={"subject": "backend"},
    )
    assert resp.provider in {"ollama", "codex", "gemini_cli"}, (
        f"RED1-02: subject specialist re-injected a paid provider under cap: {resp.model}"
    )
    assert resp.cost_usd == 0.0


@pytest.mark.asyncio
async def test_cap_hit_precision_prompt_no_free_hard_blocks():
    """RED1-01 corollary: precision prompt, cap hit, paid-only chain, hard →
    still blocks (precision-tier cannot smuggle a paid call past a hard cap)."""
    with pytest.raises(BudgetExceededError):
        await _run(
            ["openai/gpt-4o"], task_cap=0.0001, enforce="hard",
            prompt="What is 47 * 89? Reply with only the number.",
        )
