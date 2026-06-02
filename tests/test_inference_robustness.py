"""Plan 07 Phase 2 (Category D.1) — thinking-model content fallback.

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
