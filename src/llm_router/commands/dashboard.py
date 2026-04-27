"""Dashboard command — start the web dashboard at localhost:7337."""

from __future__ import annotations

import asyncio
import sys


def cmd_dashboard(args: list[str]) -> int:
    """Execute: llm-router dashboard [--port PORT]
    
    Start the web dashboard server. Defaults to localhost:7337.
    """
    port = 7337
    for i, flag in enumerate(args):
        if flag == "--port" and i + 1 < len(args):
            try:
                port = int(args[i + 1])
            except ValueError:
                print(f"Invalid port: {args[i + 1]}")
                sys.exit(1)

    from llm_router.dashboard.server import run

    asyncio.run(run(port=port))
    return 0
