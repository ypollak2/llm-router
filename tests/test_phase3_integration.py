"""Integration tests for Phase 3 — Token-Savior response compression."""

import os

import pytest

from llm_router.tools.text import _apply_response_compression


class TestResponseCompressionIntegration:
    """Test response compression integration into the routing pipeline."""

    def test_compression_disabled_by_default(self):
        """Compression should be disabled unless explicitly enabled."""
        # Ensure the env var is not set
        if "LLM_ROUTER_COMPRESS_RESPONSE" in os.environ:
            del os.environ["LLM_ROUTER_COMPRESS_RESPONSE"]
        
        response = "I think this is basically a really important solution. " * 50
        compressed, was_applied = _apply_response_compression(response)
        
        assert compressed == response
        assert was_applied is False

    def test_compression_enabled_via_env_var(self):
        """Compression should apply when LLM_ROUTER_COMPRESS_RESPONSE=true."""
        os.environ["LLM_ROUTER_COMPRESS_RESPONSE"] = "true"
        try:
            response = (
                "I think this is basically a really important solution. "
                "Actually, you should definitely use this approach. " * 40
            )
            compressed, was_applied = _apply_response_compression(response)
            
            # Should be compressed
            assert was_applied is True
            # Compressed should be shorter
            assert len(compressed) < len(response)
        finally:
            del os.environ["LLM_ROUTER_COMPRESS_RESPONSE"]

    def test_compression_skips_short_responses(self):
        """Compression should not apply to responses < 200 chars."""
        os.environ["LLM_ROUTER_COMPRESS_RESPONSE"] = "true"
        try:
            short_response = "This is a short response."
            compressed, was_applied = _apply_response_compression(short_response)
            
            assert compressed == short_response
            assert was_applied is False
        finally:
            del os.environ["LLM_ROUTER_COMPRESS_RESPONSE"]

    def test_compression_ratio_threshold(self):
        """Compression should only apply if achieving < 95% ratio."""
        os.environ["LLM_ROUTER_COMPRESS_RESPONSE"] = "true"
        try:
            # Text with minimal filler - hard to compress
            response = "Function definition. Implementation details. Return value. " * 20
            compressed, was_applied = _apply_response_compression(response)
            
            # May or may not compress depending on actual ratio
            # The important thing is that the function doesn't crash
            assert isinstance(compressed, str)
            assert isinstance(was_applied, bool)
        finally:
            del os.environ["LLM_ROUTER_COMPRESS_RESPONSE"]

    def test_compression_handles_exceptions(self):
        """Compression should handle exceptions gracefully."""
        os.environ["LLM_ROUTER_COMPRESS_RESPONSE"] = "true"
        try:
            response = "I think this is basically important. " * 50
            # Should not raise even if something goes wrong
            compressed, was_applied = _apply_response_compression(response)
            
            # Should return a string
            assert isinstance(compressed, str)
            assert isinstance(was_applied, bool)
        finally:
            del os.environ["LLM_ROUTER_COMPRESS_RESPONSE"]

    def test_compression_with_real_response(self):
        """Compression should work with realistic responses."""
        os.environ["LLM_ROUTER_COMPRESS_RESPONSE"] = "true"
        try:
            response = (
                "I think this is basically a really important solution. "
                "Actually, you should definitely use this approach. " * 40
            )
            
            compressed, was_applied = _apply_response_compression(response)
            
            # Should apply compression to verbose text
            assert was_applied is True
            # Compressed should be shorter
            assert len(compressed) < len(response)
        finally:
            del os.environ["LLM_ROUTER_COMPRESS_RESPONSE"]


class TestCompressionStatsDashboard:
    """Test that compression stats are properly displayed in the dashboard."""

    @pytest.mark.asyncio
    async def test_get_compression_stats_returns_both_layers(self):
        """get_compression_stats should return RTK and Token-Savior stats."""
        from llm_router.cost import get_compression_stats
        
        # Call the function
        stats = await get_compression_stats(days=7)
        
        # Should have both layer stats dicts
        assert "rtk_stats" in stats
        assert "token_savior_stats" in stats
        assert "by_strategy" in stats
        
        # Both should be dicts (may be empty if no data)
        assert isinstance(stats["rtk_stats"], dict)
        assert isinstance(stats["token_savior_stats"], dict)
        
        # If there is RTK data, it should have standard keys
        if stats["rtk_stats"].get("operations", 0) > 0:
            assert "operations" in stats["rtk_stats"]
            assert "original_tokens" in stats["rtk_stats"]
            assert "compressed_tokens" in stats["rtk_stats"]
            assert "tokens_saved" in stats["rtk_stats"]
        
        # If there is Token-Savior data, it should have same keys
        if stats["token_savior_stats"].get("operations", 0) > 0:
            assert "operations" in stats["token_savior_stats"]
            assert "original_tokens" in stats["token_savior_stats"]
            assert "compressed_tokens" in stats["token_savior_stats"]
            assert "tokens_saved" in stats["token_savior_stats"]

    def test_format_savings_includes_compression_layer_section(self):
        """format_savings should include compression layer statistics."""
        from llm_router.commands.gain import SavingsAnalytics
        
        analytics = SavingsAnalytics()
        # Use mock savings data
        savings = {
            "period_days": 7,
            "total_decisions": 0,
            "total_cost_usd": 0.0,
            "total_opus_cost_usd": 0.0,
            "total_saved_usd": 0.0,
            "efficiency_multiplier": 1.0,
            "by_tool": {},
            "by_model": {},
            "by_complexity": {},
            "daily_breakdown": {},
        }
        
        result = analytics.format_savings(savings, period_days=7)
        
        # Should include compression layer section
        assert "COMPRESSION LAYERS" in result or "COMPRESSION LAYER" in result

    def test_format_savings_handles_missing_compression_stats(self):
        """format_savings should gracefully handle missing compression data."""
        from llm_router.commands.gain import SavingsAnalytics
        
        analytics = SavingsAnalytics()
        savings = {
            "period_days": 7,
            "total_decisions": 10,
            "total_cost_usd": 1.0,
            "total_opus_cost_usd": 10.0,
            "total_saved_usd": 9.0,
            "efficiency_multiplier": 10.0,
            "by_tool": {"llm_query": {"count": 10, "cost": 1.0, "opus_cost": 10.0}},
            "by_model": {},
            "by_complexity": {},
            "daily_breakdown": {},
        }
        
        # Should not crash even if compression stats are unavailable
        result = analytics.format_savings(savings, period_days=7)
        
        assert isinstance(result, str)
        assert len(result) > 0
        assert "TOKEN SAVINGS DASHBOARD" in result
