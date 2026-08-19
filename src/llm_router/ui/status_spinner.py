"""Live routing feedback with animated status spinner.

Shows real-time feedback when llm_router is routing a call:
  ⚡ Classifying prompt...  [████░░░░░░] 35%  ~2.3s
  → Routing to claude-opus
  ✓ Complete (gpt-4o bypass)
"""

from __future__ import annotations

import time
from typing import Optional

from rich.console import Console
from rich.status import Status

from llm_router.ui.theme import PALETTE, progress_bar


class RoutingStatusSpinner:
    """Display animated feedback while routing a prompt."""

    def __init__(self, console: Optional[Console] = None):
        """Initialize spinner with optional console."""
        self.console = console or Console()
        self.start_time = time.time()
        self.status: Optional[Status] = None

    def start(self, stage: str = "Classifying") -> None:
        """Start showing routing status.

        Args:
            stage: Initial stage name (e.g., "Classifying", "Routing")
        """
        self.start_time = time.time()
        message = f"[{PALETTE.warning}]⚡[/] {stage}..."
        self.status = self.console.status(message, spinner="dots")
        self.status.start()

    def update(self, stage: str, model: str = "", progress: float = 0.0) -> None:
        """Update the current stage.

        Args:
            stage: Stage name (e.g., "Routing to claude-opus")
            model: Model being routed to (optional)
            progress: Progress 0-100 (optional)
        """
        if not self.status:
            return

        elapsed = time.time() - self.start_time
        eta = 2.5 if elapsed < 0.5 else 0.0  # Simple ETA

        # Color based on stage
        if "Routing" in stage:
            color = PALETTE.accent
            icon = "→"
        elif "Complete" in stage or "✓" in stage:
            color = PALETTE.success
            icon = "✓"
        else:
            color = PALETTE.warning
            icon = "⚡"

        # Build message
        message = f"[{color}]{icon}[/] {stage}"

        if progress > 0:
            message += f"  {progress_bar(progress)}  {progress:.0f}%"

        if eta > 0:
            message += f"  ~{eta:.1f}s"

        self.status.update(message)

    def complete(self, model: str = "", decision_reason: str = "") -> None:
        """Mark routing as complete.

        Args:
            model: Model that was selected
            decision_reason: Why this model was chosen (e.g., "via heuristic")
        """
        if not self.status:
            return

        reason = f" ({decision_reason})" if decision_reason else ""
        message = f"[{PALETTE.success}]✓[/] Routed to [bold]{model}[/bold]{reason}"

        self.status.update(message)
        self.status.stop()
        self.console.print(message)

    def error(self, reason: str) -> None:
        """Mark routing as failed.

        Args:
            reason: Error reason
        """
        if self.status:
            self.status.stop()

        message = f"[{PALETTE.error}]✗[/] Routing failed: {reason}"
        self.console.print(message)


def show_routing_progress(
    stage: str,
    model: str = "",
    decision_reason: str = "",
    console: Optional[Console] = None,
) -> None:
    """Show a single-line routing status (non-animated fallback).

    Args:
        stage: Routing stage (e.g., "Classifying", "Routing", "Complete")
        model: Model name (if routed)
        decision_reason: How decision was made
        console: Optional Rich Console
    """
    console = console or Console()

    # Icon & color based on stage
    if "Complete" in stage or "✓" in stage:
        icon = "✓"
        color = PALETTE.success
    elif "Routing" in stage or "→" in stage:
        icon = "→"
        color = PALETTE.accent
    else:
        icon = "⚡"
        color = PALETTE.warning

    # Build message
    message = f"[{color}]{icon}[/] {stage}"

    if model:
        message += f" [bold]{model}[/bold]"

    if decision_reason:
        message += f" [dim]({decision_reason})[/dim]"

    console.print(message)
