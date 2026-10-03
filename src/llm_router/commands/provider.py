"""``llm-router provider`` -- see and lift provider benches.

A provider that reports "usage limit, try again at 06:39" is skipped until then
(``llm_router.provider_reset``). If that report was wrong, or the limit was lifted
early (an upgraded plan, a new key), ``unban`` clears it without waiting.
"""

from __future__ import annotations

import sys
import time


def _print_help() -> None:
    print("usage: llm-router provider list")
    print("       llm-router provider unban <name>   (or: unban --all)")


def _fmt(epoch: float) -> str:
    left = max(0, int(epoch - time.time()))
    return (
        f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(epoch))}"
        f" (in {left // 3600}h {left % 3600 // 60}m)"
    )


def cmd_provider(args: list[str]) -> None:
    from llm_router import provider_reset

    sub = args[0] if args else "list"
    if sub in ("-h", "--help", "help"):
        _print_help()
        return
    if sub == "list":
        resets = provider_reset.all_provider_resets()
        if not resets:
            print("No provider is benched until a reset time.")
            return
        for name, until in sorted(resets.items()):
            print(f"{name}: unavailable until {_fmt(until)}")
        return
    if sub == "unban":
        target = args[1] if len(args) > 1 else ""
        if not target:
            _print_help()
            sys.exit(2)
        cleared = provider_reset.clear_provider_reset(None if target == "--all" else target)
        if cleared:
            print("Cleared: " + ", ".join(cleared))
        else:
            print(f"Nothing to clear for '{target}'.")
        return
    print(f"llm-router provider: unknown subcommand '{sub}'", file=sys.stderr)
    _print_help()
    sys.exit(2)
