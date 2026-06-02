"""Plan 07 Phase 2 (Categories D.1 + D.2) — provider response & request hardening.

Some LiteLLM-compatible providers (DeepSeek R1, qwen3 reasoning variants,
o1-family) place the model's actual answer in `message.reasoning` and leave
`message.content` as None or "". The router previously dropped these
responses silently — see providers.py:126 prior to the fix.

extract_content(message) is the small, pure-function fix.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pytest


# Minimal shape-mimicking class for tests. We don't import the LiteLLM type
# because the real type is opaque and we want fast hermetic tests.
@dataclass
class FakeMessage:
    content: Optional[str] = None
    reasoning: Optional[str] = None


class TestExtractContent:
    def test_returns_content_when_populated(self) -> None:
        from llm_router.inference_robustness import extract_content

        msg = FakeMessage(content="real answer", reasoning="some thinking")
        assert extract_content(msg) == "real answer"

    def test_falls_back_to_reasoning_when_content_is_none(self) -> None:
        from llm_router.inference_robustness import extract_content

        msg = FakeMessage(content=None, reasoning="actually the answer")
        assert extract_content(msg) == "actually the answer"

    def test_falls_back_to_reasoning_when_content_is_empty_string(self) -> None:
        """Some providers send "" not None — both must trigger fallback."""
        from llm_router.inference_robustness import extract_content

        msg = FakeMessage(content="", reasoning="actually the answer")
        assert extract_content(msg) == "actually the answer"

    def test_returns_empty_string_when_both_are_none(self) -> None:
        from llm_router.inference_robustness import extract_content

        msg = FakeMessage(content=None, reasoning=None)
        assert extract_content(msg) == ""

    def test_returns_empty_string_when_both_are_empty(self) -> None:
        from llm_router.inference_robustness import extract_content

        msg = FakeMessage(content="", reasoning="")
        assert extract_content(msg) == ""

    def test_does_not_crash_when_reasoning_attribute_missing(self) -> None:
        """OpenAI-style messages have no `reasoning` attribute at all."""
        from llm_router.inference_robustness import extract_content

        class NoReasoningMessage:
            content = "openai answer"

        assert extract_content(NoReasoningMessage()) == "openai answer"

    def test_does_not_crash_when_content_attribute_missing(self) -> None:
        """Defensive: missing content attribute should not raise."""
        from llm_router.inference_robustness import extract_content

        class OnlyReasoningMessage:
            reasoning = "only thinking"

        assert extract_content(OnlyReasoningMessage()) == "only thinking"

    def test_whitespace_only_content_is_treated_as_empty(self) -> None:
        """Whitespace-only content shouldn't count as a real answer."""
        from llm_router.inference_robustness import extract_content

        msg = FakeMessage(content="   \n  ", reasoning="real answer")
        assert extract_content(msg) == "real answer"

    @pytest.mark.parametrize(
        ("content", "reasoning", "expected"),
        [
            ("answer", "thinking", "answer"),          # content wins
            (None, "thinking", "thinking"),            # fallback
            ("", "thinking", "thinking"),              # fallback
            (None, None, ""),                          # nothing
            ("answer", None, "answer"),                # no reasoning needed
        ],
        ids=["content-wins", "fallback-from-none",
             "fallback-from-empty", "both-empty", "no-reasoning"],
    )
    def test_extract_content_matrix(
        self,
        content: Optional[str],
        reasoning: Optional[str],
        expected: str,
    ) -> None:
        from llm_router.inference_robustness import extract_content

        msg = FakeMessage(content=content, reasoning=reasoning)
        assert extract_content(msg) == expected


class TestSafeMaxTokensKnownModels:
    """For models in the caps table, safe_max_tokens never returns more than
    the model's published output limit. This prevents OpenAI silent truncation
    and Anthropic 400-errors on oversized max_tokens."""

    def test_below_cap_returns_requested(self) -> None:
        from llm_router.inference_robustness import safe_max_tokens

        assert safe_max_tokens(2000, "anthropic/claude-sonnet-4-6") == 2000

    def test_above_cap_clamps_to_cap(self) -> None:
        from llm_router.inference_robustness import safe_max_tokens

        # Claude 4.x output cap is 8192
        assert safe_max_tokens(50000, "anthropic/claude-sonnet-4-6") == 8192

    def test_at_cap_returns_cap(self) -> None:
        from llm_router.inference_robustness import safe_max_tokens

        assert safe_max_tokens(8192, "anthropic/claude-sonnet-4-6") == 8192

    def test_gpt4o_higher_cap(self) -> None:
        """OpenAI gpt-4o family caps at 16384 — higher than Claude's 8192."""
        from llm_router.inference_robustness import safe_max_tokens

        assert safe_max_tokens(20000, "openai/gpt-4o") == 16384
        assert safe_max_tokens(20000, "openai/gpt-4o-mini") == 16384

    def test_all_three_claude_4x_models_have_caps(self) -> None:
        from llm_router.inference_robustness import safe_max_tokens

        for model in (
            "anthropic/claude-opus-4-6",
            "anthropic/claude-sonnet-4-6",
            "anthropic/claude-haiku-4-5-20251001",
        ):
            assert safe_max_tokens(99999, model) <= 8192, (
                f"{model} should cap at 8192 to avoid hard 400-errors"
            )


class TestSafeMaxTokensUnknownModels:
    """Unknown models bypass the cap — we'd rather pass through what the caller
    asked for than over-restrict a newer/better model that's not in our table."""

    def test_unknown_model_returns_requested(self) -> None:
        from llm_router.inference_robustness import safe_max_tokens

        assert safe_max_tokens(50000, "newprovider/newmodel") == 50000

    def test_unknown_model_with_small_request(self) -> None:
        from llm_router.inference_robustness import safe_max_tokens

        assert safe_max_tokens(500, "newprovider/newmodel") == 500


class TestSafeMaxTokensNoneAndZero:
    """Falsy inputs fall back to DEFAULT_MAX_TOKENS (matches existing config
    behavior)."""

    def test_none_with_known_model_returns_default_or_cap(self) -> None:
        from llm_router.inference_robustness import (
            DEFAULT_MAX_TOKENS,
            safe_max_tokens,
        )

        # With Claude (cap 8192) and DEFAULT 4096, expect DEFAULT.
        result = safe_max_tokens(None, "anthropic/claude-sonnet-4-6")
        assert result == DEFAULT_MAX_TOKENS

    def test_none_with_unknown_model_returns_default(self) -> None:
        from llm_router.inference_robustness import (
            DEFAULT_MAX_TOKENS,
            safe_max_tokens,
        )

        assert safe_max_tokens(None, "newprovider/x") == DEFAULT_MAX_TOKENS

    def test_zero_treated_as_none(self) -> None:
        from llm_router.inference_robustness import (
            DEFAULT_MAX_TOKENS,
            safe_max_tokens,
        )

        assert safe_max_tokens(0, "anthropic/claude-sonnet-4-6") == DEFAULT_MAX_TOKENS

    def test_negative_treated_as_none(self) -> None:
        from llm_router.inference_robustness import (
            DEFAULT_MAX_TOKENS,
            safe_max_tokens,
        )

        assert safe_max_tokens(-100, "openai/gpt-4o") == DEFAULT_MAX_TOKENS


@pytest.mark.parametrize(
    ("requested", "model", "expected"),
    [
        # Known model, various sizes
        (1000, "anthropic/claude-sonnet-4-6", 1000),
        (8192, "anthropic/claude-sonnet-4-6", 8192),
        (100000, "anthropic/claude-sonnet-4-6", 8192),
        # Known higher-cap model
        (20000, "openai/gpt-4o", 16384),
        (10000, "openai/gpt-4o-mini", 10000),
        # Unknown model — pass through
        (50000, "x/y", 50000),
    ],
    ids=[
        "claude-below-cap",
        "claude-at-cap",
        "claude-above-cap-clamped",
        "gpt4o-above-cap-clamped",
        "gpt4o-mini-below-cap",
        "unknown-passthrough",
    ],
)
def test_safe_max_tokens_matrix(requested: int, model: str, expected: int) -> None:
    from llm_router.inference_robustness import safe_max_tokens

    assert safe_max_tokens(requested, model) == expected
