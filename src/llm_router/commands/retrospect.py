#!/usr/bin/env python3
"""llm-router retrospect — IAF-style session debrief with routing directives.

Command: uv run llm-router retrospect [--weekly] [--compact] [--no-directives]

Performs 5-step debrief analysis:
1. FACTS — What happened (neutral, data-driven)
2. EXPECTATIONS vs REALITY — Where routing fell short
3. ROOT CAUSE — Why (classifier wrong, profile stale, etc.)
4. ACTIONS — Concrete directives to improve next session
5. MEMORY — Binding directives written to files

Example output:
  ══════════════════════════════════════════════════════════════
  SESSION RETROSPECTIVE — 47 calls
  ══════════════════════════════════════════════════════════════

  【FACTS】
    Calls: 47  |  Cost: $2.34  |  Saved: $89.50
    Accuracy: 91%  |  Corrections: 2  |  Duration: 75min

  【GAPS】
    ⚠ security_review: confidence 23% — user escalated to opus [2x]
    ⚠ jwt_auth_review: quality score 0.74

  【ROOT CAUSES】
    • CLASSIFIER_ERROR: 'security' keyword not detected
    • PROFILE_STALE: 2/3 overrides for security tasks

  【ACTIONS】
    → security_review: 1 more override locks in opus routing
    → Add 'security','jwt','oauth' to high-risk keywords
  ──────────────────────────────────────────────────────────────
"""

import argparse
import asyncio
from pathlib import Path
from typing import Optional

from llm_router.retrospective import (
    run_session_retrospective,
    run_weekly_retrospective,
    format_full_report,
    format_compact_summary,
)


def main(args: Optional[list[str]] = None) -> int:
    """Main entry point for retrospect command.

    Args:
        args: Command line arguments (for testing)

    Returns:
        Exit code (0 on success)
    """
    parser = argparse.ArgumentParser(
        description="IAF-style session retrospective with routing directives"
    )
    parser.add_argument(
        "--weekly",
        action="store_true",
        help="Aggregate last 7 days into weekly report",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Print compact 2-line summary (for session-end hook)",
    )
    parser.add_argument(
        "--no-directives",
        action="store_true",
        help="Don't write directives to disk",
    )

    parsed = parser.parse_args(args or [])

    # Run retrospective asynchronously
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        if parsed.weekly:
            retro = loop.run_until_complete(run_weekly_retrospective())
        else:
            write_files = not parsed.no_directives
            retro = loop.run_until_complete(
                run_session_retrospective(write_files=write_files)
            )
    finally:
        loop.close()

    if not retro:
        return 1

    # Format and print
    if parsed.compact:
        output = format_compact_summary(retro)
    else:
        output = format_full_report(retro)

    if output:
        print()
        print(output)
        print()

    return 0


if __name__ == "__main__":
    exit(main())
