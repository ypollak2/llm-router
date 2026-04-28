"""Download statistics command for PyPI packages."""

import argparse
import sys

from llm_router.stats import print_stats


def cmd_stats(args: list[str] | None = None) -> int:
    """Display combined download statistics for llm-routing and claude-code-llm-router.

    Args:
        args: Command-line arguments (defaults to sys.argv[1:])

    Returns:
        0 on success, 1 on error
    """
    parser = argparse.ArgumentParser(
        description="Show combined download statistics for llm-routing packages"
    )
    parser.add_argument(
        "--period",
        choices=["recent", "last_month", "last_quarter", "all_time"],
        default="recent",
        help="Time period for statistics (default: recent = last 6 weeks)",
    )
    parser.add_argument(
        "--format",
        choices=["text", "markdown", "json"],
        default="text",
        help="Output format (default: text)",
    )

    parsed = parser.parse_args(args)

    try:
        print_stats(period=parsed.period, format_type=parsed.format)
        return 0
    except Exception as e:
        print(f"Error fetching statistics: {e}", file=sys.stderr)
        return 1
