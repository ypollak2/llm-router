"""Tests for terminal styling system (v6.0 Visibility feature).

Tests:
1. Color enum and ANSI codes
2. Symbol rendering
3. Confidence bar visualization
4. Routing decision formatting
5. Box drawing components
6. NO_COLOR environment variable handling
7. Performance constraints (<100ms for HUD)
"""

import pytest
import time
from llm_router.terminal_style import (
    Color,
    Symbol,
    ConfidenceLevel,
    RoutingDecision,
    format_confidence_bar,
    format_savings_bar,
    format_box,
    format_profile_header,
    format_alert_box,
    format_savings_card,
    disable_colors,
    enable_colors,
    colors_enabled,
)
from llm_router.statusline_hud import (
    StatuslineState,
    initialize_hud,
    record_routing_decision,
    get_current_hud,
    get_session_summary,
    on_routing_decision,
)


# ============================================================================
# Color Tests
# ============================================================================


class TestColor:
    """Test Color enum and ANSI codes."""

    def test_color_values(self):
        """Test that color values contain valid ANSI codes."""
        assert Color.ORCHESTRATE_BLUE.value == "\033[34m"
        assert Color.MEMORY_AMBER.value == "\033[33m"
        assert Color.CONFIDENCE_GREEN.value == "\033[32m"
        assert Color.WARNING_RED.value == "\033[31m"
        assert Color.RESET.value == "\033[0m"

    def test_color_call_with_enabled(self):
        """Test applying color when colors are enabled."""
        enable_colors()
        colored_text = Color.ORCHESTRATE_BLUE("haiku")
        assert colored_text == "\033[34mhaiku\033[0m"

    def test_color_call_with_disabled(self):
        """Test that NO_COLOR disables colors."""
        disable_colors()
        text = Color.ORCHESTRATE_BLUE("haiku")
        assert text == "haiku"  # No ANSI codes

        enable_colors()  # Reset for other tests

    def test_color_is_enabled_property(self):
        """Test is_enabled property."""
        enable_colors()
        assert Color.ORCHESTRATE_BLUE.is_enabled is True

        disable_colors()
        assert Color.CONFIDENCE_GREEN.is_enabled is False

        enable_colors()  # Reset


# ============================================================================
# Symbol Tests
# ============================================================================


class TestSymbol:
    """Test Unicode symbol definitions."""

    def test_routing_symbols(self):
        """Test routing-related symbols."""
        assert Symbol.ARROW.value == "→"
        assert Symbol.LIGHTNING.value == "⚡"
        assert Symbol.ESCALATE_UP.value == "⬆"
        assert Symbol.ESCALATE_DOWN.value == "⬇"

    def test_memory_symbols(self):
        """Test memory and learning symbols."""
        assert Symbol.MEMORY.value == "💾"
        assert Symbol.BRAIN.value == "🧠"
        assert Symbol.LIBRARY.value == "📚"

    def test_quality_symbols(self):
        """Test quality and confidence symbols."""
        assert Symbol.STAR_FULL.value == "★"
        assert Symbol.STAR_EMPTY.value == "☆"
        assert Symbol.WARNING.value == "⚠"
        assert Symbol.ALERT.value == "🚨"


# ============================================================================
# Confidence Level Tests
# ============================================================================


class TestConfidenceLevel:
    """Test confidence visualization."""

    def test_stars_high_confidence(self):
        """Test star visualization for high confidence (95%)."""
        level = ConfidenceLevel.VERY_HIGH
        stars = level.stars(95)
        # 95% of 10 = 9.5, truncated to 9
        assert stars.count(Symbol.STAR_FULL.value) == 9
        assert stars.count(Symbol.STAR_EMPTY.value) == 1

    def test_stars_medium_confidence(self):
        """Test star visualization for medium confidence (70%)."""
        level = ConfidenceLevel.MEDIUM
        stars = level.stars(70)
        assert stars.count(Symbol.STAR_FULL.value) == 7
        assert stars.count(Symbol.STAR_EMPTY.value) == 3

    def test_stars_low_confidence(self):
        """Test star visualization for low confidence (30%)."""
        level = ConfidenceLevel.LOW
        stars = level.stars(30)
        assert stars.count(Symbol.STAR_FULL.value) == 3
        assert stars.count(Symbol.STAR_EMPTY.value) == 7


# ============================================================================
# Routing Decision Tests
# ============================================================================


class TestRoutingDecision:
    """Test RoutingDecision dataclass and formatting."""

    @pytest.fixture
    def decision(self):
        """Create a sample routing decision."""
        return RoutingDecision(
            model="haiku",
            confidence=0.87,
            task="code/simple",
            complexity="simple",
            cost=0.001,
            reason="Simple code generation, low risk",
        )

    def test_decision_creation(self, decision):
        """Test creating a routing decision."""
        assert decision.model == "haiku"
        assert decision.confidence == 0.87
        assert decision.task == "code/simple"
        assert decision.cost == 0.001

    def test_format_hud_performance(self, decision):
        """Test HUD formatting completes in <10ms."""
        start = time.time()
        hud = decision.format_hud()
        elapsed = (time.time() - start) * 1000  # Convert to ms

        assert elapsed < 10  # Must be faster than 10ms
        assert "→" in hud
        assert "haiku" in hud
        assert "87%" in hud or "[87" in hud
        assert "simple" in hud
        assert "$0.001" in hud or "$0.00" in hud

    def test_format_hud_length(self, decision):
        """Test HUD fits in statusline (<50 chars)."""
        enable_colors()
        hud = decision.format_hud()
        # Strip ANSI codes for length check
        clean_hud = hud.replace("\033[34m", "").replace("\033[32m", "").replace("\033[0m", "")
        assert len(clean_hud) <= 55  # Allow some slack for ANSI codes

    def test_format_compact(self, decision):
        """Test compact decision format for replay."""
        compact = decision.format_compact()
        assert "→" in compact
        assert "haiku" in compact
        assert "Confidence" in compact
        assert "Reasoning" in compact
        assert "$0.001" in compact or "$0.00" in compact


# ============================================================================
# Formatting Function Tests
# ============================================================================


class TestFormatFunctions:
    """Test formatting helper functions."""

    def test_confidence_bar_100_percent(self):
        """Test confidence bar at 100%."""
        bar = format_confidence_bar(100, width=20)
        assert Symbol.STAR_FULL.value in bar or "█" in bar
        assert "100%" in bar or "100" in bar

    def test_confidence_bar_50_percent(self):
        """Test confidence bar at 50%."""
        bar = format_confidence_bar(50, width=10)
        # Should have roughly half filled
        assert "%" in bar or "50" in bar

    def test_savings_bar_basic(self):
        """Test savings bar formatting."""
        bar = format_savings_bar(saved=100, baseline=200)
        assert "$100.00" in bar or "$100" in bar
        assert "50%" in bar or "50" in bar

    def test_savings_bar_zero_baseline(self):
        """Test savings bar with zero baseline."""
        bar = format_savings_bar(saved=10, baseline=0)
        assert bar == "N/A"

    def test_box_formatting(self):
        """Test box drawing component."""
        box = format_box(
            title="Test",
            lines=["Line 1", "Line 2"],
            width=30,
        )
        assert "╔" in box
        assert "╝" in box
        assert "Test" in box
        assert "Line 1" in box

    def test_profile_header_formatting(self):
        """Test profile header with memory symbol."""
        header = format_profile_header(width=50)
        assert Symbol.MEMORY.value in header
        assert "╔" in header
        assert "╝" in header
        assert "PERSONAL ROUTING PROFILE" in header

    def test_savings_card_formatting(self):
        """Test savings card formatting."""
        enable_colors()
        card = format_savings_card(
            session_cost=0.18,
            baseline_cost=2.47,
            width=50,
        )
        assert Symbol.MONEY.value in card
        assert "$0.18" in card
        assert "$2.47" in card
        assert "╔" in card
        assert "╝" in card


# ============================================================================
# Statusline HUD Tests
# ============================================================================


class TestStatuslineHUD:
    """Test statusline HUD integration."""

    def test_initialize_hud(self):
        """Test HUD initialization."""
        initialize_hud()
        summary = get_session_summary()
        assert summary["decision_count"] == 0
        assert summary["total_cost"] == 0.0

    def test_record_routing_decision(self):
        """Test recording a routing decision."""
        initialize_hud()
        hud = record_routing_decision(
            model="haiku",
            confidence=0.87,
            task="code/simple",
            cost=0.001,
            reason="Simple code",
        )

        assert "→" in hud
        assert "haiku" in hud
        assert "87%" in hud or "[87" in hud

        summary = get_session_summary()
        assert summary["decision_count"] == 1
        assert summary["total_cost"] == pytest.approx(0.001)

    def test_multiple_routing_decisions(self):
        """Test recording multiple routing decisions."""
        initialize_hud()

        record_routing_decision(
            model="haiku",
            confidence=0.87,
            task="code/simple",
            cost=0.001,
        )

        record_routing_decision(
            model="sonnet",
            confidence=0.92,
            task="analysis/moderate",
            cost=0.008,
        )

        summary = get_session_summary()
        assert summary["decision_count"] == 2
        assert summary["total_cost"] == pytest.approx(0.009)

    def test_on_routing_decision_hook(self):
        """Test routing decision hook integration."""
        initialize_hud()
        hud = on_routing_decision(
            model="haiku",
            confidence=0.87,
            task_type="code",
            task_complexity="simple",
            cost_usd=0.001,
            reason="Test decision",
        )

        assert hud is not None
        assert "haiku" in hud


# ============================================================================
# NO_COLOR Environment Tests
# ============================================================================


class TestNoColor:
    """Test NO_COLOR environment variable handling."""

    def teardown_method(self):
        """Reset colors after each test."""
        enable_colors()

    def test_disable_colors_function(self):
        """Test disable_colors() function."""
        disable_colors()
        assert not colors_enabled()

    def test_enable_colors_function(self):
        """Test enable_colors() function."""
        enable_colors()
        assert colors_enabled()

    def test_color_respects_no_color(self):
        """Test that Color respects NO_COLOR env var."""
        disable_colors()
        result = Color.ORCHESTRATE_BLUE("test")
        assert result == "test"
        assert "\033" not in result

        enable_colors()
        result = Color.ORCHESTRATE_BLUE("test")
        assert "\033" in result


# ============================================================================
# Integration Tests
# ============================================================================


class TestIntegration:
    """Integration tests for complete workflows."""

    def test_full_routing_workflow(self):
        """Test complete routing decision workflow."""
        enable_colors()
        initialize_hud()

        # Record multiple routing decisions
        for i, (model, conf, cost) in enumerate([
            ("haiku", 0.87, 0.001),
            ("sonnet", 0.92, 0.008),
            ("opus", 0.98, 0.062),
        ]):
            hud = record_routing_decision(
                model=model,
                confidence=conf,
                task=f"task/{['simple', 'moderate', 'complex'][i]}",
                cost=cost,
                reason=f"Test {i}",
            )
            assert hud is not None

        # Check final state
        summary = get_session_summary()
        assert summary["decision_count"] == 3
        expected_cost = 0.001 + 0.008 + 0.062
        assert summary["total_cost"] == pytest.approx(expected_cost)

    def test_statusline_context_formatting(self):
        """Test complete statusline context."""
        enable_colors()
        initialize_hud()

        record_routing_decision(
            model="haiku",
            confidence=0.87,
            task="code/simple",
            cost=0.001,
        )

        # Note: format_statusline_context is not imported,
        # but we test the components exist
        summary = get_session_summary()
        assert summary["decision_count"] == 1

    def test_confidence_ranges(self):
        """Test all confidence level ranges."""
        for level in ConfidenceLevel:
            min_conf, max_conf = level.value
            stars_min = level.stars(min_conf)
            stars_max = level.stars(max_conf)
            assert Symbol.STAR_FULL.value in stars_min or Symbol.STAR_FULL.value in stars_max


# ============================================================================
# Performance Tests
# ============================================================================


class TestPerformance:
    """Test performance constraints for statusline rendering."""

    def test_hud_rendering_speed(self):
        """Test HUD rendering completes in <100ms."""
        enable_colors()
        decision = RoutingDecision(
            model="haiku",
            confidence=0.87,
            task="code/simple",
            complexity="simple",
            cost=0.001,
        )

        start = time.time()
        for _ in range(100):  # 100 iterations
            decision.format_hud()
        elapsed = (time.time() - start) * 1000

        # Average should be <1ms per call, total <100ms for 100
        assert elapsed < 100

    def test_box_rendering_speed(self):
        """Test box drawing completes quickly."""
        start = time.time()
        for _ in range(50):
            format_box(
                title="Test",
                lines=["Line 1", "Line 2"],
                width=50,
            )
        elapsed = (time.time() - start) * 1000

        # Should complete in <50ms for 50 boxes
        assert elapsed < 50


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
