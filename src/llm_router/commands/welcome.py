"""`llm_router welcome` — print the painterly LLM Router banner on demand.

The full ANSI banner used to render automatically at SessionStart, but
Claude Code collapses everything after the first stderr line so the art
was invisible. This subcommand surfaces it directly on stdout, so the
user can view it whenever they want.

Flags:
    --compact   Print the one-line statusline variant instead.
"""

from __future__ import annotations

import sys

from llm_router.banner import render_banner, render_compact_banner


def cmd_welcome(args: list[str]) -> int:
    if "-h" in args or "--help" in args:
        print(__doc__)
        return 0

    if "--compact" in args:
        print(render_compact_banner())
        return 0

    print(render_banner())
    return 0


if __name__ == "__main__":
    sys.exit(cmd_welcome(sys.argv[1:]))
