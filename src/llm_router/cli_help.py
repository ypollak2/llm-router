"""A shared `--help` guard for the standalone console scripts.

GH#51/#52. Only `llm-router` and `llm-router-install-hooks` handled `--help`.
`llm-router-onboard` and `llm-router-quickstart` ignored it, ran their
interactive flows and died on `EOFError` the moment stdin was not a TTY;
`llm-router-isolation-test` ignored it and ran a full health check;
`llm-router-sse` tried `int("--help")` as a port — after having already made a
live model call, which on a paid provider key is a billed request from the
safest flag on the CLI.

Two properties matter, and the second is the one that bites:

1. `--help` prints usage and exits 0, on a closed stdin.
2. `--help` is INERT. It must run before anything that opens a socket, writes
   a file or prompts. So the guard is called at the TOP of each entry point,
   before any other import or setup work.
"""
from __future__ import annotations

import sys

_HELP_FLAGS = ("-h", "--help", "help")


def handle_help(prog: str, summary: str, *, usage: str = "", notes: str = "") -> None:
    """Print usage and exit 0 if the invocation asks for help. Otherwise return.

    Args:
        prog: The console-script name as a user types it.
        summary: One line describing what the command does.
        usage: Optional usage line; defaults to ``prog`` taking no arguments.
        notes: Optional extra paragraph (interactivity requirements, etc).
    """
    argv = sys.argv[1:]
    if not any(a in _HELP_FLAGS for a in argv):
        return

    print(f"usage: {usage or prog}")
    print()
    print(f"  {summary}")
    if notes:
        print()
        for line in notes.strip().splitlines():
            print(f"  {line.strip()}")
    print()
    print("options:")
    print("  -h, --help    show this message and exit")
    sys.exit(0)
