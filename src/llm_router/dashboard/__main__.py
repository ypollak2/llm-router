"""Entry point for: python -m llm_router.dashboard [--port N]"""

from __future__ import annotations

import asyncio
import sys


def main() -> None:
    port = 7337
    args = sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == "--port" and i + 1 < len(args):
            port = int(args[i + 1])

    from llm_router.dashboard.server import run
    asyncio.run(run(port=port))


if __name__ == "__main__":
    main()
