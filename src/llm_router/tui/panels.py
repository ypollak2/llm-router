"""TUI Panels — Specialized widgets for dashboard display.

Defines reusable panel components:
  - TimelinePanel: Route progress with stage indicators
  - OutputPanel: Live streaming output with formatting
  - MetricsPanel: Real-time KPI metrics
"""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.reactive import reactive
from textual.widgets import Static, RichLog
from rich.console import RenderableType
from rich.panel import Panel
from rich.text import Text
from rich.table import Table


class TimelinePanel(Static):
    """Display route progress timeline with stage indicators.

    Shows ordered sequence of routing stages:
      ✓ Completed (success)
      ✗ Failed (with reason)
      ⏳ Pending (in progress)
      ✨ Committed (output started - commit barrier)
    """

    stages: reactive[list[dict[str, Any]]] = reactive(
        [], recompose=True
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize timeline panel."""
        super().__init__(*args, **kwargs)
        self.stages = []

    def add_stage(
        self,
        name: str,
        status: str = "pending",
        details: str = "",
        duration_ms: float = 0.0,
    ) -> None:
        """Add a new stage to the timeline.

        Args:
            name: Stage name (e.g., "Classification", "Attempt 1")
            status: Stage status - "pending", "success", or "failed"
            details: Additional details (e.g., model name, error reason)
            duration_ms: How long the stage took
        """
        icon = self._status_to_icon(status)
        stage = {
            "name": name,
            "status": status,
            "details": details,
            "duration_ms": duration_ms,
            "icon": icon,
        }
        self.stages = [*self.stages, stage]
        self.render()

    def _status_to_icon(self, status: str) -> str:
        """Convert status to display icon."""
        icons = {
            "success": "✓",
            "failed": "✗",
            "pending": "⏳",
        }
        return icons.get(status, "•")

    def render(self) -> Panel:
        """Render the timeline panel."""
        lines = []
        for stage in self.stages:
            icon = stage["icon"]
            name = stage["name"]
            status = stage["status"]
            details = stage["details"]

            # Color code by status
            if status == "success":
                color = "green"
            elif status == "failed":
                color = "red"
            else:
                color = "yellow"

            detail_text = f" — {details}" if details else ""
            line = f"  {icon} {name}{detail_text}"
            lines.append(Text(line, style=color))

        content = "\n".join(str(line) for line in lines) if lines else "Waiting..."
        return Panel(content, title="📋 Route Timeline", border_style="blue")


class OutputPanel(Static):
    """Display live streaming output with syntax highlighting.

    Features:
      - Real-time text streaming
      - Syntax highlighting for code blocks
      - Thinking block extraction and collapsible display
      - Scrollable with line wrapping
    """

    accumulated_text: str = ""
    in_code_block: bool = False
    code_language: str = ""
    code_buffer: str = ""
    thinking_blocks: list[str] = []

    def compose(self) -> ComposeResult:
        """Compose the output panel with a RichLog."""
        yield RichLog(wrap=True, markup=True, highlight=True, id="output-log")

    def append_text(self, text: str) -> None:
        """Append text to the output stream."""
        self.accumulated_text += text

        # Check for thinking blocks (Claude)
        if "<thinking>" in text:
            self.thinking_blocks.append("🧠 Thinking...")

        # Check for code blocks
        if "```" in text:
            self._handle_code_block()
        else:
            # Regular text output
            log: RichLog = self.query_one("#output-log", RichLog)
            log.write(Text(text, style="default"))

    def _handle_code_block(self) -> None:
        """Handle code block formatting."""
        log: RichLog = self.query_one("#output-log", RichLog)
        lines = self.accumulated_text.split("\n")
        for line in lines:
            if line.startswith("```"):
                self.in_code_block = not self.in_code_block
                if self.in_code_block:
                    parts = line.split("```")
                    self.code_language = parts[1].strip() if len(parts) > 1 else ""
            elif self.in_code_block:
                self.code_buffer += line + "\n"
            else:
                log.write(Text(line + "\n", style="default"))

    def render(self) -> RenderableType:
        """Render the panel content."""
        return Text(self.accumulated_text or "(Waiting for output...)", style="default")


class MetricsPanel(Static):
    """Display real-time routing metrics.

    Shows:
      - Current model
      - Token counts (input + output)
      - Cost accumulation
      - Throughput (tokens/sec)
      - Latency
      - Confidence (if classification available)
    """

    model: reactive[str] = reactive("N/A")
    input_tokens: reactive[int] = reactive(0)
    output_tokens: reactive[int] = reactive(0)
    cost_usd: reactive[float] = reactive(0.0)
    latency_ms: reactive[float] = reactive(0.0)
    throughput: reactive[float] = reactive(0.0)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize metrics panel."""
        super().__init__(*args, **kwargs)

    def update_metrics(
        self,
        model: str = "N/A",
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
        latency_ms: float = 0.0,
        throughput: float = 0.0,
    ) -> None:
        """Update metric values."""
        self.model = model
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cost_usd = cost_usd
        self.latency_ms = latency_ms
        self.throughput = throughput
        self.render()

    def render(self) -> Panel:
        """Render metrics panel."""
        total_tokens = self.input_tokens + self.output_tokens

        # Build metrics table
        table = Table(show_header=False, show_footer=False, padding=(0, 1))
        table.add_column("Metric", style="cyan")
        table.add_column("Value", style="green")

        table.add_row("🤖 Model", self.model)
        table.add_row("📊 Tokens", f"{total_tokens:,}")
        table.add_row("📥 Input", f"{self.input_tokens:,}")
        table.add_row("📤 Output", f"{self.output_tokens:,}")
        table.add_row("💰 Cost", f"${self.cost_usd:.4f}")
        table.add_row("⏱️  Latency", f"{self.latency_ms:.0f}ms")
        table.add_row("🚀 Throughput", f"{self.throughput:.1f} tok/s")

        return Panel(table, title="📈 Metrics", border_style="green", expand=True)


class SparklinePanel(Static):
    """Display 14-day cost trend with sparkline visualization.

    Shows daily cost history as a compact sparkline chart,
    enabling quick visualization of usage patterns and cost trends.
    """

    daily_costs: reactive[list[float]] = reactive([])
    total_savings: reactive[float] = reactive(0.0)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize sparkline panel."""
        super().__init__(*args, **kwargs)
        self.daily_costs = []
        self.total_savings = 0.0

    def update_sparkline(
        self,
        daily_costs: list[float],
        total_savings: float = 0.0,
    ) -> None:
        """Update sparkline data.

        Args:
            daily_costs: List of daily costs (last 14 days)
            total_savings: Total USD saved this period
        """
        self.daily_costs = daily_costs
        self.total_savings = total_savings
        self.render()

    def _generate_sparkline(self, values: list[float]) -> str:
        """Generate ASCII sparkline from values.

        Uses block characters to create a compact visualization.
        """
        if not values:
            return "No data"

        # Normalize values to 0-8 range (8 block heights)
        min_val = min(values) if values else 0
        max_val = max(values) if values else 1
        range_val = max_val - min_val

        if range_val == 0:
            blocks = ["▅"] * len(values)
        else:
            blocks = []
            for v in values:
                normalized = (v - min_val) / range_val
                block_index = min(7, int(normalized * 8))
                block_chars = ["▁", "▂", "▃", "▄", "▅", "▆", "▇", "█"]
                blocks.append(block_chars[block_index])

        return "".join(blocks)

    def render(self) -> Panel:
        """Render sparkline panel."""
        lines = []

        # Sparkline chart
        if self.daily_costs:
            sparkline = self._generate_sparkline(self.daily_costs)
            lines.append(f"14-day trend: {sparkline}")

            # Stats
            avg_cost = sum(self.daily_costs) / len(self.daily_costs)
            max_cost = max(self.daily_costs)
            min_cost = min(self.daily_costs)
            total_cost = sum(self.daily_costs)

            lines.append("")
            lines.append(f"  Total:  ${total_cost:.2f}")
            lines.append(f"  Avg:    ${avg_cost:.2f}/day")
            lines.append(f"  Peak:   ${max_cost:.2f}")
            lines.append(f"  Low:    ${min_cost:.2f}")

            if self.total_savings > 0:
                lines.append(f"  Saved:  ${self.total_savings:.2f}")
        else:
            lines.append("No historical data yet")

        content = "\n".join(lines)
        return Panel(content, title="📊 14-Day Trend", border_style="blue", expand=True)


class ModelBreakdownPanel(Static):
    """Display model usage distribution.

    Shows percentage breakdown of API calls by model,
    helping identify which models are being used most.
    """

    model_stats: reactive[dict[str, float]] = reactive({})

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize model breakdown panel."""
        super().__init__(*args, **kwargs)
        self.model_stats = {}

    def update_breakdown(self, model_stats: dict[str, float]) -> None:
        """Update model usage breakdown.

        Args:
            model_stats: Dict mapping model names to usage percentages
        """
        self.model_stats = model_stats
        self.render()

    def render(self) -> Panel:
        """Render model breakdown panel."""
        if not self.model_stats:
            content = "No model data yet"
        else:
            # Sort by percentage descending
            sorted_models = sorted(
                self.model_stats.items(),
                key=lambda x: x[1],
                reverse=True
            )

            lines = []
            for model, pct in sorted_models[:10]:  # Show top 10
                # Create bar
                bar_length = int(pct / 2)  # Max 50 chars per bar
                bar = "█" * bar_length
                model_short = model.split("/")[-1][:20]  # Truncate long names
                lines.append(f"  {model_short:20} {bar} {pct:5.1f}%")

            content = "\n".join(lines)

        return Panel(content, title="🤖 Model Distribution", border_style="cyan", expand=True)


class QuotaPanel(Static):
    """Display subscription quota usage and limits.

    Shows remaining quotas for Claude Pro Max subscriptions
    and estimates based on current usage patterns.
    """

    claude_quota_pct: reactive[float] = reactive(0.0)
    gemini_quota_pct: reactive[float] = reactive(0.0)
    claude_remaining: reactive[str] = reactive("Unknown")
    gemini_remaining: reactive[str] = reactive("Unknown")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize quota panel."""
        super().__init__(*args, **kwargs)

    def update_quotas(
        self,
        claude_quota_pct: float = 0.0,
        gemini_quota_pct: float = 0.0,
        claude_remaining: str = "Unknown",
        gemini_remaining: str = "Unknown",
    ) -> None:
        """Update quota information.

        Args:
            claude_quota_pct: Claude Pro Max quota used (0-100)
            gemini_quota_pct: Gemini API quota used (0-100)
            claude_remaining: Time remaining for Claude quota
            gemini_remaining: Time remaining for Gemini quota
        """
        self.claude_quota_pct = claude_quota_pct
        self.gemini_quota_pct = gemini_quota_pct
        self.claude_remaining = claude_remaining
        self.gemini_remaining = gemini_remaining
        self.render()

    def _quota_bar(self, pct: float) -> str:
        """Generate quota usage bar."""
        used = int(pct / 5)  # 20 chars max
        remaining = 20 - used
        bar = "█" * used + "░" * remaining

        # Color based on usage level
        if pct >= 80:
            color = "[red]"
        elif pct >= 50:
            color = "[yellow]"
        else:
            color = "[green]"

        return f"{color}{bar}[/] {pct:5.1f}%"

    def render(self) -> Panel:
        """Render quota panel."""
        lines = []

        # Claude quota
        lines.append("Claude Pro Max:")
        lines.append(f"  {self._quota_bar(self.claude_quota_pct)}")
        lines.append(f"  Remaining: {self.claude_remaining}")

        lines.append("")

        # Gemini quota
        lines.append("Gemini API:")
        lines.append(f"  {self._quota_bar(self.gemini_quota_pct)}")
        lines.append(f"  Remaining: {self.gemini_remaining}")

        content = "\n".join(lines)
        return Panel(content, title="📦 Quotas", border_style="magenta", expand=True)
