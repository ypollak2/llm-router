"""Real-time routing feedback — token streaming, ETA, activity timeline.

This module provides three layers of user-visible feedback:
  1. TokenFeedback (MVP): Token counter + ETA
  2. RoutingTimeline (Phase 2): Detailed stage timing
  3. ThinkingExtractor (Phase 3): Model-specific thinking extraction

All feedback is emitted as events and stored in SQLite for later retrieval.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional


class RoutingEventType(str, Enum):
    """Enum of all possible routing stages."""
    CLASSIFY = "classify"      # Task classification complete
    ROUTE = "route"            # Model selected from chain
    SEND = "send"              # Prompt sent to model
    TOKEN = "token"            # Token received from stream
    THINKING = "thinking"      # Thinking block extracted
    DONE = "done"              # Response complete


@dataclass(frozen=True)
class RoutingEvent:
    """One event in a routing session — immutable."""
    timestamp: float            # Absolute time (time.time())
    session_id: str             # Session identifier (UUID)
    event_type: RoutingEventType
    elapsed_ms: float           # Time since session start
    data: dict[str, Any] = field(default_factory=dict)

    def to_row(self) -> tuple:
        """Serialize for SQLite storage."""
        return (
            self.timestamp,
            self.session_id,
            self.event_type.value,
            self.elapsed_ms,
            json.dumps(self.data),
        )


@dataclass
class TokenFeedback:
    """Phase 1 MVP: Real-time token counter + ETA calculation.

    Hooks into LiteLLM streaming callbacks to track:
      - Tokens received so far
      - Tokens/second rate
      - ETA based on rate extrapolation
      - Visual progress display

    Example usage:
        feedback = TokenFeedback(session_id="xyz", on_progress=print_progress_bar)
        for chunk in client.messages.stream(...):
            feedback.on_token(chunk)
        print(feedback.summary())
    """

    session_id: str
    start_time: float = field(default_factory=time.time)
    tokens_received: int = 0
    on_progress: Optional[Callable[[str], None]] = None
    events: list[RoutingEvent] = field(default_factory=list)

    # Estimated total tokens (used for ETA). May be refined over time.
    estimated_total: int = 0

    @property
    def elapsed_seconds(self) -> float:
        """Time since session start."""
        return time.time() - self.start_time

    @property
    def rate_tokens_per_sec(self) -> float:
        """Current throughput."""
        if self.elapsed_seconds < 0.001:  # 1ms minimum to avoid division by very small number
            return 0.0
        return self.tokens_received / self.elapsed_seconds

    @property
    def estimated_remaining_ms(self) -> float:
        """ETA in milliseconds (or -1 if no estimate available)."""
        if self.estimated_total <= 0 or self.rate_tokens_per_sec <= 0:
            return -1.0
        remaining_tokens = max(0, self.estimated_total - self.tokens_received)
        return (remaining_tokens / self.rate_tokens_per_sec) * 1000.0

    def on_token(self, count: int = 1) -> None:
        """Called each time a token is received from the LLM stream."""
        self.tokens_received += count

        # Emit progress event
        event = RoutingEvent(
            timestamp=time.time(),
            session_id=self.session_id,
            event_type=RoutingEventType.TOKEN,
            elapsed_ms=self.elapsed_seconds * 1000,
            data={
                "tokens_received": self.tokens_received,
                "rate_tokens_per_sec": self.rate_tokens_per_sec,
            },
        )
        self.events.append(event)

        # Call progress callback if provided
        if self.on_progress:
            self.on_progress(self.progress_bar())

    def progress_bar(self, width: int = 20) -> str:
        """Generate a terminal-friendly progress bar.

        Example output:
          "⏳ Processing... [████░░░░░] 45% · 1,245 tokens · ~1s remaining"
        """
        if self.estimated_total <= 0:
            # No estimate yet — just show token count
            pct = 0
            filled = 0
        else:
            pct = int((self.tokens_received / self.estimated_total) * 100)
            pct = min(pct, 100)
            filled = int((pct / 100) * width)

        bar = "█" * filled + "░" * (width - filled)

        # Format ETA
        eta_ms = self.estimated_remaining_ms
        if eta_ms < 0:
            eta_str = "? remaining"
        elif eta_ms < 1000:
            eta_str = f"~{int(eta_ms / 100) * 100}ms remaining"
        else:
            eta_str = f"~{int(eta_ms / 1000)}s remaining"

        return (
            f"⏳ Processing... [{bar}] {pct}%\n"
            f"   {self.tokens_received:,} tokens ({self.elapsed_seconds:.1f}s) · {eta_str}"
        )

    def summary(self) -> dict[str, Any]:
        """Return summary stats for this feedback session."""
        return {
            "session_id": self.session_id,
            "total_tokens": self.tokens_received,
            "elapsed_seconds": self.elapsed_seconds,
            "rate_tokens_per_sec": self.rate_tokens_per_sec,
            "event_count": len(self.events),
        }


@dataclass
class RoutingTimeline:
    """Phase 2: Detailed timeline showing each routing stage.

    Stages tracked:
      - classify: Task classification (complexity, type, signals)
      - route: Model selection from chain
      - send: Prompt transmission
      - receive: Token receiving (ongoing)

    Example output:
        ✓ 0.0s Classified as reasoning/complex (+0.2ms)
        ✓ 0.2s Selected model: claude-opus (+0.1ms)
        ✓ 0.3s Sent prompt (2.1k tokens, +1.2ms)
        ⏳ 1.5s Receiving response (45 tokens)
    """

    session_id: str
    events: list[RoutingEvent] = field(default_factory=list)

    def add_event(
        self,
        event_type: RoutingEventType,
        data: dict[str, Any],
        elapsed_ms: float,
    ) -> None:
        """Record one stage completion."""
        event = RoutingEvent(
            timestamp=time.time(),
            session_id=self.session_id,
            event_type=event_type,
            elapsed_ms=elapsed_ms,
            data=data,
        )
        self.events.append(event)

    def format_for_display(self) -> str:
        """Return formatted timeline for terminal display."""
        lines = ["📋 Timeline:"]

        for event in self.events:
            elapsed_sec = event.elapsed_ms / 1000.0

            if event.event_type == RoutingEventType.CLASSIFY:
                detail = event.data.get("complexity", "unknown")
                lines.append(f"  ✓ {elapsed_sec:.1f}s Classified as {detail}")

            elif event.event_type == RoutingEventType.ROUTE:
                model = event.data.get("model", "unknown")
                lines.append(f"  ✓ {elapsed_sec:.1f}s Selected: {model}")

            elif event.event_type == RoutingEventType.SEND:
                tokens = event.data.get("tokens", 0)
                lines.append(f"  ✓ {elapsed_sec:.1f}s Sent {tokens:,} token prompt")

            elif event.event_type == RoutingEventType.TOKEN:
                count = event.data.get("tokens_received", 0)
                lines.append(f"  ⏳ {elapsed_sec:.1f}s Receiving... ({count} tokens)")

            elif event.event_type == RoutingEventType.DONE:
                lines.append(f"  ✓ {elapsed_sec:.1f}s Complete")

        return "\n".join(lines)


class FeedbackStore:
    """SQLite storage for routing events (all 3 phases).

    Enables historical review: `llm_router summary --history`
    """

    def __init__(self, db_path: Path | str = "~/.llm-router/feedback.db"):
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        """Create tables if they don't exist."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS routing_events (
                    timestamp REAL NOT NULL,
                    session_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    elapsed_ms REAL NOT NULL,
                    data TEXT NOT NULL,  -- JSON
                    PRIMARY KEY (session_id, timestamp)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_session_timestamp
                ON routing_events(session_id, timestamp)
            """)
            conn.commit()

    def record_event(self, event: RoutingEvent) -> None:
        """Store one event to SQLite."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO routing_events
                (timestamp, session_id, event_type, elapsed_ms, data)
                VALUES (?, ?, ?, ?, ?)
                """,
                event.to_row(),
            )
            conn.commit()

    def record_events(self, events: list[RoutingEvent]) -> None:
        """Batch store multiple events."""
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                """
                INSERT INTO routing_events
                (timestamp, session_id, event_type, elapsed_ms, data)
                VALUES (?, ?, ?, ?, ?)
                """,
                [e.to_row() for e in events],
            )
            conn.commit()

    def get_session_events(self, session_id: str) -> list[RoutingEvent]:
        """Retrieve all events for a session in order."""
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT timestamp, session_id, event_type, elapsed_ms, data
                FROM routing_events
                WHERE session_id = ?
                ORDER BY timestamp ASC
                """,
                (session_id,),
            ).fetchall()

        return [
            RoutingEvent(
                timestamp=row[0],
                session_id=row[1],
                event_type=RoutingEventType(row[2]),
                elapsed_ms=row[3],
                data=json.loads(row[4]),
            )
            for row in rows
        ]

    def recent_sessions(self, limit: int = 50) -> list[str]:
        """Get most recent N unique session IDs."""
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT session_id, MAX(timestamp) as last_event
                FROM routing_events
                GROUP BY session_id
                ORDER BY last_event DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [row[0] for row in rows]
