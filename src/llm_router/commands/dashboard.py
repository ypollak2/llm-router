"""Dashboard command — launch interactive TUI or web dashboard.

Hybrid approach:
- `llm_router dashboard` — Launch interactive TUI (default)
- `llm_router dashboard --web [--port PORT]` — Launch web dashboard at localhost:7337
"""

from __future__ import annotations

import asyncio
import sys


def cmd_dashboard(args: list[str]) -> int:
    """Execute: llm_router dashboard [--web] [--port PORT]

    By default, launches the interactive TUI dashboard for real-time monitoring.
    Use --web to launch the legacy web dashboard instead.

    Args:
        args: Command-line arguments
        --web: Launch web dashboard instead of TUI
        --port PORT: Web dashboard port (default: 7337, requires --web)
    """
    # Parse arguments
    launch_web = "--web" in args
    port = 7337

    for i, flag in enumerate(args):
        if flag == "--port" and i + 1 < len(args):
            try:
                port = int(args[i + 1])
            except ValueError:
                print(f"Invalid port: {args[i + 1]}")
                sys.exit(1)

    if launch_web:
        return _launch_web_dashboard(port)
    else:
        return _launch_tui_dashboard()


def _launch_tui_dashboard() -> int:
    """Launch the interactive TUI dashboard."""
    try:
        # Import inside function to defer dependency check
        try:
            from llm_router.tui import LLMRouterDashboard
        except ImportError:
            print("⚠️  Textual not installed. Install with: pipx inject llm-routing textual")
            return 1

        app = LLMRouterDashboard()
        app.run()
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as e:
        print(f"❌ Dashboard error: {e}")
        return 1


def _launch_web_dashboard(port: int) -> int:
    """Launch the web dashboard server."""
    try:
        from llm_router.dashboard.server import run
        asyncio.run(run(port=port))
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as e:
        print(f"❌ Web dashboard error: {e}")
        return 1
