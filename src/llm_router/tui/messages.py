"""Message types for TUI event communication.

Defines custom message classes for inter-widget communication in the TUI dashboard,
particularly for streaming events from route_and_stream() API.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from textual.message import Message


class StreamEventMessage(Message):
    """Message carrying a RouterStreamEvent for TUI display.

    Posted by the streaming worker whenever route_and_stream() yields an event.
    Listeners can subscribe to specific event types via the event's type field.
    """

    def __init__(self, event: dict[str, Any]) -> None:
        """Initialize with a router event."""
        super().__init__()
        self.event = event
        self.correlation_id = event.get("correlation_id", "")
        self.sequence = event.get("seq", 0)


@dataclass
class MetricsUpdateMessage(Message):
    """Message to update metrics panel with aggregated statistics."""

    elapsed_ms: float = 0.0
    tokens_received: int = 0
    tokens_per_second: float = 0.0
    total_cost: float = 0.0
    current_model: str = "N/A"
    attempt_index: int = 0
    confidence_percent: float = 0.0


@dataclass
class TimelineUpdateMessage(Message):
    """Message to update timeline panel with stage progress."""

    stage_name: str
    status: str = "pending"  # pending, success, failed
    details: str = ""
    duration_ms: float = 0.0
    icon: str = "⏳"


@dataclass
class OutputDeltaMessage(Message):
    """Message to append content to live output panel."""

    text: str
    model: str = ""
    is_thinking: bool = False


@dataclass
class SessionReplayStartMessage(Message):
    """Message to start session replay from stored events."""

    session_id: str
    events: list[dict[str, Any]]
    replay_speed: float = 1.0  # Multiplier on original delays


@dataclass
class SessionReplayPauseMessage(Message):
    """Message to pause current session replay."""

    paused: bool = True


@dataclass
class ModalOpenMessage(Message):
    """Message to open a modal dialog."""

    modal_type: str  # "help", "cost_chart", "history"
    data: dict[str, Any] | None = None


@dataclass
class ModalCloseMessage(Message):
    """Message to close current modal."""

    modal_type: str = ""


@dataclass
class ThemeChangeMessage(Message):
    """Message to change TUI theme."""

    theme_name: str  # "light", "dark", "monokai", etc.
