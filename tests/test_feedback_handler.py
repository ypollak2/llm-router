"""Tests for unified FeedbackHandler (Phases 1-3)."""

from __future__ import annotations

import time


from llm_router.feedback_handler import FeedbackHandler, TimelineEvent


class TestTimelineEvent:
    """Timeline event serialization."""

    def test_event_to_display_formats_correctly(self):
        """TimelineEvent.to_display() outputs valid format."""
        event = TimelineEvent(
            name="Classification",
            timestamp=0.5,
            status="success",
            details="moderate (heuristic)",
            duration_ms=2.3,
        )

        display = event.to_display()
        assert "✓" in display
        assert "0.5s" in display
        assert "Classification" in display
        assert "moderate" in display
        assert "+2.3ms" in display


class TestFeedbackHandler:
    """Unified handler orchestrating all 3 phases."""

    def test_handler_initializes_with_session_id(self):
        """FeedbackHandler creates a unique session ID."""
        h1 = FeedbackHandler()
        h2 = FeedbackHandler()

        assert h1.session_id != h2.session_id
        assert len(h1.session_id) == 36  # UUID format

    def test_routing_lifecycle_events(self):
        """Handler records classification → routing → send."""
        handler = FeedbackHandler()

        handler.on_routing_start()
        assert len(handler.timeline) == 1

        handler.on_classification(complexity="moderate", method="heuristic")
        assert len(handler.timeline) == 2
        assert "moderate" in handler.timeline[1].details

        handler.on_model_selected(model="claude-opus")
        assert len(handler.timeline) == 3
        assert "claude-opus" in handler.timeline[2].details

        handler.on_send(token_count=2100)
        assert len(handler.timeline) == 4
        assert "2,100" in handler.timeline[3].details

    def test_token_tracking_phase_1(self):
        """Phase 1: Token counting and rate calculation."""
        handler = FeedbackHandler()
        handler.on_routing_start()
        handler.on_send(2100)

        # Simulate token stream
        for _ in range(100):
            handler.on_token(count=1)
            time.sleep(0.001)  # 1ms per token

        assert handler.token_count == 100
        assert handler.tokens_per_second > 0
        assert handler.first_token_time is not None

    def test_progress_bar_format(self):
        """Phase 1: Progress bar displays correctly."""
        handler = FeedbackHandler()
        handler.estimated_total_tokens = 200
        handler.start_time = time.monotonic()
        handler.first_token_time = time.monotonic() - 1.0  # Fake 1 second elapsed

        # Simulate receiving tokens
        for _ in range(100):
            handler.on_token(count=1)

        bar = handler.progress_bar()
        assert "50%" in bar  # 100/200 = 50%
        assert "100 tokens" in bar
        assert "⏳ Processing" in bar

    def test_eta_calculation(self):
        """Phase 1: ETA calculates based on token rate."""
        handler = FeedbackHandler()
        handler.estimated_total_tokens = 200
        handler.token_count = 50
        handler.tokens_per_second = 10.0  # 10 tokens/sec

        eta_ms = handler.eta_ms
        # Should be ~15 seconds for remaining 150 tokens
        assert 14000 < eta_ms < 16000

    def test_timeline_phase_2(self):
        """Phase 2: Timeline tracks all stages with timing."""
        handler = FeedbackHandler()

        handler.on_routing_start()
        time.sleep(0.01)  # 10ms
        handler.on_classification(complexity="complex", method="heuristic")
        time.sleep(0.01)
        handler.on_model_selected(model="claude-opus")

        assert len(handler.timeline) >= 2
        assert handler.timeline[0].name == "Routing"
        assert handler.timeline[1].name == "Classification"
        assert handler.timeline[2].name == "Model Selected"

    def test_thinking_extraction_phase_3(self):
        """Phase 3: Extract Claude thinking blocks."""
        handler = FeedbackHandler()

        # Simulate streaming content with thinking block
        handler.on_token("<thinking>")
        handler.on_token("Let me analyze this problem. ")
        handler.on_token("Step 1: Identify components. ")
        handler.on_token("</thinking>")
        handler.on_token("Here's my analysis: ")

        assert len(handler.final_thinking_blocks) == 1
        assert "analyze this problem" in handler.final_thinking_blocks[0]
        assert "<thinking>" not in handler.final_thinking_blocks[0]

    def test_multiple_thinking_blocks(self):
        """Phase 3: Handle multiple thinking blocks in one response."""
        handler = FeedbackHandler()

        handler.on_token("<thinking>First thought block</thinking>")
        handler.on_token("Regular response part. ")
        handler.on_token("<thinking>Second thought block</thinking>")

        assert len(handler.final_thinking_blocks) == 2
        assert "First thought block" in handler.final_thinking_blocks[0]
        assert "Second thought block" in handler.final_thinking_blocks[1]

    def test_format_display_includes_all_phases(self):
        """Display output includes token counter, timeline, and thinking."""
        handler = FeedbackHandler()
        handler.on_routing_start()
        handler.on_classification(complexity="moderate", method="heuristic")
        handler.on_model_selected(model="claude-opus")
        handler.on_send(1000)

        # Add tokens
        handler.estimated_total_tokens = 200
        for _ in range(50):
            handler.on_token(count=1)
            time.sleep(0.001)

        # Add thinking
        handler.on_token("<thinking>I think therefore I am</thinking>")

        display = handler.format_display()

        # Verify all phases present
        assert "📋 Timeline:" in display
        assert "✓" in display  # Timeline events
        assert "⏳ Progress:" in display
        assert "tokens/sec" in display
        assert "🧠 Model's Thinking:" in display
        assert "I think therefore I am" in display

    def test_to_json_serializes(self):
        """Handler serializes to JSON for storage."""
        handler = FeedbackHandler()
        handler.on_routing_start()
        handler.on_classification(complexity="simple", method="heuristic")
        handler.on_send(100)
        handler.on_token(count=10)
        handler.on_complete()

        json_str = handler.to_json()
        assert handler.session_id in json_str
        assert "10" in json_str  # token count
        assert "timeline" in json_str

    def test_elapsed_ms_tracks_time(self):
        """Handler.elapsed_ms increases over time."""
        handler = FeedbackHandler()
        handler.on_routing_start()

        time1 = handler.elapsed_ms
        time.sleep(0.01)
        time2 = handler.elapsed_ms

        assert time2 > time1
        assert time2 - time1 >= 10  # At least 10ms difference
