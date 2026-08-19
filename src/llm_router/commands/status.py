"""Status command — display routing status, savings, and subscription pressure.

Uses premium Tokyo Night styled UI components for high information density.
"""

from __future__ import annotations

from llm_router.ui.status_premium import PremiumStatusCommand


def cmd_status(args: list[str]) -> int:
    """Execute: llm_router status

    Display routing status, savings summary, subscription pressure, and top models.
    Uses premium TUI components for Tokyo Night styled output.
    """
    cmd = PremiumStatusCommand()
    cmd.print_status()
    return 0
