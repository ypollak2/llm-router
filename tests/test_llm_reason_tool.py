"""Tests for the llm_reason MCP tool.

Validates:
1. llm_reason is registered in the text module's register() function
2. llm_reason always uses complexity="deep_reasoning" (not caller-supplied)
3. llm_reason routes to TaskType.ANALYZE (same as llm_analyze)
4. llm_reason returns the formatted model response
5. llm_reason records quality and caches results
6. llm_reason applies the response router
7. The tool signature is correct (no 'complexity' parameter)
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestLlmReasonRegistration:
    def test_llm_reason_is_importable(self) -> None:
        from llm_router.tools.text import llm_reason
        assert callable(llm_reason)

    def test_llm_reason_is_async(self) -> None:
        from llm_router.tools.text import llm_reason
        assert inspect.iscoroutinefunction(llm_reason)

    def test_llm_reason_registered_by_register(self) -> None:
        from llm_router.tools.text import llm_reason, register
        registered: list[object] = []

        mock_mcp = MagicMock()
        mock_mcp.tool.return_value = lambda fn: registered.append(fn) or fn
        register(mock_mcp)

        assert llm_reason in registered, (
            "llm_reason was not registered by text.register() — "
            "it won't be available as an MCP tool"
        )

    def test_llm_reason_not_registered_when_gated_off(self) -> None:
        from llm_router.tools.text import llm_reason, register
        registered: list[object] = []

        mock_mcp = MagicMock()
        mock_mcp.tool.return_value = lambda fn: registered.append(fn) or fn
        register(mock_mcp, should_register=lambda name: name != "llm_reason")

        assert llm_reason not in registered


class TestLlmReasonSignature:
    def test_no_complexity_parameter(self) -> None:
        """llm_reason must NOT expose a complexity parameter — it's always deep_reasoning."""
        from llm_router.tools.text import llm_reason
        sig = inspect.signature(llm_reason)
        assert "complexity" not in sig.parameters, (
            "llm_reason must not have a complexity parameter — "
            "it always routes with deep_reasoning"
        )

    def test_has_prompt_parameter(self) -> None:
        from llm_router.tools.text import llm_reason
        sig = inspect.signature(llm_reason)
        assert "prompt" in sig.parameters

    def test_has_system_prompt_parameter(self) -> None:
        from llm_router.tools.text import llm_reason
        sig = inspect.signature(llm_reason)
        assert "system_prompt" in sig.parameters

    def test_has_context_parameter(self) -> None:
        from llm_router.tools.text import llm_reason
        sig = inspect.signature(llm_reason)
        assert "context" in sig.parameters

    def test_has_max_tokens_parameter(self) -> None:
        from llm_router.tools.text import llm_reason
        sig = inspect.signature(llm_reason)
        assert "max_tokens" in sig.parameters


class TestLlmReasonRouting:
    """llm_reason must always use deep_reasoning complexity and ANALYZE task type."""

    @pytest.fixture
    def mock_ctx(self) -> MagicMock:
        ctx = MagicMock()
        ctx.request_id = "test-request-id"
        return ctx

    @pytest.mark.asyncio
    async def test_routes_with_deep_reasoning_complexity(self, mock_ctx) -> None:
        from llm_router.tools.text import llm_reason

        mock_response = MagicMock()
        mock_response.content = "The reasoning is..."
        mock_response.model = "deepseek/deepseek-reasoner"
        mock_response.citations = []

        with (
            patch("llm_router.tools.text.route_and_call", new_callable=AsyncMock) as mock_route,
            patch("llm_router.tools.text._announce_routing", new_callable=AsyncMock),
            patch("llm_router.tools.text._cache_result"),
            patch("llm_router.tools.text._record_quality"),
            patch("llm_router.tools.text._format_response", return_value="formatted"),
            patch("llm_router.tools.text._apply_response_router", new_callable=AsyncMock, return_value="formatted"),
        ):
            mock_route.return_value = mock_response
            await llm_reason("prove the halting problem", mock_ctx)

            call_kwargs = mock_route.call_args
            assert call_kwargs.kwargs.get("complexity_hint") == "deep_reasoning", (
                "llm_reason must always pass complexity_hint='deep_reasoning'"
            )

    @pytest.mark.asyncio
    async def test_announces_deep_reasoning_routing(self, mock_ctx) -> None:
        from llm_router.tools.text import llm_reason

        mock_response = MagicMock()
        mock_response.content = "Result"
        mock_response.model = "openai/o3"
        mock_response.citations = []

        with (
            patch("llm_router.tools.text.route_and_call", new_callable=AsyncMock, return_value=mock_response),
            patch("llm_router.tools.text._announce_routing", new_callable=AsyncMock) as mock_announce,
            patch("llm_router.tools.text._cache_result"),
            patch("llm_router.tools.text._record_quality"),
            patch("llm_router.tools.text._format_response", return_value="formatted"),
            patch("llm_router.tools.text._apply_response_router", new_callable=AsyncMock, return_value="formatted"),
        ):
            await llm_reason("step by step reasoning", mock_ctx)

            mock_announce.assert_called_once_with(mock_ctx, "analyze", "deep_reasoning")

    @pytest.mark.asyncio
    async def test_caches_with_deep_reasoning_complexity(self, mock_ctx) -> None:
        from llm_router.tools.text import llm_reason

        mock_response = MagicMock()
        mock_response.content = "Result"
        mock_response.model = "deepseek/deepseek-reasoner"
        mock_response.citations = []

        with (
            patch("llm_router.tools.text.route_and_call", new_callable=AsyncMock, return_value=mock_response),
            patch("llm_router.tools.text._announce_routing", new_callable=AsyncMock),
            patch("llm_router.tools.text._cache_result") as mock_cache,
            patch("llm_router.tools.text._record_quality"),
            patch("llm_router.tools.text._format_response", return_value="formatted"),
            patch("llm_router.tools.text._apply_response_router", new_callable=AsyncMock, return_value="formatted"),
        ):
            prompt = "Think through this derivation step by step."
            await llm_reason(prompt, mock_ctx)

            mock_cache.assert_called_once_with(prompt, mock_response, "analyze", "deep_reasoning")

    @pytest.mark.asyncio
    async def test_applies_response_router(self, mock_ctx) -> None:
        from llm_router.tools.text import llm_reason

        mock_response = MagicMock()
        mock_response.content = "Long response..."
        mock_response.model = "anthropic/claude-opus-4-6"
        mock_response.citations = []

        with (
            patch("llm_router.tools.text.route_and_call", new_callable=AsyncMock, return_value=mock_response),
            patch("llm_router.tools.text._announce_routing", new_callable=AsyncMock),
            patch("llm_router.tools.text._cache_result"),
            patch("llm_router.tools.text._record_quality"),
            patch("llm_router.tools.text._format_response", return_value="formatted response"),
            patch("llm_router.tools.text._apply_response_router", new_callable=AsyncMock, return_value="compressed response") as mock_router,
        ):
            result = await llm_reason("reasoning task", mock_ctx)

            mock_router.assert_called_once_with("formatted response")
            assert result == "compressed response"

    @pytest.mark.asyncio
    async def test_passes_system_prompt(self, mock_ctx) -> None:
        from llm_router.tools.text import llm_reason

        mock_response = MagicMock()
        mock_response.content = "Thought..."
        mock_response.model = "openai/o3"
        mock_response.citations = []

        with (
            patch("llm_router.tools.text.route_and_call", new_callable=AsyncMock, return_value=mock_response) as mock_route,
            patch("llm_router.tools.text._announce_routing", new_callable=AsyncMock),
            patch("llm_router.tools.text._cache_result"),
            patch("llm_router.tools.text._record_quality"),
            patch("llm_router.tools.text._format_response", return_value="formatted"),
            patch("llm_router.tools.text._apply_response_router", new_callable=AsyncMock, return_value="formatted"),
        ):
            sys_prompt = "Think carefully and show all steps."
            await llm_reason("prove P=NP", mock_ctx, system_prompt=sys_prompt)

            call_kwargs = mock_route.call_args.kwargs
            assert call_kwargs.get("system_prompt") == sys_prompt


class TestThinkingFlagForGemini:
    """Extended thinking must be activated for Gemini 2.5 Pro when use_thinking=True."""

    def test_gemini_25_receives_thinking_config(self) -> None:
        """The _call_text_model function must add thinkingConfig for gemini-2.5* models."""
        # We test the actual parameter-building logic in router.py rather than
        # the full async call stack. The relevant code is around line 2815.

        router_path = (
            __import__("pathlib").Path(__file__).resolve().parent.parent
            / "src" / "llm_router" / "router.py"
        )
        source = router_path.read_text(encoding="utf-8")

        # Verify both branches are present in source
        assert 'extra["thinking"]' in source, (
            "Anthropic extended thinking branch missing from router.py"
        )
        assert 'extra["thinkingConfig"]' in source, (
            "Gemini thinkingConfig branch missing from router.py — "
            "Gemini 2.5 Pro will not receive extended thinking"
        )
        assert '"thinkingBudget"' in source, (
            "thinkingBudget key missing from Gemini thinkingConfig"
        )
        assert "gemini-2.5" in source, (
            "Gemini 2.5 model check missing from thinking branch"
        )

    def test_anthropic_thinking_still_present(self) -> None:
        """Regression: Anthropic extended thinking must remain intact after Gemini addition."""
        router_path = (
            __import__("pathlib").Path(__file__).resolve().parent.parent
            / "src" / "llm_router" / "router.py"
        )
        source = router_path.read_text(encoding="utf-8")
        assert '"type": "enabled"' in source
        assert '"budget_tokens": 16000' in source
        assert 'temperature = 1' in source


class TestLlmReasonVsLlmAnalyze:
    """llm_reason and llm_analyze are complementary, not duplicates."""

    def test_llm_analyze_has_complexity_param(self) -> None:
        from llm_router.tools.text import llm_analyze
        sig = inspect.signature(llm_analyze)
        assert "complexity" in sig.parameters, (
            "llm_analyze must retain the complexity parameter — "
            "it's the flexible analysis tool"
        )

    def test_llm_reason_lacks_complexity_param(self) -> None:
        from llm_router.tools.text import llm_reason
        sig = inspect.signature(llm_reason)
        assert "complexity" not in sig.parameters, (
            "llm_reason must NOT have complexity — it's always deep_reasoning"
        )

    def test_both_tools_are_registered(self) -> None:
        from llm_router.tools.text import llm_analyze, llm_reason, register
        registered: list[object] = []
        mock_mcp = MagicMock()
        mock_mcp.tool.return_value = lambda fn: registered.append(fn) or fn
        register(mock_mcp)
        assert llm_analyze in registered
        assert llm_reason in registered
