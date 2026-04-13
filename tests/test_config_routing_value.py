"""Tests for RouterConfig — provider detection and cost-saving routing contracts.

These tests verify the key cost-avoidance behaviors: which providers are
visible to the router, when Claude is excluded from chains to prevent
double-billing, and how Ollama is treated as a free-tier provider.
"""

from __future__ import annotations

import os
from unittest.mock import patch


from llm_router.config import RouterConfig
from llm_router.types import RoutingProfile


class TestAvailableProviders:
    """available_providers determines which models the router can try.

    An empty set means all routed calls will fail immediately.
    A partial set limits fallback chains — bugs here silently reduce reliability.
    """

    def test_empty_env_returns_empty_set(self):
        # ollama_base_url="" ensures .env OLLAMA_BASE_URL doesn't leak in
        cfg = RouterConfig(
            openai_api_key="",
            anthropic_api_key="",
            gemini_api_key="",
            ollama_base_url="",
        )
        assert cfg.available_providers == set()

    def test_single_key_exposes_that_provider(self):
        # Explicitly set all other keys to "" so the .env file doesn't leak providers
        cfg = RouterConfig(
            openai_api_key="sk-test-openai",
            anthropic_api_key="",
            gemini_api_key="",
            groq_api_key="",
            perplexity_api_key="",
            mistral_api_key="",
            deepseek_api_key="",
            together_api_key="",
            xai_api_key="",
            cohere_api_key="",
            fal_key="",
            stability_api_key="",
            elevenlabs_api_key="",
            runway_api_key="",
            replicate_api_token="",
            ollama_base_url="",
        )
        providers = cfg.available_providers
        assert "openai" in providers
        assert "anthropic" not in providers
        assert "gemini" not in providers

    def test_multiple_keys_all_visible(self, monkeypatch):
        # Clear env first to prevent .env leakage; then explicitly configure
        for var in ["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY",
                    "OLLAMA_BASE_URL", "LLM_ROUTER_CLAUDE_SUBSCRIPTION"]:
            monkeypatch.delenv(var, raising=False)
        cfg = RouterConfig(
            openai_api_key="sk-openai",
            anthropic_api_key="sk-anthropic",
            gemini_api_key="AIz-gemini",
            groq_api_key="gsk-groq",
            ollama_base_url="",
            llm_router_claude_subscription=False,
        )
        providers = cfg.available_providers
        assert {"openai", "anthropic", "gemini", "groq"}.issubset(providers)

    def test_media_keys_included_in_available_providers(self):
        cfg = RouterConfig(fal_key="fal-test", stability_api_key="sk-stability", ollama_base_url="")
        providers = cfg.available_providers
        assert "fal" in providers
        assert "stability" in providers

    def test_text_providers_excludes_media_only(self):
        cfg = RouterConfig(
            openai_api_key="sk-openai",
            fal_key="fal-test",         # fal = media only, not in text providers
            ollama_base_url="",
        )
        assert "openai" in cfg.text_providers
        # fal is media-only, should not appear in text providers
        assert "fal" not in cfg.text_providers

    def test_media_providers_excludes_text_only(self):
        cfg = RouterConfig(
            anthropic_api_key="sk-anthropic",   # text only
            fal_key="fal-test",
            ollama_base_url="",
        )
        assert "fal" in cfg.media_providers
        # anthropic is text-only, not in media providers
        assert "anthropic" not in cfg.media_providers

    def test_openai_appears_in_both_provider_sets(self):
        """OpenAI supports both text (GPT-4) and image (DALL-E) generation."""
        cfg = RouterConfig(openai_api_key="sk-openai", ollama_base_url="")
        assert "openai" in cfg.text_providers
        assert "openai" in cfg.media_providers


class TestClaudeSubscriptionMode:
    """When LLM_ROUTER_CLAUDE_SUBSCRIPTION=true, Anthropic is excluded.

    This is the most important cost-saving contract: inside Claude Code the user
    already pays for Claude via their Pro/Max subscription. Routing back to
    anthropic/* via API key would create duplicate billing. The router must
    hard-exclude anthropic in this mode even if ANTHROPIC_API_KEY is set.
    """

    def test_anthropic_excluded_when_subscription_mode_on(self):
        cfg = RouterConfig(
            anthropic_api_key="sk-anthropic-valid",
            llm_router_claude_subscription=True,
        )
        assert "anthropic" not in cfg.available_providers

    def test_anthropic_included_when_subscription_mode_off(self):
        cfg = RouterConfig(
            anthropic_api_key="sk-anthropic-valid",
            llm_router_claude_subscription=False,
        )
        assert "anthropic" in cfg.available_providers

    def test_other_providers_unaffected_by_subscription_mode(self):
        cfg = RouterConfig(
            openai_api_key="sk-openai",
            gemini_api_key="AIz-gemini",
            anthropic_api_key="sk-anthropic",
            llm_router_claude_subscription=True,
        )
        providers = cfg.available_providers
        assert "openai" in providers
        assert "gemini" in providers
        # Only anthropic is removed
        assert "anthropic" not in providers

    def test_apply_keys_to_env_withholds_anthropic_key_in_subscription_mode(self):
        """LiteLLM reads from os.environ; anthropic key must not be set there."""
        # Capture the env state before applying
        original_key = os.environ.get("ANTHROPIC_API_KEY")
        try:
            os.environ.pop("ANTHROPIC_API_KEY", None)
            cfg = RouterConfig(
                anthropic_api_key="sk-secret-anthropic",
                llm_router_claude_subscription=True,
            )
            cfg.apply_keys_to_env()
            # The key must NOT have been exported to the environment
            assert os.environ.get("ANTHROPIC_API_KEY") is None
        finally:
            if original_key is not None:
                os.environ["ANTHROPIC_API_KEY"] = original_key
            else:
                os.environ.pop("ANTHROPIC_API_KEY", None)

    def test_apply_keys_to_env_exports_anthropic_when_not_subscription(self):
        """Without subscription mode, the Anthropic key IS exported for LiteLLM."""
        original_key = os.environ.get("ANTHROPIC_API_KEY")
        try:
            os.environ.pop("ANTHROPIC_API_KEY", None)
            cfg = RouterConfig(
                anthropic_api_key="sk-secret-anthropic",
                llm_router_claude_subscription=False,
            )
            cfg.apply_keys_to_env()
            assert os.environ.get("ANTHROPIC_API_KEY") == "sk-secret-anthropic"
        finally:
            if original_key is not None:
                os.environ["ANTHROPIC_API_KEY"] = original_key
            else:
                os.environ.pop("ANTHROPIC_API_KEY", None)


class TestOllamaProviderInclusion:
    """Ollama has no API key — its inclusion is controlled by ollama_base_url + liveness.

    When Ollama is reachable, it is inserted at the front of routing chains
    because it is free and local. Tests here ensure the probe/cache logic
    correctly gates Ollama visibility.
    """

    def test_ollama_included_when_reachable(self):
        with patch("llm_router.config.probe_ollama", return_value=True):
            cfg = RouterConfig(ollama_base_url="http://localhost:11434")
            assert "ollama" in cfg.available_providers

    def test_ollama_excluded_when_unreachable(self):
        with patch("llm_router.config.probe_ollama", return_value=False):
            cfg = RouterConfig(ollama_base_url="http://localhost:11434")
            assert "ollama" not in cfg.available_providers

    def test_ollama_excluded_when_base_url_empty(self):
        cfg = RouterConfig(ollama_base_url="")
        assert "ollama" not in cfg.available_providers

    def test_ollama_model_ids_have_correct_prefix(self):
        """Router needs 'ollama/modelname' format for LiteLLM dispatch."""
        cfg = RouterConfig(
            ollama_base_url="http://localhost:11434",
            ollama_budget_models="llama3.2,qwen2.5-coder:7b",
        )
        models = cfg.all_ollama_models()
        assert models == ["ollama/llama3.2", "ollama/qwen2.5-coder:7b"]

    def test_ollama_models_empty_when_no_base_url(self):
        cfg = RouterConfig(ollama_base_url="")
        assert cfg.all_ollama_models() == []

    def test_ollama_models_empty_when_no_model_names(self):
        cfg = RouterConfig(
            ollama_base_url="http://localhost:11434",
            ollama_budget_models="",
        )
        assert cfg.all_ollama_models() == []


class TestRoutingDefaults:
    """Router defaults must be sensible out-of-the-box."""

    def test_default_profile_is_balanced(self):
        cfg = RouterConfig()
        assert cfg.llm_router_profile == RoutingProfile.BALANCED

    def test_default_monthly_budget_is_twenty_dollars(self):
        cfg = RouterConfig()
        assert cfg.llm_router_monthly_budget == 20.0

    def test_default_temperature_is_point_seven(self):
        cfg = RouterConfig()
        assert cfg.default_temperature == 0.7

    def test_default_max_tokens_is_sufficient_for_responses(self):
        cfg = RouterConfig()
        assert cfg.default_max_tokens >= 4096
