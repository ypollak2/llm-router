"""Tests for real-time routing feedback (Phases 1-3)."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from llm_router.feedback import (
    FeedbackStore,
    RoutingEvent,
    RoutingEventType,
    RoutingTimeline,
    TokenFeedback,
)


class TestTokenFeedback:
    """Phase 1 MVP: Token counter + ETA."""

    def test_token_counter_tracks_rate(self):
        """TokenFeedback.on_token() updates rate correctly."""
        feedback = TokenFeedback(
            session_id="test-session",
            estimated_total=100,
        )

        # Simulate token stream with slight delay
        for _ in range(10):
            feedback.on_token()
            time.sleep(0.001)  # 1ms per token

        assert feedback.tokens_received == 10
        assert feedback.rate_tokens_per_sec > 0

    def test_eta_calculation(self):
        """TokenFeedback.estimated_remaining_ms calculates ETA."""
        feedback = TokenFeedback(
            session_id="test-session",
            estimated_total=100,
        )

        # Fake timing: 10 tokens in 1 second
        feedback.tokens_received = 10
        feedback.start_time = time.time() - 1.0

        eta_ms = feedback.estimated_remaining_ms
        # Should be ~9 seconds for remaining 90 tokens
        # (Allow ±2s variance due to timing)
        assert 7000 < eta_ms < 11000

    def test_eta_returns_negative_with_no_estimate(self):
        """ETA returns -1 when estimated_total is 0."""
        feedback = TokenFeedback(session_id="test-session", estimated_total=0)
        feedback.on_token()

        assert feedback.estimated_remaining_ms == -1.0

    def test_progress_bar_formats_correctly(self):
        """TokenFeedback.progress_bar() outputs valid format."""
        feedback = TokenFeedback(
            session_id="test-session",
            estimated_total=100,
        )

        # Receive 50 tokens
        for _ in range(50):
            feedback.on_token()

        bar = feedback.progress_bar()
        assert "50%" in bar
        assert "50 tokens" in bar
        assert "⏳ Processing" in bar

    def test_progress_callback_invoked(self):
        """TokenFeedback calls on_progress callback."""
        calls = []

        def callback(msg: str):
            calls.append(msg)

        feedback = TokenFeedback(
            session_id="test-session",
            estimated_total=100,
            on_progress=callback,
        )

        feedback.on_token()
        assert len(calls) == 1
        assert "⏳" in calls[0]

    def test_summary_returns_stats(self):
        """TokenFeedback.summary() includes all key metrics."""
        feedback = TokenFeedback(session_id="test-session")
        for _ in range(10):
            feedback.on_token()
            time.sleep(0.001)  # 1ms per token

        summary = feedback.summary()
        assert summary["session_id"] == "test-session"
        assert summary["total_tokens"] == 10
        assert summary["elapsed_seconds"] > 0
        assert summary["rate_tokens_per_sec"] > 0


class TestRoutingTimeline:
    """Phase 2: Activity timeline with stage timing."""

    def test_add_event_records_stage(self):
        """RoutingTimeline.add_event() stores one stage."""
        timeline = RoutingTimeline(session_id="test-session")

        timeline.add_event(
            RoutingEventType.CLASSIFY,
            {"complexity": "moderate"},
            elapsed_ms=0.5,
        )

        assert len(timeline.events) == 1
        assert timeline.events[0].event_type == RoutingEventType.CLASSIFY

    def test_timeline_maintains_order(self):
        """RoutingTimeline maintains chronological order."""
        timeline = RoutingTimeline(session_id="test-session")

        timeline.add_event(RoutingEventType.CLASSIFY, {}, 0.0)
        timeline.add_event(RoutingEventType.ROUTE, {}, 0.2)
        timeline.add_event(RoutingEventType.SEND, {}, 0.4)

        assert timeline.events[0].event_type == RoutingEventType.CLASSIFY
        assert timeline.events[1].event_type == RoutingEventType.ROUTE
        assert timeline.events[2].event_type == RoutingEventType.SEND

    def test_format_for_display_includes_all_stages(self):
        """RoutingTimeline.format_for_display() includes each stage."""
        timeline = RoutingTimeline(session_id="test-session")

        timeline.add_event(RoutingEventType.CLASSIFY, {"complexity": "complex"}, 0.2)
        timeline.add_event(RoutingEventType.ROUTE, {"model": "claude-opus"}, 0.3)
        timeline.add_event(RoutingEventType.SEND, {"tokens": 2100}, 1.5)
        timeline.add_event(RoutingEventType.TOKEN, {"tokens_received": 45}, 1.6)

        display = timeline.format_for_display()
        assert "Classified" in display
        assert "claude-opus" in display
        assert "2,100" in display
        assert "⏳" in display


class TestRoutingEvent:
    """Core event structure."""

    def test_routing_event_is_immutable(self):
        """RoutingEvent is frozen (immutable)."""
        event = RoutingEvent(
            timestamp=time.time(),
            session_id="test",
            event_type=RoutingEventType.TOKEN,
            elapsed_ms=100.0,
        )

        with pytest.raises(AttributeError):
            event.tokens_received = 10  # type: ignore

    def test_routing_event_to_row_serializes(self):
        """RoutingEvent.to_row() produces tuple for SQLite."""
        event = RoutingEvent(
            timestamp=1234567890.0,
            session_id="test-session",
            event_type=RoutingEventType.TOKEN,
            elapsed_ms=100.5,
            data={"count": 42},
        )

        row = event.to_row()
        assert row[0] == 1234567890.0
        assert row[1] == "test-session"
        assert row[2] == "token"
        assert row[3] == 100.5
        assert json.loads(row[4]) == {"count": 42}


class TestFeedbackStore:
    """SQLite persistence for routing events."""

    def test_feedback_store_creates_db(self, tmp_path: Path):
        """FeedbackStore._init_schema() creates database."""
        db_path = tmp_path / "feedback.db"
        FeedbackStore(db_path=db_path)

        assert db_path.exists()

        # Verify schema exists
        with sqlite3.connect(db_path) as conn:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='routing_events'"
            )
            assert cursor.fetchone() is not None

    def test_record_event_stores_single_event(self, tmp_path: Path):
        """FeedbackStore.record_event() inserts one event."""
        store = FeedbackStore(db_path=tmp_path / "test.db")

        event = RoutingEvent(
            timestamp=time.time(),
            session_id="test-session",
            event_type=RoutingEventType.TOKEN,
            elapsed_ms=100.0,
            data={"count": 5},
        )

        store.record_event(event)

        # Verify retrieval
        events = store.get_session_events("test-session")
        assert len(events) == 1
        assert events[0].event_type == RoutingEventType.TOKEN

    def test_record_events_batch_insert(self, tmp_path: Path):
        """FeedbackStore.record_events() batches multiple events."""
        store = FeedbackStore(db_path=tmp_path / "test.db")

        events = [
            RoutingEvent(
                timestamp=time.time() + i * 0.1,
                session_id="test-session",
                event_type=RoutingEventType.TOKEN,
                elapsed_ms=i * 100.0,
                data={"count": i},
            )
            for i in range(5)
        ]

        store.record_events(events)

        # Verify all inserted
        retrieved = store.get_session_events("test-session")
        assert len(retrieved) == 5

    def test_get_session_events_ordered(self, tmp_path: Path):
        """FeedbackStore.get_session_events() returns events in order."""
        store = FeedbackStore(db_path=tmp_path / "test.db")

        for i in range(3):
            event = RoutingEvent(
                timestamp=time.time() + i * 0.1,
                session_id="test",
                event_type=RoutingEventType.TOKEN,
                elapsed_ms=i * 100.0,
            )
            store.record_event(event)

        events = store.get_session_events("test")
        for i in range(len(events) - 1):
            assert events[i].elapsed_ms < events[i + 1].elapsed_ms

    def test_recent_sessions_returns_latest(self, tmp_path: Path):
        """FeedbackStore.recent_sessions() returns most recent sessions."""
        store = FeedbackStore(db_path=tmp_path / "test.db")

        # Create 3 sessions with different timestamps
        base_time = time.time()
        for session_idx in range(3):
            for i in range(2):
                event = RoutingEvent(
                    timestamp=base_time + session_idx * 100 + i * 10,
                    session_id=f"session-{session_idx}",
                    event_type=RoutingEventType.TOKEN,
                    elapsed_ms=0.0,
                )
                store.record_event(event)

        recent = store.recent_sessions(limit=2)
        # Should return sessions in reverse chronological order (newest first)
        assert len(recent) == 2
        assert recent[0] == "session-2"
        assert recent[1] == "session-1"
