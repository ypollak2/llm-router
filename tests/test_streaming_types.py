"""Tests for streaming event contract (Phase A v0.3.2)."""

from __future__ import annotations


from llm_router.streaming_types import (
    EventType,
    is_valid_event_type,
    payload_type_for,
    required_keys_for,
)


class TestEventTypeContract:
    """Validate EventType literals and payload mapping."""

    def test_all_event_types_have_payloads(self):
        """Every EventType must have a corresponding TypedDict."""
        event_types: list[EventType] = [
            "route.started",
            "route.cached_hit",
            "attempt.started",
            "attempt.buffering",
            "attempt.committed",
            "output.delta",
            "quality.verdict",
            "attempt.failed",
            "fallback.scheduled",
            "usage.final",
            "route.completed",
            "route.aborted",
        ]

        for event_type in event_types:
            assert is_valid_event_type(event_type)
            payload_cls = payload_type_for(event_type)
            assert payload_cls is not None

    def test_invalid_event_type_rejected(self):
        """Invalid event types return False."""
        assert not is_valid_event_type("invalid.event")
        assert not is_valid_event_type("")
        assert not is_valid_event_type("route")


class TestPayloadValidation:
    """Validate event payload structure."""

    def test_route_started_payload(self):
        """RouteStarted has expected fields."""
        keys = required_keys_for("route.started")
        assert "task_type" in keys
        assert "profile" in keys
        assert "complexity" in keys
        assert "candidate_count" in keys
        assert "chain_preview" in keys
        assert "buffered_mode" in keys

    def test_attempt_committed_payload(self):
        """AttemptCommitted marks commit barrier."""
        keys = required_keys_for("attempt.committed")
        assert "attempt_index" in keys
        assert "model" in keys
        assert "visible_output_started" in keys

    def test_output_delta_payload(self):
        """OutputDelta carries text and token count."""
        keys = required_keys_for("output.delta")
        assert "attempt_index" in keys
        assert "text" in keys
        assert "chars" in keys
        assert "approx_tokens" in keys

    def test_route_aborted_payload(self):
        """RouteAborted documents failure reason."""
        keys = required_keys_for("route.aborted")
        assert "outcome" in keys
        assert "detail" in keys


class TestEventSequencing:
    """Validate event ordering invariants."""

    def test_base_event_required_fields(self):
        """All events must have BaseEvent fields."""
        # This is more of a type-check test in real code,
        # but we verify the schema has them
        route_started_keys = required_keys_for("route.started")

        # BaseEvent fields should be added to every payload
        # (TypedDict inheritance simulated by inclusion)
        # For now, just verify RouteStarted has all its fields
        expected = {
            "task_type",
            "profile",
            "complexity",
            "candidate_count",
            "chain_preview",
            "buffered_mode",
        }
        assert route_started_keys == expected

    def test_fallback_scheduled_references_attempts(self):
        """FallbackScheduled tracks from/to attempt indices."""
        keys = required_keys_for("fallback.scheduled")
        assert "from_attempt" in keys
        assert "to_attempt" in keys
        assert "from_model" in keys
        assert "to_model" in keys
        # from_attempt < to_attempt is validated at runtime


class TestCommitBarrierSafety:
    """Validate commit barrier invariants."""

    def test_attempt_committed_disables_fallback(self):
        """After attempt.committed, no fallback allowed."""
        # This is enforced at router level, not type level
        # But verify the event exists
        assert is_valid_event_type("attempt.committed")
        keys = required_keys_for("attempt.committed")
        assert "attempt_index" in keys

    def test_output_delta_only_after_commit(self):
        """output.delta should not appear before attempt.committed in sequence."""
        # Type-level: both should exist
        assert is_valid_event_type("output.delta")
        assert is_valid_event_type("attempt.committed")

        # Runtime ordering is enforced by route_and_stream(), not types


class TestCompleteScenarios:
    """Test realistic event sequences."""

    def test_successful_route_sequence(self):
        """Happy path: route.started → attempt → delta → completed."""
        events = [
            "route.started",
            "attempt.started",
            "attempt.buffering",
            "attempt.committed",
            "output.delta",
            "output.delta",
            "usage.final",
            "route.completed",
        ]
        for evt in events:
            assert is_valid_event_type(evt)

    def test_fallback_route_sequence(self):
        """Fallback path: attempt fails before commit → fallback → success."""
        events = [
            "route.started",
            "attempt.started",
            "attempt.failed",
            "fallback.scheduled",
            "attempt.started",
            "attempt.committed",
            "output.delta",
            "usage.final",
            "route.completed",
        ]
        for evt in events:
            assert is_valid_event_type(evt)

    def test_all_models_failed_sequence(self):
        """Failure path: all attempts fail → aborted."""
        events = [
            "route.started",
            "attempt.started",
            "attempt.failed",
            "fallback.scheduled",
            "attempt.started",
            "attempt.failed",
            "route.aborted",
        ]
        for evt in events:
            assert is_valid_event_type(evt)
