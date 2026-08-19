"""Tokyo Night dark color palette for llm_router TUI."""

from dataclasses import dataclass

@dataclass(frozen=True)
class TokyoNightPalette:
    """Premium Tokyo Night color palette with True Color support."""

    # Accent & Primary
    accent = "#7aa2f7"           # Cyan-Blue — routing decisions, key metrics
    success = "#9ece6a"          # Vivid Green — savings, success states
    warning = "#e0af68"          # Amber/Gold — alerts, warnings
    error = "#f7768e"            # Neon Pink — failures, issues
    violet = "#bb9af7"           # Soft Purple — models section, LLM names

    # Structural & Text
    muted_border = "#3b4261"     # Deep Slate — frames, dividers, low contrast
    text_primary = "#c0caf5"     # Off-White — main readable text
    text_dim = "#565f89"         # Deep Gray — labels, secondary info

    # Backgrounds
    bg_main = "#1a1b26"          # Near Black — main background
    surface = "#192734"          # Dark Navy — panels, containers

    # Semantic
    info = accent
    positive = success
    negative = error
    caution = warning


PALETTE = TokyoNightPalette()


def styled_text(text: str, style: str = "primary") -> str:
    """Return text with ANSI color codes for the given style.

    Args:
        text: Text to style
        style: One of "primary", "dim", "accent", "success", "warning", "error"

    Returns:
        ANSI-escaped text with color
    """

    colors = {
        "primary": PALETTE.text_primary,
        "dim": PALETTE.text_dim,
        "accent": PALETTE.accent,
        "success": PALETTE.success,
        "warning": PALETTE.warning,
        "error": PALETTE.error,
    }

    color = colors.get(style, PALETTE.text_primary)
    return f"[{color}]{text}[/]"


def progress_bar(value: float, max_val: float = 100.0, width: int = 20) -> str:
    """Render a colored progress bar.

    Args:
        value: Current value
        max_val: Maximum value
        width: Bar width in characters

    Returns:
        Rendered bar with color
    """
    filled = max(0, min(width, round((value / max_val) * width)))
    empty = width - filled

    filled_bar = "█" * filled
    empty_bar = "░" * empty

    return f"[{PALETTE.accent}]{filled_bar}[/][{PALETTE.muted_border}]{empty_bar}[/]"


def divider(width: int = 70) -> str:
    """Render a muted divider line."""
    return f"[{PALETTE.muted_border}]{'━' * width}[/]"


def header(title: str, width: int = 70) -> str:
    """Render a styled header."""

    return f"[{PALETTE.accent}]█ {title}[/]"
