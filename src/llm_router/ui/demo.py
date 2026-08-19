"""Demo and test of all premium UI components.

Run with: python -m llm_router.ui.demo
"""

from __future__ import annotations

from datetime import datetime, timezone

from rich.console import Console

from llm_router.ui.status_spinner import RoutingStatusSpinner, show_routing_progress
from llm_router.ui.session_summary import SessionSummaryDashboard
from llm_router.ui.status_premium import PremiumStatusCommand


def demo_routing_spinner():
    """Demo live routing feedback spinner."""
    console = Console()

    console.print("\n[bold cyan]═══ DEMO 1: Live Routing Feedback ═══[/]\n")

    # Scenario 1: Simple one-liner
    console.print("[dim]Scenario 1: Quick routing decision[/]")
    show_routing_progress(
        stage="Classifying",
        model="",
        decision_reason="",
        console=console,
    )
    show_routing_progress(
        stage="Routing to",
        model="claude-opus",
        decision_reason="heuristic",
        console=console,
    )
    console.print()

    # Scenario 2: Animated spinner
    console.print("[dim]Scenario 2: Animated feedback (5 second demo)[/]")
    spinner = RoutingStatusSpinner(console)
    spinner.start("Classifying prompt")

    import time

    time.sleep(1)
    spinner.update("Checking context", progress=33)
    time.sleep(1)
    spinner.update("Routing to model", progress=66)
    time.sleep(1)
    spinner.complete(model="gpt-4o", decision_reason="via heuristic")

    console.print()


def demo_session_summary():
    """Demo session summary dashboard."""
    console = Console()

    console.print("\n[bold cyan]═══ DEMO 2: Session Summary Dashboard ═══[/]\n")

    dashboard = SessionSummaryDashboard(console)

    # Sample data
    decisions = [
        {"method": "Heuristic", "count": 35, "pct": 42},
        {"method": "Context-Inherit", "count": 16, "pct": 19},
        {"method": "Fallback", "count": 8, "pct": 10},
        {"method": "Build-Fast", "count": 7, "pct": 8},
        {"method": "Content-Gen", "count": 2, "pct": 2},
    ]

    savings = {
        "today": 10.30,
        "week": 16.62,
        "month": 16.62,
        "lifetime": 16.62,
        "free_calls": 76,
        "free_saved": 0.6269,
    }

    daily_calls = [120, 145, 180, 165, 140, 200, 190, 175, 210, 195, 160, 185, 220, 240]
    daily_tokens = [400, 480, 600, 550, 470, 670, 640, 580, 700, 650, 530, 620, 730, 800]

    models = [
        {"name": "gpt-4o", "count": 577, "cost": 5.77, "pct": 65},
        {"name": "claude-opus", "count": 180, "cost": 1.80, "pct": 20},
        {"name": "gemini-2.5-flash", "count": 23, "cost": 0.02, "pct": 0.2},
        {"name": "gpt-5.4", "count": 8, "cost": 0.0, "pct": 0},
    ]

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    dashboard.print_dashboard(
        timestamp=f"Session · {timestamp}",
        decisions=decisions,
        savings=savings,
        daily_calls=daily_calls,
        daily_tokens=daily_tokens,
        models=models,
    )

    console.print()


def demo_status_command():
    """Demo llm_router status command."""
    console = Console()

    console.print("\n[bold cyan]═══ DEMO 3: LLM Router Status Command ═══[/]\n")

    status = PremiumStatusCommand(console)
    status.print_status()

    console.print()


def main():
    """Run all demos."""
    console = Console()

    console.print("\n")
    console.print(
        "[bold yellow]╔════════════════════════════════════════════════════════════════╗[/]"
    )
    console.print(
        "[bold yellow]║  LLM_ROUTER Premium TUI Component Demo                              ║[/]"
    )
    console.print(
        "[bold yellow]║  Tokyo Night Dark Palette · High-Contrast Metrics              ║[/]"
    )
    console.print(
        "[bold yellow]╚════════════════════════════════════════════════════════════════╝[/]"
    )

    demo_routing_spinner()
    demo_session_summary()
    demo_status_command()

    console.print("[dim]Demo complete! These components are ready for production.[/]\n")


if __name__ == "__main__":
    main()
