"""Tests for streaming LLM responses."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from llm_router.providers import call_llm_stream


class MockDelta:
    def __init__(self, content: str | None = None):
        self.content = content


class MockChoice:
    def __init__(self, content: str | None = None):
        self.delta = MockDelta(content)


class MockUsage:
    def __init__(self, prompt_tokens: int = 0, completion_tokens: int = 0):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class MockChunk:
    def __init__(self, content: str | None = None, usage: MockUsage | None = None):
        self.choices = [MockChoice(content)] if content is not None else [MockChoice()]
        self.usage = usage


async def _mock_stream(*chunks):
    """Create an async iterator from chunks."""
    for chunk in chunks:
        yield chunk


class TestCallLlmStream:
    @pytest.mark.asyncio
    async def test_yields_content_chunks(self):
        chunks = [
            MockChunk("Hello"),
            MockChunk(" world"),
            MockChunk("!"),
            MockChunk(None, MockUsage(10, 5)),
        ]

        async def mock_acompletion(**kwargs):
            assert kwargs["stream"] is True
            return _mock_stream(*chunks)

        with (
            patch("llm_router.providers.litellm.acompletion", side_effect=mock_acompletion),
            patch("llm_router.providers.get_config") as mock_config,
            patch("llm_router.providers.litellm.completion_cost", return_value=0.001),
        ):
            mock_config.return_value.default_temperature = 0.7
            mock_config.return_value.default_max_tokens = 4096
            mock_config.return_value.request_timeout = 30

            collected = []
            async for chunk in call_llm_stream(
                "gemini/gemini-2.5-flash",
                [{"role": "user", "content": "test"}],
            ):
                collected.append(chunk)

        # Content chunks + META
        content_chunks = [c for c in collected if not c.startswith("\n[META]")]
        assert content_chunks == ["Hello", " world", "!"]

        # META chunk
        meta_chunks = [c for c in collected if c.startswith("\n[META]")]
        assert len(meta_chunks) == 1
        meta = json.loads(meta_chunks[0][7:])
        assert meta["model"] == "gemini/gemini-2.5-flash"
        assert meta["input_tokens"] == 10
        assert meta["output_tokens"] == 5
        assert meta["cost_usd"] == 0.001

    @pytest.mark.asyncio
    async def test_reasoning_model_temperature(self):
        """O-series models should force temperature=1."""
        chunks = [MockChunk("ok")]

        captured_kwargs = {}

        async def mock_acompletion(**kwargs):
            captured_kwargs.update(kwargs)
            return _mock_stream(*chunks)

        with (
            patch("llm_router.providers.litellm.acompletion", side_effect=mock_acompletion),
            patch("llm_router.providers.get_config") as mock_config,
            patch("llm_router.providers.litellm.completion_cost", return_value=0.0),
        ):
            mock_config.return_value.default_temperature = 0.7
            mock_config.return_value.default_max_tokens = 4096
            mock_config.return_value.request_timeout = 30

            async for _ in call_llm_stream(
                "openai/o3",
                [{"role": "user", "content": "test"}],
            ):
                pass

        assert captured_kwargs["temperature"] == 1

    @pytest.mark.asyncio
    async def test_empty_stream(self):
        """Empty stream should yield only META."""
        async def mock_acompletion(**kwargs):
            return _mock_stream()

        with (
            patch("llm_router.providers.litellm.acompletion", side_effect=mock_acompletion),
            patch("llm_router.providers.get_config") as mock_config,
            patch("llm_router.providers.litellm.completion_cost", return_value=0.0),
        ):
            mock_config.return_value.default_temperature = 0.7
            mock_config.return_value.default_max_tokens = 4096
            mock_config.return_value.request_timeout = 30

            collected = []
            async for chunk in call_llm_stream(
                "openai/gpt-4o-mini",
                [{"role": "user", "content": "test"}],
            ):
                collected.append(chunk)

        assert len(collected) == 1  # just META
        assert collected[0].startswith("\n[META]")

    @pytest.mark.asyncio
    async def test_cost_estimation_fallback(self):
        """If completion_cost raises, cost should be 0.0."""
        chunks = [MockChunk("hi")]

        async def mock_acompletion(**kwargs):
            return _mock_stream(*chunks)

        with (
            patch("llm_router.providers.litellm.acompletion", side_effect=mock_acompletion),
            patch("llm_router.providers.get_config") as mock_config,
            patch("llm_router.providers.litellm.completion_cost", side_effect=Exception("no cost data")),
        ):
            mock_config.return_value.default_temperature = 0.7
            mock_config.return_value.default_max_tokens = 4096
            mock_config.return_value.request_timeout = 30

            collected = []
            async for chunk in call_llm_stream(
                "openai/gpt-4o-mini",
                [{"role": "user", "content": "test"}],
            ):
                collected.append(chunk)

        meta = json.loads(collected[-1][7:])
        assert meta["cost_usd"] == 0.0
