"""Regression tests for two model strings pricing.resolve() got wrong.

Found in a 2026-09-27 audit of ~/.llm-router/usage.db (claude_usage): 1,374
rows carried model strings resolve() could not price.

1. A tag-less Ollama name ("ollama/llama3.2") resolved to unknown instead of
   free/local. The prefix-stripping in _normalize() removes "ollama/" before
   resolve()'s own ollama-fallback check runs, so a name with neither a
   surviving "ollama" prefix nor a ":tag" had nothing left to be recognized
   by. Tagged names ("ollama/qwen3.5:latest") happened to survive on the
   ":" check alone, which is why this went unnoticed.
2. Bare "claude-opus" (no version suffix) was not in the alias table at all,
   so it resolved to unknown even though "opus" already resolved fine.
"""
from __future__ import annotations

from llm_router import pricing


class TestOllamaTagless:
    def test_taglesss_ollama_prefix_resolves_to_local(self) -> None:
        assert pricing.resolve("ollama/llama3.2") == "ollama"

    def test_tagged_ollama_prefix_still_resolves_to_local(self) -> None:
        assert pricing.resolve("ollama/qwen3.5:latest") == "ollama"

    def test_bare_taglesss_ollama_name_resolves_to_local(self) -> None:
        assert pricing.resolve("ollama/foo") == "ollama"

    def test_taglesss_ollama_is_genuinely_free(self) -> None:
        assert pricing.is_free("ollama/llama3.2") is True
        assert pricing.cost_usd("ollama/llama3.2", 10_000, 10_000) == 0.0


class TestBareClaudeOpus:
    def test_bare_claude_opus_resolves(self) -> None:
        # Chosen policy: follows the repo's single savings-baseline policy --
        # "claude-opus" means the same "which Opus is current" question as
        # the family alias "opus", so it resolves through
        # SAVINGS_BASELINE_MODEL rather than being rejected.
        assert pricing.resolve("claude-opus") == pricing.savings_baseline_model()
        assert pricing.resolve("claude-opus") in pricing.known_models()

    def test_bare_claude_opus_carries_no_price_of_its_own(self) -> None:
        assert "claude-opus" not in pricing.known_models()

    def test_bare_claude_opus_prices_the_same_as_the_family_alias(self) -> None:
        assert pricing.input_rate("claude-opus") == pricing.input_rate("opus")
        assert pricing.output_rate("claude-opus") == pricing.output_rate("opus")


class TestUnknownStaysUnknown:
    """Neither fix may widen resolve() into pricing things it shouldn't."""

    def test_unrelated_unknown_string_is_still_unknown(self) -> None:
        assert pricing.resolve("some-model-that-does-not-exist") is None
        assert pricing.price_for("some-model-that-does-not-exist") is None

    def test_unknown_is_not_silently_priced_as_free(self) -> None:
        assert pricing.is_free("some-model-that-does-not-exist") is False
