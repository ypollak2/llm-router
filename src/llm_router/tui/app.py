"""LLM Router TUI Application — Main Dashboard.

Textual-based terminal dashboard for real-time monitoring of LLM routing,
streaming output, cost tracking, and session replay.
"""

from __future__ import annotations

import asyncio
from typing import Any

from textual.app import App, ComposeResult
from textual.containers import Container, Grid
from textual.reactive import reactive
from textual.widgets import Header, Footer
from textual.binding import Binding

from llm_router.tui.messages import (
    StreamEventMessage,
    MetricsUpdateMessage,
    ModalOpenMessage,
)
from llm_router.tui.panels import (
    TimelinePanel,
    MetricsPanel,
    OutputPanel,
    SparklinePanel,
    ModelBreakdownPanel,
    QuotaPanel,
)


class LLMRouterDashboard(App[None]):
    """Main TUI application for LLM Router routing visualization.

    Features:
      - Real-time streaming output with syntax highlighting
      - Route progress timeline with stage indicators
      - Live metrics (tokens, cost, latency, throughput)
      - Interactive keyboard navigation
      - Session replay capability
      - Cost trend analysis

    Keyboard Shortcuts:
      ↓/↑    Scroll output / timeline
      C      Toggle cost chart modal
      R      Replay last session
      H      Show help
      Space  Pause/resume streaming
      Q      Quit
    """

    TITLE = "LLM Router Router — Real-time LLM Routing"
    SUB_TITLE = "Session: Loading... | v0.3.5"

    CSS_PATH = "dashboard.css"
    BINDINGS = [
        Binding("c", "show_cost_chart", "Cost Chart", show=True),
        Binding("r", "replay_session", "Replay", show=True),
        Binding("h", "show_help", "Help", show=True),
        Binding("space", "toggle_pause", "Pause", show=True),
        Binding("q", "quit", "Quit", show=True),
    ]

    # Reactive state
    session_id: reactive[str] = reactive("")
    total_cost: reactive[float] = reactive(0.0)
    tokens_received: reactive[int] = reactive(0)
    tokens_per_second: reactive[float] = reactive(0.0)
    current_model: reactive[str] = reactive("N/A")
    is_paused: reactive[bool] = reactive(False)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize the dashboard."""
        super().__init__(*args, **kwargs)
        self.session_events: list[dict[str, Any]] = []
        self.is_streaming = False
        self.committed = False

    def compose(self) -> ComposeResult:
        """Create the dashboard layout."""
        yield Header(show_clock=True)

        with Grid(id="main-grid"):
            # Timeline panel (left column, top)
            yield Container(
                TimelinePanel(id="timeline-panel"),
                id="container-timeline",
            )

            # Output panel (right column, spans 2 rows)
            yield Container(
                OutputPanel(id="output-panel"),
                id="container-output",
            )

            # Metrics panel (left column, middle)
            yield Container(
                MetricsPanel(id="metrics-panel"),
                id="container-metrics",
            )

            # Sparkline panel (left column, bottom)
            yield Container(
                SparklinePanel(id="sparkline-panel"),
                id="container-sparkline",
            )

            # Model breakdown panel (right column, bottom-left)
            yield Container(
                ModelBreakdownPanel(id="breakdown-panel"),
                id="container-breakdown",
            )

            # Quota panel (right column, bottom-right)
            yield Container(
                QuotaPanel(id="quota-panel"),
                id="container-quota",
            )

        yield Footer()

    def on_mount(self) -> None:
        """Initialize dashboard after mount."""
        # Set initial title
        self.title = "LLM Router Router v0.3.5 — Starting..."
        self.sub_title = "Ready to route requests"

        # Load and display historical data
        self._load_historical_data()

        # Set focus to output panel
        self.query_one("#output-panel", OutputPanel).focus()

    def on_stream_event_message(self, message: StreamEventMessage) -> None:
        """Handle streaming event from router.

        Routes events to appropriate panels based on event type.
        """
        event = message.event
        event_type = event.get("type", "")

        # Store event for replay
        self.session_events.append(event)

        # Extract correlation ID
        if not self.session_id and "correlation_id" in event:
            self.session_id = event["correlation_id"][:8]
            self.sub_title = f"Session: {self.session_id}"

        # Route to appropriate handler
        if event_type == "route.started":
            self._handle_route_started(event)
        elif event_type == "attempt.started":
            self._handle_attempt_started(event)
        elif event_type == "attempt.committed":
            self._handle_attempt_committed(event)
            self.committed = True
        elif event_type == "output.delta":
            self._handle_output_delta(event)
        elif event_type == "usage.final":
            self._handle_usage_final(event)
        elif event_type == "route.completed":
            self._handle_route_completed(event)
        elif event_type == "route.aborted":
            self._handle_route_aborted(event)

    def _handle_route_started(self, event: dict[str, Any]) -> None:
        """Handle route.started event."""
        timeline: TimelinePanel = self.query_one("#timeline-panel", TimelinePanel)
        timeline.add_stage(
            name="Route Started",
            status="success",
            details=f"{event.get('candidate_count', 0)} models available",
        )

    def _handle_attempt_started(self, event: dict[str, Any]) -> None:
        """Handle attempt.started event."""
        model = event.get("model", "unknown")
        attempt = event.get("attempt_index", 1)
        self.current_model = model

        timeline: TimelinePanel = self.query_one("#timeline-panel", TimelinePanel)
        timeline.add_stage(
            name=f"Attempt {attempt}",
            status="pending",
            details=model,
        )

    def _handle_attempt_committed(self, event: dict[str, Any]) -> None:
        """Handle attempt.committed event (commit barrier)."""
        timeline: TimelinePanel = self.query_one("#timeline-panel", TimelinePanel)
        timeline.add_stage(
            name="Committed ✨",
            status="success",
            details="Output started (no fallback)",
        )

    def _handle_output_delta(self, event: dict[str, Any]) -> None:
        """Handle output.delta event."""
        if self.is_paused:
            return

        text = event.get("text", "")

        self.tokens_received += event.get("approx_tokens", 0)

        output: OutputPanel = self.query_one("#output-panel", OutputPanel)
        output.append_text(text)

    def _handle_usage_final(self, event: dict[str, Any]) -> None:
        """Handle usage.final event."""
        self.total_cost += event.get("cost_usd", 0.0)

        metrics: MetricsPanel = self.query_one("#metrics-panel", MetricsPanel)
        metrics.update_metrics(
            model=event.get("model", self.current_model),
            input_tokens=event.get("input_tokens", 0),
            output_tokens=event.get("output_tokens", 0),
            cost_usd=self.total_cost,
            latency_ms=event.get("latency_ms", 0.0),
        )

    def _handle_route_completed(self, event: dict[str, Any]) -> None:
        """Handle route.completed event."""
        model = event.get("final_model", "unknown")
        timeline: TimelinePanel = self.query_one("#timeline-panel", TimelinePanel)
        timeline.add_stage(
            name="Completed",
            status="success",
            details=f"Final model: {model}",
        )
        self.is_streaming = False

    def _handle_route_aborted(self, event: dict[str, Any]) -> None:
        """Handle route.aborted event."""
        outcome = event.get("outcome", "unknown")
        timeline: TimelinePanel = self.query_one("#timeline-panel", TimelinePanel)
        timeline.add_stage(
            name="Aborted",
            status="failed",
            details=outcome,
        )
        self.is_streaming = False

    def on_metrics_update_message(self, message: MetricsUpdateMessage) -> None:
        """Handle metrics update."""
        self.tokens_received = message.tokens_received
        self.tokens_per_second = message.tokens_per_second
        self.total_cost = message.total_cost
        self.current_model = message.current_model

        metrics: MetricsPanel = self.query_one("#metrics-panel", MetricsPanel)
        metrics.update_metrics(
            model=message.current_model,
            input_tokens=0,  # Aggregated in message
            output_tokens=message.tokens_received,
            cost_usd=message.total_cost,
            latency_ms=0.0,
        )

    def action_show_cost_chart(self) -> None:
        """Show cost trend chart modal."""
        self.post_message(ModalOpenMessage("cost_chart"))

    def action_replay_session(self) -> None:
        """Replay last session from stored events."""
        if self.session_events:
            self.post_message(
                ModalOpenMessage(
                    "replay",
                    {"events": self.session_events, "session_id": self.session_id},
                )
            )
        else:
            self.notify("No session events to replay", severity="warning")

    def action_show_help(self) -> None:
        """Show help modal."""
        self.post_message(ModalOpenMessage("help"))

    def action_toggle_pause(self) -> None:
        """Pause/resume streaming output."""
        self.is_paused = not self.is_paused
        status = "paused" if self.is_paused else "resumed"
        self.notify(f"Streaming {status}")

    def notify(
        self, message: str, title: str = "Info", severity: str = "information"
    ) -> None:
        """Show a notification to the user."""
        # Notification would go to footer or a dedicated Toast widget

    def _load_historical_data(self) -> None:
        """Load and display historical data from database."""
        try:
            from llm_router.dashboard_data import query_daily, query_window, query_model_distribution
            from llm_router.claude_usage import get_claude_pressure

            # Load 14-day cost history
            daily_rows = query_daily(days=14)
            daily_costs = [row.saved_usd for row in daily_rows]

            # Get total savings
            total_row = query_window("week")
            total_savings = total_row.saved_usd

            sparkline: SparklinePanel = self.query_one("#sparkline-panel", SparklinePanel)
            sparkline.update_sparkline(daily_costs, total_savings)

            # Load model breakdown from 14-day usage
            model_counts = query_model_distribution(days=14)
            total_calls = sum(model_counts.values())
            model_stats = {
                model: (count / total_calls) * 100
                for model, count in model_counts.items()
            } if total_calls > 0 else {}

            breakdown: ModelBreakdownPanel = self.query_one("#breakdown-panel", ModelBreakdownPanel)
            breakdown.update_breakdown(model_stats)

            # Load quota information
            claude_pressure = get_claude_pressure()
            claude_pct = claude_pressure.weekly_pct if claude_pressure else 0.0
            claude_remaining = f"{100 - claude_pct:.0f}%"

            quota: QuotaPanel = self.query_one("#quota-panel", QuotaPanel)
            quota.update_quotas(
                claude_quota_pct=claude_pct,
                gemini_quota_pct=0.0,
                claude_remaining=claude_remaining,
                gemini_remaining="Unknown",
            )
        except Exception as e:
            import logging
            logging.warning(f"Failed to load historical dashboard data: {e}")


async def run_dashboard(
    task_type: str,
    prompt: str,
    **routing_kwargs: Any,
) -> None:
    """Run the LLM Router TUI dashboard for a routing request.

    This is the main entry point for the TUI mode. It launches the Textual
    application and streams routing events into the dashboard.

    Args:
        task_type: Task type for routing (query, code, analyze, etc.)
        prompt: User prompt to route
        **routing_kwargs: Additional arguments for route_and_stream()
    """
    from llm_router.router import route_and_stream

    app = LLMRouterDashboard()

    async def stream_worker() -> None:
        """Background worker that streams routing events into the TUI."""
        try:
            async for event in route_and_stream(
                task_type=task_type,
                prompt=prompt,
                **routing_kwargs,
            ):
                message = StreamEventMessage(event)
                app.post_message(message)
                # Brief yield to let TUI process the message
                await asyncio.sleep(0.001)
        except Exception as e:
            app.notify(f"Routing error: {str(e)}", severity="error")

    async def run_app() -> None:
        """Run the app and stream worker concurrently."""
        async with app.run_test():
            # Start streaming worker
            task = asyncio.create_task(stream_worker())
            try:
                # Keep app running until it exits
                await asyncio.sleep(float("inf"))
            except asyncio.CancelledError:
                task.cancel()

    # Run the app
    app.run()
