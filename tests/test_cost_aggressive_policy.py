"""Plan 06 Steps 1+2 — cost_aggressive policy + OpenRouter provider.

Three surfaces covered:

* :mod:`llm_router.config` — OpenRouter API key wiring (env, available_providers,
  text_providers).
* :mod:`llm_router.provider_quirks` — OpenRouterQuirks ``max_tokens`` cap added
  on top of the Plan 07 D.4 prefix rename.
* :mod:`llm_router.calibration` — OpenRouter open-weight workhorse pricing.
* :mod:`llm_router.policies.cost_aggressive` — the new policy YAML.

We don't run live OpenRouter calls here (that needs network + a real API
key); the test surface pins the static contracts so the policy and pricing
can't drift away from each other or from the OpenRouterQuirks behaviour.
"""

from __future__ import annotations

import pytest

from llm_router.calibration import cost_for_tokens
from llm_router.policy import PolicyManager
from llm_router.provider_quirks import OpenRouterQuirks


# ── Config wiring ──────────────────────────────────────────────────────────


class TestOpenRouterConfig:
    """Plan 06 Step 2 — config-level wiring."""

    def test_openrouter_key_makes_provider_available(self, monkeypatch):
        """Setting OPENROUTER_API_KEY surfaces ``openrouter`` in available_providers."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-router")
        import llm_router.config as config_module
        config_module._config = None
        from llm_router.config import get_config
        cfg = get_config()
        assert "openrouter" in cfg.available_providers
        assert "openrouter" in cfg.text_providers

    def test_no_key_excludes_openrouter(self, monkeypatch):
        """Unset key → openrouter must not appear (no silent fallback).

        Patches the field directly because pydantic-settings reads ``.env``
        at construction time and the test runner's .env may legitimately have
        ``OPENROUTER_API_KEY`` set for live integration runs. The contract
        we're pinning is "empty key string excludes openrouter from
        available_providers", which is what monkeypatching the attribute
        actually tests.
        """
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        import llm_router.config as config_module
        config_module._config = None
        from llm_router.config import get_config
        cfg = get_config()
        monkeypatch.setattr(cfg, "openrouter_api_key", "")
        assert "openrouter" not in cfg.available_providers


# ── OpenRouterQuirks max_tokens cap ─────────────────────────────────────────


class TestOpenRouterMaxTokensCap:
    """Plan 06 line 103: cap at 2048 to avoid 402 'requires fewer max_tokens'."""

    def test_caps_when_above(self):
        out = OpenRouterQuirks().transform_request(
            {"model": "openrouter/qwen/qwen3-235b-a22b-2507", "max_tokens": 8192}
        )
        assert out["max_tokens"] == 2048

    def test_passes_through_when_under(self):
        """Under the cap → no change, preserving the caller's smaller request."""
        out = OpenRouterQuirks().transform_request(
            {"model": "openrouter/qwen/qwen3-235b-a22b-2507", "max_tokens": 512}
        )
        assert out["max_tokens"] == 512

    def test_no_max_tokens_field_unchanged(self):
        """Absent ``max_tokens`` → no key added, no copy made."""
        payload = {"model": "openrouter/qwen/qwen3-235b-a22b-2507"}
        out = OpenRouterQuirks().transform_request(payload)
        assert "max_tokens" not in out
        assert out is payload  # identity preserved when nothing to change

    def test_does_not_mutate_input(self):
        original = {"model": "openrouter/qwen/qwen3-235b-a22b-2507", "max_tokens": 8192}
        OpenRouterQuirks().transform_request(original)
        assert original["max_tokens"] == 8192

    def test_combined_with_claude_rename(self):
        """Both the claude rename and the cap must apply together."""
        out = OpenRouterQuirks().transform_request(
            {"model": "claude-sonnet-4", "max_tokens": 4096}
        )
        assert out["model"] == "anthropic/claude-sonnet-4"
        assert out["max_tokens"] == 2048


# ── OpenRouter pricing in calibration ───────────────────────────────────────


class TestOpenRouterPricing:
    """The cost_aggressive workhorses + specialists must be priced."""

    @pytest.mark.parametrize("model", [
        "openrouter/qwen/qwen3-235b-a22b-2507",
        "openrouter/deepseek/deepseek-v4-flash",
        "openrouter/google/gemini-3.1-flash-lite",
        "openrouter/qwen/qwen3-coder-next",
        "openrouter/qwen/qwen3-next-80b-a3b-instruct",
        "openrouter/x-ai/grok-4.3",
    ])
    def test_model_has_pricing(self, model):
        """Each policy-referenced model resolves to a non-zero cost."""
        # 1000 input + 1000 output keeps the test independent of exact rates.
        cost = cost_for_tokens(model, 1000, 1000)
        assert cost > 0, f"{model} priced at zero — bandit/policy-diff will misrank"

    def test_workhorse_pool_cheaper_than_gpt4o_for_typical_workload(self):
        """Workhorse pool must beat gpt-4o (NOT mini — the standard policy
        ships gpt-4o in its balanced workhorses) on a representative
        RouterArena workload of 200 input + 250 output tokens.

        This is the per-prompt cost claim that underpins the Arena Score
        improvement: cheaper workhorses means we can route more aggressively
        without budget pressure forcing escalations.
        """
        prompt_in, prompt_out = 200, 250
        gpt4o = cost_for_tokens("gpt-4o", prompt_in, prompt_out)
        for model in [
            "openrouter/qwen/qwen3-235b-a22b-2507",
            "openrouter/deepseek/deepseek-v4-flash",
            "openrouter/google/gemini-3.1-flash-lite",
        ]:
            cost = cost_for_tokens(model, prompt_in, prompt_out)
            assert cost < gpt4o, (
                f"workhorse {model!r} (${cost:.5f}) is not cheaper "
                f"than gpt-4o (${gpt4o:.5f}) on typical RouterArena prompt — "
                f"cost_aggressive will not save money vs standard"
            )


# ── cost_aggressive policy ────────────────────────────────────────────────


class TestRouterArenaTunedPolicy:
    """The policy YAML must load cleanly and match the strategy spec."""

    @pytest.fixture
    def policy(self):
        return PolicyManager().load_policy("cost_aggressive")

    def test_loads_and_names_match(self, policy):
        # YAML name has underscore (must be a valid Python identifier per
        # RoutingPolicy.__post_init__); user activates via the hyphenated
        # filename, the loader normalises.
        assert policy.name == "cost_aggressive"

    def test_workhorses_are_free_or_cheap(self, policy):
        """First workhorse must be free (Ollama) — free-first is the policy thesis."""
        assert policy.workhorses[0].startswith("ollama/")
        # All remaining workhorses must be cheap (OpenRouter or codex
        # subscription). No paid OpenAI/Anthropic direct API in the
        # workhorse pool.
        for model in policy.workhorses[1:]:
            prefix = model.split("/", 1)[0]
            assert prefix in {"openrouter", "codex"}, (
                f"workhorse {model!r} uses paid direct API — "
                f"breaks RouterArena cost strategy"
            )

    def test_specialists_cover_routerarena_subjects(self, policy):
        """The plan calls out code/cloze/narrative/history/medical as the
        five highest-leverage subjects. Missing any of them regresses
        expected Arena lift."""
        expected = {"code", "cloze", "narrative", "history", "medical"}
        assert expected.issubset(policy.specialists.keys())

    def test_specialists_route_to_openrouter(self, policy):
        """All subject specialists must be OpenRouter — that's the whole
        thesis of Plan 06 Step 1. A specialist that quietly falls back to
        a paid direct API would blow the cost budget."""
        for subject, model in policy.specialists.items():
            assert model.startswith("openrouter/"), (
                f"specialist {subject!r} → {model!r} is not on OpenRouter"
            )

    def test_chains_present_for_text_task_types(self, policy):
        """Each task type the router dispatches must have at least one
        candidate in each profile, or routing will raise a no-chain error."""
        for profile in ("budget", "balanced", "premium"):
            for task in ("query", "research", "generate", "analyze", "code"):
                chain = policy.chains.get(profile, {}).get(task)
                assert chain, f"{profile}/{task} chain is empty in cost_aggressive"

    def test_cost_cap_set(self, policy):
        """The cap is the production safety net — must be set, must be sane."""
        assert policy.cost_cap_per_query is not None
        assert 0 < policy.cost_cap_per_query < 1.0


class TestRouterArenaTunedAlias:
    """v10.0.0 deprecation — ``routerarena_tuned`` is a backward-compat alias.

    Existing user configs setting ``LLM_ROUTER_POLICY=routerarena_tuned`` must
    continue to load a policy with byte-identical workhorses / specialists /
    chains as the new ``cost_aggressive``. Slated for removal in v11.
    """

    def test_alias_loads(self):
        from llm_router.policy import PolicyManager
        # Clear cache between loads so we test the on-disk content, not the
        # cached object from a prior test.
        mgr = PolicyManager()
        new = mgr.load_policy("cost_aggressive")
        mgr._policy_cache.clear()
        old = mgr.load_policy("routerarena_tuned")
        assert old.workhorses == new.workhorses
        assert old.specialists == new.specialists
        assert old.chains == new.chains
        assert old.fallback_chain_complex == new.fallback_chain_complex

    def test_alias_keeps_old_name_for_identity(self):
        """The alias keeps ``name: routerarena_tuned`` so users can
        introspect ``get_active_policy().name`` and see what they set."""
        from llm_router.policy import PolicyManager
        mgr = PolicyManager()
        old = mgr.load_policy("routerarena_tuned")
        assert old.name == "routerarena_tuned"
        # And cost_aggressive identifies as itself
        mgr._policy_cache.clear()
        new = mgr.load_policy("cost_aggressive")
        assert new.name == "cost_aggressive"
