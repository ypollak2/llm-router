"""SECTION 1 — Chain-building correctness audit for ``_build_and_filter_chain``.

These tests exercise the real function (no mocking of the function itself) with a
lightweight config double, isolating only the ambient/global lookups (dynamic
routing cache, benchmark penalty DB, Codex/Gemini CLI availability, active-agent
state, org policy file) that would otherwise make results depend on the machine
running the suite.

Written for the audit described in the task brief:
  1. explicit model pin beats Ollama/Codex/Claude injection + every reorder pass
     (regression test for the "Final pin re-assert" fix at the end of
     ``_build_and_filter_chain``).
  2. ``provider_override`` behaviour — including a bug found while writing this
     suite: unlike the model pin, the provider pin is NOT re-asserted after the
     later reorder passes, so it can still be buried (see bugs section of
     REPORT_A.md).
  3. PREMIUM / REASONING bypass the pin + Ollama-injection gate.
  4. ``agentic_model`` only applies to ``AGENTIC_TASK_TYPES``.
  5. ``block_providers`` / ``block_models`` / ``allow_models`` filtering — including
     a second bug found here: these filters run BEFORE the Ollama/Codex/Gemini-CLI
     injection steps, so blocking a free-tier provider does not stop it from being
     injected back in afterwards.
  6. final dedup step keeps the FIRST occurrence of a duplicated model.
  7. media task types never receive text-model (Ollama/Codex/Gemini CLI) injections.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from llm_router.profiles import provider_from_model
from llm_router.repo_config import RepoConfig, TaskRouteOverride
from llm_router.router import AGENTIC_TASK_TYPES, MEDIA_TASK_TYPES, _build_and_filter_chain
from llm_router.types import Complexity, RoutingProfile, TaskType


# ── Fake config double ──────────────────────────────────────────────────────
# Mirrors the shape of llm_router.config.RouterConfig that _build_and_filter_chain
# actually reads, without touching real env vars / ~/.llm-router files so results
# don't depend on the machine running the suite.

class _FakeConfig:
    def __init__(
        self,
        *,
        available_providers: set[str],
        ollama_models: list[str] | None = None,
        openai_compat_models: list[str] | None = None,
        claude_subscription: bool = False,
        gemini_subscription: bool = False,
        claw_code: bool = False,
        routing_policy: str = "balanced",
        agentic_model: str = "",
    ) -> None:
        self.available_providers = available_providers
        self._ollama_models = ollama_models or []
        self._openai_compat_models = openai_compat_models or []
        self.llm_router_claude_subscription = claude_subscription
        self.llm_router_gemini_subscription = gemini_subscription
        self.llm_router_claw_code = claw_code
        self.llm_router_routing_policy = routing_policy
        self.llm_router_agentic_model = agentic_model

    def all_ollama_models(self) -> list[str]:
        return list(self._ollama_models)

    def all_openai_compat_models(self) -> list[str]:
        return list(self._openai_compat_models)


@pytest.fixture(autouse=True)
def isolate_chain_build(monkeypatch):
    """Neutralise every ambient/global lookup _build_and_filter_chain touches.

    Individual tests override specific pieces (get_repo_config, is_codex_available,
    is_gemini_cli_available, get_active_agent) to exercise the behaviour under test.
    """
    monkeypatch.setattr("llm_router.dynamic_routing.get_dynamic_model_chain", lambda *a, **k: None)
    monkeypatch.setattr("llm_router.cost.get_model_failure_rates", AsyncMock(return_value={}))
    monkeypatch.setattr("llm_router.cost.get_model_latency_stats", AsyncMock(return_value={}))
    monkeypatch.setattr("llm_router.cost.get_model_acceptance_scores", AsyncMock(return_value={}))
    monkeypatch.setattr("llm_router.claude_usage.get_claude_pressure", lambda: 0.0)
    monkeypatch.setattr("llm_router.policy.load_org_policy", lambda *a, **k: None)
    monkeypatch.setattr("llm_router.router.get_repo_config", lambda *a, **k: RepoConfig())
    monkeypatch.setattr("llm_router.router.is_codex_available", lambda: False)
    monkeypatch.setattr("llm_router.router.is_gemini_cli_available", lambda: False)
    monkeypatch.setattr("llm_router.router.get_active_agent", lambda: None)
    monkeypatch.setattr(
        "llm_router.quota_balance.get_provider_pressures",
        AsyncMock(return_value={"claude": 0.5, "gemini_cli": 0.5, "codex": 0.5}),
    )


async def _chain(
    task_type: TaskType,
    profile: RoutingProfile,
    config: _FakeConfig,
    *,
    complexity: Complexity = Complexity.MODERATE,
) -> list[str]:
    return await _build_and_filter_chain(
        task_type, profile, None, complexity, complexity, config,
    )


# ── 1. Explicit model pin beats every reorder pass (regression: bug #2) ─────


@pytest.mark.asyncio
async def test_explicit_pin_wins_over_ollama_codex_claude_and_every_reorder(monkeypatch):
    """Regression test for the "Final pin re-assert" fix.

    Forces ALL of: Ollama injection, Codex injection, agent-context tier
    grouping, a non-balanced user routing policy, AND the QUOTA_BALANCED
    reorder pass to run in the same call, with an explicit per-task model
    pin set. The pin must still land at index 0.
    """
    monkeypatch.setattr("llm_router.router.is_codex_available", lambda: True)
    monkeypatch.setattr("llm_router.router.get_active_agent", lambda: "claude_code")
    monkeypatch.setattr(
        "llm_router.router.get_repo_config",
        lambda *a, **k: RepoConfig(
            routing={"analyze": TaskRouteOverride(model="openai/gpt-4o-mini-PINNED")}
        ),
    )
    config = _FakeConfig(
        available_providers={"anthropic", "openai", "deepseek", "zhipu", "gemini"},
        ollama_models=["ollama/qwen3.5:latest"],
        routing_policy="cost",
    )

    chain = await _chain(TaskType.ANALYZE, RoutingProfile.QUOTA_BALANCED, config)

    assert chain, "chain must not be empty"
    assert chain[0] == "openai/gpt-4o-mini-PINNED"
    # Sanity: prove the other injections actually ran (otherwise this test
    # would pass trivially without ever exercising the reorder passes).
    assert any(provider_from_model(m) == "ollama" for m in chain[1:])
    assert any(provider_from_model(m) == "codex" for m in chain[1:])
    assert any(provider_from_model(m) == "anthropic" for m in chain[1:])


@pytest.mark.asyncio
async def test_explicit_pin_not_duplicated_when_already_in_chain(monkeypatch):
    """A pin that names a model already present in the static chain should
    still end up at index 0 exactly once (not duplicated)."""
    monkeypatch.setattr(
        "llm_router.router.get_repo_config",
        lambda *a, **k: RepoConfig(routing={"code": TaskRouteOverride(model="openai/gpt-4o")}),
    )
    config = _FakeConfig(available_providers={"anthropic", "openai", "deepseek"})
    chain = await _chain(TaskType.CODE, RoutingProfile.BALANCED, config)
    assert chain[0] == "openai/gpt-4o"
    assert chain.count("openai/gpt-4o") == 1


# ── 2. provider_override behaviour ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_provider_override_reorders_chain_when_no_model_pin(monkeypatch):
    """With no other reorder pass active, a provider pin promotes that
    provider's models to the front, preserving relative order otherwise."""
    monkeypatch.setattr(
        "llm_router.router.get_repo_config",
        lambda *a, **k: RepoConfig(routing={"code": TaskRouteOverride(provider="deepseek")}),
    )
    config = _FakeConfig(available_providers={"anthropic", "openai", "deepseek"})
    chain = await _chain(TaskType.CODE, RoutingProfile.BALANCED, config)
    assert chain[0] == "deepseek/deepseek-chat"


@pytest.mark.asyncio
async def test_provider_override_survives_later_reorders(monkeypatch):
    """Regression test for the provider-pin sibling of the "Final pin
    re-assert" fix: like the model pin, ``provider_override`` must also
    survive Ollama injection and agent-context reordering, not just the
    single application near the top of ``_build_and_filter_chain``.
    """
    monkeypatch.setattr(
        "llm_router.router.get_repo_config",
        lambda *a, **k: RepoConfig(routing={"code": TaskRouteOverride(provider="deepseek")}),
    )
    monkeypatch.setattr("llm_router.router.get_active_agent", lambda: "claude_code")
    config = _FakeConfig(
        available_providers={"anthropic", "openai", "deepseek"},
        ollama_models=["ollama/qwen3.5:latest"],
    )
    chain = await _chain(TaskType.CODE, RoutingProfile.BALANCED, config)

    # A deepseek model must lead, ahead of the freshly-injected Ollama model.
    assert provider_from_model(chain[0]) == "deepseek"
    assert "deepseek/deepseek-chat" in chain
    assert "ollama/qwen3.5:latest" in chain
    assert chain.index("deepseek/deepseek-chat") < chain.index("ollama/qwen3.5:latest")


# ── 3. PREMIUM / REASONING bypass the pin + Ollama-injection gate ───────────


@pytest.mark.asyncio
async def test_pin_applies_for_balanced_but_not_premium_or_reasoning(monkeypatch):
    monkeypatch.setattr(
        "llm_router.router.get_repo_config",
        lambda *a, **k: RepoConfig(
            routing={"code": TaskRouteOverride(model="pinned/should-not-apply")}
        ),
    )
    config = _FakeConfig(available_providers={"anthropic", "openai", "deepseek"})

    balanced_chain = await _chain(TaskType.CODE, RoutingProfile.BALANCED, config)
    assert balanced_chain[0] == "pinned/should-not-apply"

    premium_chain = await _chain(
        TaskType.CODE, RoutingProfile.PREMIUM, config, complexity=Complexity.COMPLEX
    )
    assert "pinned/should-not-apply" not in premium_chain

    reasoning_chain = await _chain(
        TaskType.CODE, RoutingProfile.REASONING, config, complexity=Complexity.DEEP_REASONING
    )
    assert "pinned/should-not-apply" not in reasoning_chain


@pytest.mark.asyncio
async def test_ollama_injection_also_gated_for_premium(monkeypatch):
    """The cheap-tier gate covers Ollama injection too, not just the pin."""
    config = _FakeConfig(
        available_providers={"anthropic", "openai", "deepseek"},
        ollama_models=["ollama/qwen3.5:latest"],
    )
    premium_chain = await _chain(
        TaskType.CODE, RoutingProfile.PREMIUM, config, complexity=Complexity.COMPLEX
    )
    # qwen3.5:latest was never part of the static PREMIUM chain and is only
    # ever added via the gated injection step, so its absence proves the gate held.
    assert "ollama/qwen3.5:latest" not in premium_chain


# ── 4. agentic_model only applies to AGENTIC_TASK_TYPES ─────────────────────


@pytest.mark.asyncio
async def test_agentic_model_applies_only_to_agentic_task_types(monkeypatch):
    assert TaskType.ANALYZE in AGENTIC_TASK_TYPES
    assert TaskType.CODE not in AGENTIC_TASK_TYPES
    monkeypatch.setattr(
        "llm_router.router.get_repo_config",
        lambda *a, **k: RepoConfig(agentic_model="agentic/pinned-model"),
    )
    config = _FakeConfig(available_providers={"anthropic", "openai", "deepseek"})

    analyze_chain = await _chain(TaskType.ANALYZE, RoutingProfile.BALANCED, config)
    assert analyze_chain[0] == "agentic/pinned-model"

    code_chain = await _chain(TaskType.CODE, RoutingProfile.BALANCED, config)
    assert "agentic/pinned-model" not in code_chain


# ── 5. block_providers / block_models / allow_models ─────────────────────────


@pytest.mark.asyncio
async def test_block_providers_removes_matching_models(monkeypatch):
    monkeypatch.setattr(
        "llm_router.router.get_repo_config",
        lambda *a, **k: RepoConfig(block_providers=["deepseek"]),
    )
    config = _FakeConfig(available_providers={"anthropic", "openai", "deepseek"})
    chain = await _chain(TaskType.CODE, RoutingProfile.BALANCED, config)
    assert not any(provider_from_model(m) == "deepseek" for m in chain)


@pytest.mark.asyncio
async def test_block_models_removes_exact_model(monkeypatch):
    monkeypatch.setattr(
        "llm_router.router.get_repo_config",
        lambda *a, **k: RepoConfig(block_models=["openai/gpt-4o"]),
    )
    config = _FakeConfig(available_providers={"anthropic", "openai", "deepseek"})
    chain = await _chain(TaskType.ANALYZE, RoutingProfile.BALANCED, config)
    assert "openai/gpt-4o" not in chain


@pytest.mark.asyncio
async def test_allow_models_restricts_chain_to_allowlist(monkeypatch):
    """allow_models can only restrict the chain to models that are actually
    available — it can't will an unconfigured Ollama model into existence.
    ollama_models must be set here so "ollama/qwen3:32b" is a real candidate
    the provider-availability filter lets through in the first place (see the
    audit fix: unavailable-tier models, including unconfigured Ollama ones,
    are now correctly stripped before allow/block filtering ever runs).
    """
    monkeypatch.setattr(
        "llm_router.router.get_repo_config",
        lambda *a, **k: RepoConfig(
            allow_models=["anthropic/claude-sonnet-4-6", "ollama/qwen3:32b"]
        ),
    )
    config = _FakeConfig(
        available_providers={"anthropic", "openai", "deepseek"},
        ollama_models=["ollama/qwen3:32b"],
    )
    chain = await _chain(TaskType.ANALYZE, RoutingProfile.BALANCED, config)
    assert set(chain) == {"anthropic/claude-sonnet-4-6", "ollama/qwen3:32b"}


@pytest.mark.asyncio
async def test_allow_models_protects_against_later_ollama_injection(monkeypatch):
    """Regression test: allow_models / block_models / block_providers used to
    only be applied to the base chain BEFORE Ollama/Codex/Gemini-CLI injection
    ran, so a model injected afterwards was never checked against the
    allow-list. The filters are now re-applied after injection too.
    """
    monkeypatch.setattr(
        "llm_router.router.get_repo_config",
        lambda *a, **k: RepoConfig(allow_models=["anthropic/claude-sonnet-4-6"]),
    )
    config = _FakeConfig(
        available_providers={"anthropic", "openai", "deepseek"},
        ollama_models=["ollama/sneaky-not-on-allowlist:1b"],
    )
    chain = await _chain(TaskType.ANALYZE, RoutingProfile.BALANCED, config)
    # Only the allow-listed model may appear — the injected Ollama model must not.
    assert "ollama/sneaky-not-on-allowlist:1b" not in chain
    assert "anthropic/claude-sonnet-4-6" in chain


@pytest.mark.asyncio
async def test_block_providers_protects_against_later_ollama_injection(monkeypatch):
    """Same class of regression as above, from the block_providers side:
    blocking 'ollama' as a provider must also stop the Ollama-injection step
    below it from re-adding an Ollama model to the chain."""
    monkeypatch.setattr(
        "llm_router.router.get_repo_config",
        lambda *a, **k: RepoConfig(block_providers=["ollama"]),
    )
    config = _FakeConfig(
        available_providers={"anthropic", "openai", "deepseek"},
        ollama_models=["ollama/qwen3.5:latest"],
    )
    chain = await _chain(TaskType.ANALYZE, RoutingProfile.BALANCED, config)
    # The block removes the STATIC chain's baked-in "ollama/qwen3:32b" entry...
    assert "ollama/qwen3:32b" not in chain
    # ...and now also the injected model (added by a later, independent code
    # path) — the provider block must hold everywhere, not just pre-injection.
    assert "ollama/qwen3.5:latest" not in chain


# ── 6. Final dedup preserves FIRST occurrence ───────────────────────────────


@pytest.mark.asyncio
async def test_dedup_preserves_first_occurrence_not_last(monkeypatch):
    """ollama/qwen3:32b is already baked into the ANALYZE/BALANCED static chain.
    Injecting it again via all_ollama_models() creates a duplicate: the dedup
    step must keep the FRONT (first) occurrence, not the later static-chain one.
    """
    config = _FakeConfig(
        available_providers={"anthropic", "openai", "deepseek"},
        ollama_models=["ollama/qwen3:32b"],
    )
    chain = await _chain(TaskType.ANALYZE, RoutingProfile.BALANCED, config)
    assert chain.count("ollama/qwen3:32b") == 1
    assert chain[0] == "ollama/qwen3:32b"


# ── 7. Media task types never receive text-model injections ────────────────


@pytest.mark.asyncio
async def test_media_task_types_skip_text_model_injection_entirely(monkeypatch):
    """IMAGE/VIDEO/AUDIO short-circuit BEFORE the whole pin/Ollama/Codex/Gemini
    CLI injection block in _build_and_filter_chain (it's gated on
    `if task_type not in MEDIA_TASK_TYPES`), so these should never appear even
    when Codex is "available" and Ollama models are configured.
    """
    monkeypatch.setattr("llm_router.router.is_codex_available", lambda: True)
    monkeypatch.setattr("llm_router.router.is_gemini_cli_available", lambda: True)
    config = _FakeConfig(
        available_providers={"openai", "fal", "stability"},
        ollama_models=["ollama/qwen3.5:latest"],
    )
    for task_type in MEDIA_TASK_TYPES:
        chain = await _chain(task_type, RoutingProfile.BALANCED, config)
        assert chain, f"{task_type} chain should not be empty"
        for m in chain:
            assert provider_from_model(m) not in {"ollama", "codex", "gemini_cli"}, (
                f"{task_type} chain leaked a text-model injection: {m!r}"
            )
