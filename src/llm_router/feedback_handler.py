"""Unified feedback handler orchestrating Phases 1-3 with CLI display.

Manages real-time events from routing and streaming, provides statistics
for token counting (Phase 1), activity timeline (Phase 2), and thinking
extraction (Phase 3).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Optional
from uuid import uuid4


@dataclass
class TimelineEvent:
    """One event in the request lifecycle."""
    name: str
    timestamp: float = field(default_factory=time.monotonic)
    status: str = "pending"  # "pending" → "success" or "failure"
    details: str = ""
    duration_ms: float = 0.0

    def to_display(self) -> str:
        """Format for terminal display."""
        icon = "✓" if self.status == "success" else "⏳"
        elapsed = self.timestamp
        duration_str = f" (+{self.duration_ms:.1f}ms)" if self.duration_ms > 0 else ""
        return f"  {icon} {elapsed:.1f}s {self.name}{duration_str}" + (
            f" — {self.details}" if self.details else ""
        )


class FeedbackHandler:
    """Orchestrates real-time feedback across all 3 phases.

    Usage:
        handler = FeedbackHandler()
        handler.on_routing_start()
        handler.on_classification(complexity="moderate", method="heuristic")
        handler.on_model_selected(model="claude-opus")
        handler.on_send(token_count=2100)

        # During streaming
        async for chunk in stream:
            handler.on_token(chunk)

        handler.on_complete()
        print(handler.format_display())
    """

    def __init__(self):
        self.session_id: str = str(uuid4())
        self.start_time: Optional[float] = None
        self.first_token_time: Optional[float] = None

        # Phase 1: Token Counter & ETA
        self.token_count: int = 0
        self.tokens_per_second: float = 0.0
        self.estimated_total_tokens: int = 0

        # Phase 2: Activity Timeline
        self.timeline: list[TimelineEvent] = []
        self._last_event_start: Optional[float] = None

        # Phase 3: Thinking Extraction
        self.is_in_thinking_block: bool = False
        self.thinking_content: str = ""
        self.final_thinking_blocks: list[str] = []

    def on_routing_start(self) -> None:
        """Begin routing session."""
        self.start_time = time.monotonic()
        self._add_event("Routing", "pending")

    def on_classification(self, complexity: str, method: str) -> None:
        """Record classification result."""
        self._end_current_event("success", f"{complexity} ({method})")
        self._add_event("Classification", "success", complexity)

    def on_model_selected(self, model: str) -> None:
        """Record model selection."""
        self._add_event("Model Selected", "success", model)

    def on_send(self, token_count: int) -> None:
        """Record prompt transmission."""
        self._add_event("Sending", "success", f"{token_count:,} tokens")

    def on_token(self, content: str = "", count: int = 1) -> None:
        """Called for each token received."""
        if self.first_token_time is None:
            self.first_token_time = time.monotonic()
            # Mark send as complete
            if self.timeline:
                self.timeline[-1].status = "success"

        self.token_count += count

        # Calculate rate
        if self.first_token_time:
            elapsed = time.monotonic() - self.first_token_time
            if elapsed > 0:
                self.tokens_per_second = self.token_count / elapsed

        # Phase 3: Extract thinking blocks
        self._extract_thinking(content)

    def on_complete(self) -> None:
        """Mark streaming complete."""
        self._add_event("Complete", "success")

    def _add_event(self, name: str, status: str, details: str = "") -> None:
        """Add timeline event."""
        if self.start_time is None:
            return

        elapsed = time.monotonic() - self.start_time
        event = TimelineEvent(
            name=name,
            timestamp=elapsed,
            status=status,
            details=details,
        )
        self.timeline.append(event)
        self._last_event_start = time.monotonic()

    def _end_current_event(self, status: str, details: str = "") -> None:
        """Update last event with completion info."""
        if self.timeline and self._last_event_start:
            self.timeline[-1].status = status
            self.timeline[-1].details = details
            self.timeline[-1].duration_ms = (
                time.monotonic() - self._last_event_start
            ) * 1000

    def _extract_thinking(self, text: str) -> None:
        """Parse Claude thinking blocks (stateful parser)."""
        if "<thinking>" in text:
            self.is_in_thinking_block = True
            self.thinking_content = ""

        if self.is_in_thinking_block:
            # Accumulate content between markers
            self.thinking_content += text.replace("<thinking>", "").replace(
                "</thinking>", ""
            )

        if "</thinking>" in text:
            self.is_in_thinking_block = False
            # Store completed thinking block
            clean = self.thinking_content.strip()
            if clean:
                self.final_thinking_blocks.append(clean)
            self.thinking_content = ""

    @property
    def elapsed_ms(self) -> float:
        """Total elapsed time in milliseconds."""
        if self.start_time is None:
            return 0.0
        return (time.monotonic() - self.start_time) * 1000

    @property
    def eta_ms(self) -> float:
        """Estimated remaining time (or -1 if no estimate)."""
        if self.estimated_total_tokens <= 0 or self.tokens_per_second <= 0:
            return -1.0
        remaining = max(0, self.estimated_total_tokens - self.token_count)
        return (remaining / self.tokens_per_second) * 1000.0

    def progress_bar(self, width: int = 20) -> str:
        """Terminal-friendly progress bar for Phase 1."""
        if self.estimated_total_tokens <= 0:
            pct = 0
            filled = 0
        else:
            pct = int((self.token_count / self.estimated_total_tokens) * 100)
            pct = min(pct, 100)
            filled = int((pct / 100) * width)

        bar = "█" * filled + "░" * (width - filled)

        # Format ETA
        eta_ms = self.eta_ms
        if eta_ms < 0:
            eta_str = "? remaining"
        elif eta_ms < 1000:
            eta_str = f"~{int(eta_ms / 100) * 100}ms remaining"
        else:
            eta_str = f"~{int(eta_ms / 1000)}s remaining"

        return (
            f"⏳ Processing... [{bar}] {pct}%\n"
            f"   {self.token_count:,} tokens · "
            f"{self.tokens_per_second:.0f} tokens/sec · {eta_str}"
        )

    def format_display(self) -> str:
        """Format all phases for terminal display."""
        lines = []

        # Header with session info
        lines.append(f"🎯 Session: {self.session_id[:8]}...")
        lines.append("")

        # Phase 2: Timeline
        lines.append("📋 Timeline:")
        for event in self.timeline:
            lines.append(event.to_display())
        lines.append("")

        # Phase 1: Token counter & ETA
        if self.token_count > 0:
            lines.append("⏳ Progress:")
            lines.append(self.progress_bar())
            lines.append("")

        # Phase 3: Thinking (if present)
        if self.final_thinking_blocks:
            lines.append("🧠 Model's Thinking:")
            for i, block in enumerate(self.final_thinking_blocks, 1):
                # Truncate to first 300 chars for display
                preview = block[:300] + ("..." if len(block) > 300 else "")
                lines.append(f"  [{i}] {preview}")
            lines.append("")

        return "\n".join(lines)

    def to_json(self) -> str:
        """Serialize to JSON for storage."""
        return json.dumps({
            "session_id": self.session_id,
            "elapsed_ms": self.elapsed_ms,
            "token_count": self.token_count,
            "tokens_per_second": self.tokens_per_second,
            "timeline": [
                {
                    "name": e.name,
                    "timestamp": e.timestamp,
                    "status": e.status,
                    "details": e.details,
                    "duration_ms": e.duration_ms,
                }
                for e in self.timeline
            ],
            "thinking_blocks": self.final_thinking_blocks,
        })
