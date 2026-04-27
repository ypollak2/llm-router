"""Budget command — manage provider spending caps."""

from __future__ import annotations

import os
import sys


# ── Formatting utilities ────────────────────────────────────────────────────

def _color_enabled() -> bool:
    """Check if color output is enabled."""
    return sys.stdout.isatty() and not os.getenv("NO_COLOR")


def _bold(s: str) -> str:
    """Bold text."""
    return f"\033[1m{s}\033[0m" if _color_enabled() else s


def _green(s: str) -> str:
    """Green text."""
    return f"\033[32m{s}\033[0m" if _color_enabled() else s


def _red(s: str) -> str:
    """Red text."""
    return f"\033[31m{s}\033[0m" if _color_enabled() else s


def _yellow(s: str) -> str:
    """Yellow text."""
    return f"\033[33m{s}\033[0m" if _color_enabled() else s


def _dim(s: str) -> str:
    """Dim text."""
    return f"\033[2m{s}\033[0m" if _color_enabled() else s


# ── Budget management ───────────────────────────────────────────────────────

def _run_budget(subcmd: str, flags: list[str]) -> None:
    """llm-router budget [list|set|remove] ...

    list                       — show all providers with spend, cap, pressure
    set <provider> <amount>    — set monthly cap (USD) for a provider
    remove <provider>          — clear the cap for a provider
    """
    import asyncio
    from llm_router.budget_store import list_caps, remove_cap, set_cap
    from llm_router.budget import get_all_budget_states, invalidate_cache
    from llm_router.types import LOCAL_PROVIDERS

    if subcmd == "set":
        if len(flags) < 2:
            print(_red("Usage: llm-router budget set <provider> <amount>"))
            sys.exit(1)
        provider, amount_str = flags[0], flags[1]
        try:
            amount = float(amount_str)
        except ValueError:
            print(_red(f"Invalid amount: {amount_str!r} — must be a number"))
            sys.exit(1)
        try:
            set_cap(provider, amount)
        except ValueError as e:
            print(_red(str(e)))
            sys.exit(1)
        invalidate_cache(provider)
        print(f"  {_green('✓')} Budget cap set: {_bold(provider)} → ${amount:.2f}/month")
        print(f"  {_dim('Saved to ~/.llm-router/budgets.json')}")
        return

    if subcmd == "remove":
        if not flags:
            print(_red("Usage: llm-router budget remove <provider>"))
            sys.exit(1)
        provider = flags[0]
        removed = remove_cap(provider)
        invalidate_cache(provider)
        if removed:
            print(f"  {_green('✓')} Removed cap for {_bold(provider)}")
        else:
            print(f"  {_yellow('!')} No cap was set for {_bold(provider)}")
        return

    # Default: list
    states = asyncio.run(get_all_budget_states())
    caps = list_caps()

    print(f"\n{_bold('[llm-router] Budget Caps')}\n")
    header = f"  {'Provider':<14}  {'Spend':>8}  {'Cap':>8}  {'Pressure':<14}  {'Bar'}"
    print(_dim(header))
    print(_dim("  " + "─" * 62))

    for provider, state in sorted(states.items()):
        bar_len = 10
        filled = round(state.pressure * bar_len)
        bar = "█" * filled + "░" * (bar_len - filled)
        pct = f"{state.pressure:.0%}"

        if provider in LOCAL_PROVIDERS:
            spend_str = "free"
            cap_str = "—"
            bar_colored = _dim(f"[{bar}]")
        else:
            spend_str = f"${state.spend_usd:.2f}"
            cap_str = f"${state.cap_usd:.2f}" if state.cap_usd > 0 else _dim("no cap")
            if state.pressure >= 0.8:
                bar_colored = _red(f"[{bar}]")
            elif state.pressure >= 0.5:
                bar_colored = _yellow(f"[{bar}]")
            else:
                bar_colored = _green(f"[{bar}]")

        print(f"  {provider:<14}  {spend_str:>8}  {cap_str:>8}  {pct:>4}  {bar_colored}")

    uncapped = [p for p, s in states.items() if p not in LOCAL_PROVIDERS and s.cap_usd <= 0]
    if uncapped:
        print()
        print(_dim(f"  💡 No cap set for: {', '.join(sorted(uncapped))}"))
        print(_dim("     Set one: llm-router budget set <provider> <amount>"))

    stored = caps
    if stored:
        print()
        print(_dim("  Caps stored in ~/.llm-router/budgets.json"))
    print()


# ── Entry point ─────────────────────────────────────────────────────────────

def cmd_budget(args: list[str]) -> int:
    """Execute: llm-router budget [list|set|remove]

    Manage provider spending caps.
    """
    subcmd = args[0] if args else "list"
    flags = args[1:] if args else []
    _run_budget(subcmd, flags)
    return 0
