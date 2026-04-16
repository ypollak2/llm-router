"""Terminal styling system for llm-router v6.0+

Provides ANSI color codes, Unicode symbols, and formatting functions
for rendering the Visibility + Memory design system across:
- Statusline HUD (live routing decisions)
- Session replay (structured transcript)
- Profile cards (personalization visualization)
- Savings cards (shareable proof)
- Quality alerts (safety assurance)

Design system: docs/DESIGN_SYSTEM_v6.md
Design tokens: .claude-plugin/design-tokens.json
"""

import os
from enum import Enum
from typing import Optional, Tuple
from dataclasses import dataclass


class Color(Enum):
    """ANSI color codes matching design tokens."""

    # Primary colors
    ORCHESTRATE_BLUE = "\033[34m"  # Routing flow, intelligence
    MEMORY_AMBER = "\033[33m"  # Learning, personalization
    CONFIDENCE_GREEN = "\033[32m"  # Success, savings, high confidence

    # Semantic colors
    WARNING_RED = "\033[31m"  # Low confidence, escalation, errors
    INFO_GRAY = "\033[37m"  # Secondary info, metadata
    SUBTLE = "\033[90m"  # Tertiary info, less important

    # Reset
    RESET = "\033[0m"

    @property
    def is_enabled(self) -> bool:
        """Check if ANSI colors are enabled (respects NO_COLOR env var)."""
        return os.environ.get("NO_COLOR") != "1"

    def __call__(self, text: str) -> str:
        """Apply color to text, respecting NO_COLOR env var."""
        if not self.is_enabled:
            return text
        return f"{self.value}{text}{Color.RESET.value}"


class Symbol(Enum):
    """Unicode symbols for routing language."""

    # Routing flow
    ARROW = "→"
    LIGHTNING = "⚡"
    ESCALATE_UP = "⬆"
    ESCALATE_DOWN = "⬇"

    # Memory & learning
    MEMORY = "💾"
    BRAIN = "🧠"
    LIBRARY = "📚"

    # Quality & confidence
    STAR_FULL = "★"
    STAR_EMPTY = "☆"
    CHECK = "✓"
    WARNING = "⚠"
    ALERT = "🚨"

    # Status & outcome
    SUCCESS = "✅"
    CLOCK = "⏱"
    MONEY = "💰"
    CHART = "📊"


class ConfidenceLevel(Enum):
    """Confidence levels with visual representation."""

    VERY_HIGH = (95, 100)  # ★★★★★★★★★★
    HIGH = (80, 94)  # ★★★★★★★★☆☆
    MEDIUM = (60, 79)  # ★★★★★☆☆☆☆☆
    LOW = (40, 59)  # ★★★☆☆☆☆☆☆☆
    VERY_LOW = (0, 39)  # ★☆☆☆☆☆☆☆☆☆

    def stars(self, confidence_pct: float) -> str:
        """Return star visualization for confidence percentage."""
        total_stars = 10
        filled = int((confidence_pct / 100) * total_stars)
        empty = total_stars - filled
        return Symbol.STAR_FULL.value * filled + Symbol.STAR_EMPTY.value * empty


@dataclass
class RoutingDecision:
    """Represents a routing decision with styling information."""

    model: str
    confidence: float  # 0.0-1.0
    task: str  # "code/simple", "analysis/moderate", etc.
    complexity: str  # "simple", "moderate", "complex"
    cost: float  # USD cost
    reason: Optional[str] = None
    escalated: bool = False

    def format_hud(self) -> str:
        """Format as live statusline HUD (~50 chars).

        Example: → haiku [87%] (code/simple) $0.001 ⚡
        """
        confidence_pct = int(self.confidence * 100)

        # Color-code the model name based on estimated cost tier
        if self.model in ("haiku", "gemini-flash", "gpt-4o-mini"):
            model_colored = Color.CONFIDENCE_GREEN(self.model)
        elif self.model in ("sonnet", "gpt-4o"):
            model_colored = Color.ORCHESTRATE_BLUE(self.model)
        else:
            model_colored = Color.MEMORY_AMBER(self.model)

        # Cost display
        cost_colored = Color.CONFIDENCE_GREEN(f"${self.cost:.4f}")

        return (
            f"{Symbol.ARROW.value} {model_colored} [{confidence_pct}%] "
            f"({self.task}) {cost_colored} {Symbol.LIGHTNING.value}"
        )

    def format_compact(self) -> str:
        """Format as compact decision line for replay (80 chars max)."""
        confidence_pct = int(self.confidence * 100)
        stars = ConfidenceLevel.MEDIUM.stars(self.confidence)

        return (
            f"      {Symbol.ARROW.value} Routed to: {Color.ORCHESTRATE_BLUE(self.model)}\n"
            f"      {Symbol.STAR_FULL.value} Confidence: {stars} {confidence_pct}%\n"
            f"      {Symbol.BRAIN.value} Reasoning: {self.reason or 'N/A'}\n"
            f"      {Symbol.MONEY.value} Cost: {Color.CONFIDENCE_GREEN(f'${self.cost:.4f}')}"
        )

    def format_quality_alert(self, quality_score: float) -> str:
        """Format as quality alert box."""
        if quality_score >= 0.9:
            status = f"{Symbol.SUCCESS.value} Excellent"
            color = Color.CONFIDENCE_GREEN
        elif quality_score >= 0.8:
            status = f"{Symbol.CHECK.value} Good"
            color = Color.CONFIDENCE_GREEN
        elif quality_score >= 0.6:
            status = f"{Symbol.WARNING.value} Marginal"
            color = Color.WARNING_RED
        else:
            status = f"{Symbol.ALERT.value} Poor"
            color = Color.WARNING_RED

        quality_pct = int(quality_score * 100)
        return color(f"{status} ({quality_pct}%)")


def format_confidence_bar(confidence: float, width: int = 20) -> str:
    """Create a bar chart for confidence percentage.

    Example: ████████░░░░░░░░░░ 87%
    """
    filled = int((confidence / 100) * width) if isinstance(confidence, (int, float)) else 0
    empty = width - filled
    bar = "█" * filled + "░" * empty
    return f"{Color.CONFIDENCE_GREEN(bar)} {int(confidence)}%" if confidence else f"{bar} {confidence}%"


def format_savings_bar(saved: float, baseline: float, width: int = 20) -> str:
    """Create a bar chart for savings percentage.

    Example: Saved $367.20 / $442.20 (83%)
    """
    if baseline == 0:
        return "N/A"

    percentage = (saved / baseline) * 100 if baseline > 0 else 0
    filled = int((percentage / 100) * width)
    empty = width - filled
    bar = "█" * filled + "░" * empty

    savings_pct = min(100, int(percentage))
    savings_colored = Color.CONFIDENCE_GREEN(f"${saved:.2f}")
    baseline_text = f"${baseline:.2f}"

    return f"{bar} {savings_colored} saved ({savings_pct}%)"


def format_box(
    title: str,
    lines: list[str],
    width: int = 50,
    color: Color = Color.ORCHESTRATE_BLUE,
) -> str:
    """Format content as a box drawing.

    Args:
        title: Box title (goes in top-left corner)
        lines: Content lines
        width: Total box width
        color: Color for box lines and title
    """
    # Top border
    box_lines = [color(f"╔{'═' * (width - 2)}╗")]

    # Title line
    if title:
        title_padded = f"  {title}".ljust(width - 2)
        box_lines.append(color("║") + title_padded + color("║"))
        box_lines.append(color(f"╠{'═' * (width - 2)}╣"))

    # Content lines
    for line in lines:
        # Truncate or pad to fit width
        if len(line) > width - 4:
            line = line[: width - 7] + "..."
        line_padded = line.ljust(width - 2)
        box_lines.append(color("║") + " " + line_padded + color("║"))

    # Bottom border
    box_lines.append(color(f"╚{'═' * (width - 2)}╝"))

    return "\n".join(box_lines)


def format_profile_header(
    title: str = "PERSONAL ROUTING PROFILE",
    width: int = 60,
) -> str:
    """Format profile section header with memory symbol.

    Example:
    ╔════════════════════════════════════════════════════════╗
    ║  💾 PERSONAL ROUTING PROFILE                           ║
    ╚════════════════════════════════════════════════════════╝
    """
    border_width = width - 4
    top = f"╔{'═' * border_width}╗"
    content = f"║  {Symbol.MEMORY.value} {title.ljust(border_width - 4)}║"
    bottom = f"╚{'═' * border_width}╝"

    return Color.MEMORY_AMBER(f"{top}\n{content}\n{bottom}")


def format_alert_box(
    alert_type: str,
    message: str,
    detail: Optional[str] = None,
    action: Optional[str] = None,
    width: int = 50,
) -> str:
    """Format quality or degradation alert box.

    Args:
        alert_type: "quality" or "degradation"
        message: Main message
        detail: Optional detailed explanation
        action: Optional action being taken
        width: Box width
    """
    if alert_type == "quality":
        icon = Symbol.ALERT.value
        color = Color.WARNING_RED
    else:
        icon = Symbol.WARNING.value
        color = Color.WARNING_RED

    lines = [f"{icon} {alert_type.upper()}"]
    if message:
        lines.append(message)
    if detail:
        lines.append(f"\n{detail}")
    if action:
        lines.append(f"\nAction: {Color.CONFIDENCE_GREEN(action)}")

    return format_box(
        title="",
        lines=lines,
        width=width,
        color=color,
    )


def format_savings_card(
    session_cost: float,
    baseline_cost: float,
    month_cost: float = None,
    month_savings: float = None,
    annual_projection: float = None,
    width: int = 50,
) -> str:
    """Format beautiful savings proof card for sharing.

    Example:
    ╔═════════════════════════════════════════════╗
    ║  💰 SESSION SUMMARY — May 15 (14:30–15:45) ║
    ├─────────────────────────────────────────────┤
    ║  Cost this session:       $0.18             ║
    ║  Opus baseline:           $2.47             ║
    ║  Saved:                 $2.29 (93%)         ║
    ╚═════════════════════════════════════════════╝
    """
    border_width = width - 2

    lines = [
        Color.ORCHESTRATE_BLUE(f"╔{'═' * border_width}╗"),
        (
            Color.ORCHESTRATE_BLUE("║") +
            f"  {Symbol.MONEY.value} SESSION SUMMARY".ljust(border_width) +
            Color.ORCHESTRATE_BLUE("║")
        ),
        Color.ORCHESTRATE_BLUE(f"╠{'═' * border_width}╣"),
    ]

    # Cost breakdown
    lines.append(
        Color.ORCHESTRATE_BLUE("║") +
        f"  Cost this session:       ${session_cost:.2f}".ljust(border_width) +
        Color.ORCHESTRATE_BLUE("║")
    )
    lines.append(
        Color.ORCHESTRATE_BLUE("║") +
        f"  Opus baseline:           ${baseline_cost:.2f}".ljust(border_width) +
        Color.ORCHESTRATE_BLUE("║")
    )

    # Savings highlight
    savings = baseline_cost - session_cost
    savings_pct = (savings / baseline_cost * 100) if baseline_cost > 0 else 0
    savings_line = f"  {Color.CONFIDENCE_GREEN(f'Saved: ${savings:.2f} ({int(savings_pct)}%)')}"
    lines.append(
        Color.ORCHESTRATE_BLUE("║") +
        savings_line.ljust(border_width) +
        Color.ORCHESTRATE_BLUE("║")
    )

    # Monthly summary (if provided)
    if month_cost and month_savings:
        lines.append(Color.ORCHESTRATE_BLUE(f"  {'-' * (border_width - 2)}"))
        lines.append(
            Color.ORCHESTRATE_BLUE("║") +
            f"  This month: ${month_cost:.2f} saved".ljust(border_width) +
            Color.ORCHESTRATE_BLUE("║")
        )

    # Annual projection (if provided)
    if annual_projection:
        lines.append(
            Color.ORCHESTRATE_BLUE("║") +
            f"  {Color.CONFIDENCE_GREEN(f'Yearly: ${annual_projection:.2f} saved')}".ljust(border_width) +
            Color.ORCHESTRATE_BLUE("║")
        )

    lines.append(Color.ORCHESTRATE_BLUE(f"╚{'═' * border_width}╝"))

    return "\n".join(lines)


def disable_colors() -> None:
    """Disable ANSI colors globally (for testing or NO_COLOR env var)."""
    os.environ["NO_COLOR"] = "1"


def enable_colors() -> None:
    """Enable ANSI colors globally."""
    os.environ.pop("NO_COLOR", None)


def colors_enabled() -> bool:
    """Check if ANSI colors are enabled."""
    return os.environ.get("NO_COLOR") != "1"


__all__ = [
    "Color",
    "Symbol",
    "ConfidenceLevel",
    "RoutingDecision",
    "format_confidence_bar",
    "format_savings_bar",
    "format_box",
    "format_profile_header",
    "format_alert_box",
    "format_savings_card",
    "disable_colors",
    "enable_colors",
    "colors_enabled",
]
