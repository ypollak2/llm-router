"""Tests for Phase B v0.3.2 provider-level streaming events."""

from __future__ import annotations


from llm_router.providers import (
    ProviderStreamDelta,
    ProviderUsageInfo,
    ProviderStreamEvent,
)


class TestProviderStreamDelta:
    """Validate ProviderStreamDelta structure."""

    def test_delta_basic_fields(self):
        """ProviderStreamDelta carries text, chars, tokens."""
        delta: ProviderStreamDelta = {
            "text": "Hello, world!",
            "chars": 13,
            "approx_tokens": 3,
        }
        assert delta["text"] == "Hello, world!"
        assert delta["chars"] == 13
        assert delta["approx_tokens"] == 3

    def test_delta_empty_text(self):
        """ProviderStreamDelta can carry empty text (padding)."""
        delta: ProviderStreamDelta = {
            "text": "",
            "chars": 0,
            "approx_tokens": 0,
        }
        assert delta["text"] == ""
        assert delta["chars"] == 0


class TestProviderUsageInfo:
    """Validate ProviderUsageInfo structure."""

    def test_usage_final_fields(self):
        """ProviderUsageInfo carries final aggregated data."""
        usage: ProviderUsageInfo = {
            "input_tokens": 512,
            "output_tokens": 128,
            "cost_usd": 0.0042,
            "latency_ms": 2341.5,
        }
        assert usage["input_tokens"] == 512
        assert usage["output_tokens"] == 128
        assert usage["cost_usd"] == 0.0042
        assert usage["latency_ms"] == 2341.5

    def test_usage_zero_tokens(self):
        """ProviderUsageInfo handles zero tokens (edge case)."""
        usage: ProviderUsageInfo = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_usd": 0.0,
            "latency_ms": 100.0,
        }
        assert usage["input_tokens"] == 0
        assert usage["output_tokens"] == 0
        assert usage["cost_usd"] == 0.0


class TestProviderStreamEvent:
    """Validate ProviderStreamEvent union type."""

    def test_delta_event(self):
        """ProviderStreamEvent with type='delta'."""
        event: ProviderStreamEvent = {
            "type": "delta",
            "delta": {
                "text": "Hello",
                "chars": 5,
                "approx_tokens": 1,
            },
        }
        assert event["type"] == "delta"
        assert event["delta"]["text"] == "Hello"

    def test_usage_event(self):
        """ProviderStreamEvent with type='usage'."""
        event: ProviderStreamEvent = {
            "type": "usage",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cost_usd": 0.001,
                "latency_ms": 500.0,
            },
        }
        assert event["type"] == "usage"
        assert event["usage"]["input_tokens"] == 100
        assert event["usage"]["output_tokens"] == 50


class TestProviderStreamingContract:
    """Validate provider-level streaming contract invariants."""

    def test_delta_never_empty_except_padding(self):
        """Deltas carry at least metadata even if text is empty."""
        # Empty text is valid for padding (no-op chunks)
        delta: ProviderStreamDelta = {
            "text": "",
            "chars": 0,
            "approx_tokens": 0,
        }
        assert isinstance(delta["text"], str)
        assert isinstance(delta["chars"], int)
        assert isinstance(delta["approx_tokens"], int)

    def test_chars_matches_text_length(self):
        """The chars field should match len(text)."""
        text = "Hello, world!"
        delta: ProviderStreamDelta = {
            "text": text,
            "chars": len(text),
            "approx_tokens": max(1, len(text) // 4),
        }
        assert delta["chars"] == len(text)

    def test_approx_tokens_reasonable(self):
        """Approx tokens estimate ~1 token per 4 chars."""
        text = "This is a test of token estimation."
        expected_tokens = max(1, len(text) // 4)
        delta: ProviderStreamDelta = {
            "text": text,
            "chars": len(text),
            "approx_tokens": expected_tokens,
        }
        # Rough estimate: should be between 8-12 for 35-char text
        assert 5 <= delta["approx_tokens"] <= 15

    def test_usage_cost_non_negative(self):
        """Cost should never be negative."""
        usage: ProviderUsageInfo = {
            "input_tokens": 100,
            "output_tokens": 50,
            "cost_usd": 0.0,
            "latency_ms": 100.0,
        }
        assert usage["cost_usd"] >= 0

    def test_usage_latency_positive(self):
        """Latency should be positive (or zero for instant)."""
        usage: ProviderUsageInfo = {
            "input_tokens": 100,
            "output_tokens": 50,
            "cost_usd": 0.001,
            "latency_ms": 0.0,
        }
        assert usage["latency_ms"] >= 0


class TestProviderEventSequencing:
    """Validate realistic streaming event sequences."""

    def test_simple_stream_sequence(self):
        """Typical sequence: multiple deltas then usage."""
        events: list[ProviderStreamEvent] = [
            {
                "type": "delta",
                "delta": {
                    "text": "Hello",
                    "chars": 5,
                    "approx_tokens": 1,
                },
            },
            {
                "type": "delta",
                "delta": {
                    "text": " ",
                    "chars": 1,
                    "approx_tokens": 0,
                },
            },
            {
                "type": "delta",
                "delta": {
                    "text": "world!",
                    "chars": 6,
                    "approx_tokens": 2,
                },
            },
            {
                "type": "usage",
                "usage": {
                    "input_tokens": 50,
                    "output_tokens": 12,
                    "cost_usd": 0.0012,
                    "latency_ms": 1500.0,
                },
            },
        ]

        # All events are valid
        for event in events:
            if event["type"] == "delta":
                assert "delta" in event
                assert "text" in event["delta"]
            elif event["type"] == "usage":
                assert "usage" in event
                assert "input_tokens" in event["usage"]

    def test_empty_stream_no_output(self):
        """Edge case: no deltas, just usage (empty response)."""
        events: list[ProviderStreamEvent] = [
            {
                "type": "usage",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 0,
                    "cost_usd": 0.0,
                    "latency_ms": 500.0,
                },
            },
        ]
        assert len(events) == 1
        assert events[0]["type"] == "usage"
        assert events[0]["usage"]["output_tokens"] == 0

    def test_usage_final_only_once(self):
        """A stream should have exactly one usage event."""
        events: list[ProviderStreamEvent] = [
            {"type": "delta", "delta": {"text": "x", "chars": 1, "approx_tokens": 0}},
            {
                "type": "usage",
                "usage": {
                    "input_tokens": 50,
                    "output_tokens": 10,
                    "cost_usd": 0.001,
                    "latency_ms": 1000.0,
                },
            },
        ]

        usage_count = sum(1 for e in events if e["type"] == "usage")
        assert usage_count == 1, "Stream should have exactly one usage event"
