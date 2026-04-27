"""Routing command — display current routing configuration."""

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


def _yellow(s: str) -> str:
    """Yellow text."""
    return f"\033[33m{s}\033[0m" if _color_enabled() else s


def _red(s: str) -> str:
    """Red text."""
    return f"\033[31m{s}\033[0m" if _color_enabled() else s


def _dim(s: str) -> str:
    """Dim text."""
    return f"\033[2m{s}\033[0m" if _color_enabled() else s


# ── Routing display ─────────────────────────────────────────────────────────

def _run_routing() -> None:
    """Display current routing configuration and model chains."""
    from llm_router.profiles import (
        ROUTING_TABLE, _FREE_EXTERNAL_MODELS,
        reorder_for_pressure
    )
    from llm_router.types import RoutingProfile, TaskType
    from llm_router.claude_usage import get_claude_pressure
    from llm_router.codex_agent import is_codex_available
    from llm_router.gemini_cli_agent import is_gemini_cli_available
    from llm_router.config import get_config

    bold = "\033[1m" if _color_enabled() else ""
    reset = "\033[0m" if _color_enabled() else ""

    w = 80
    print(f"\n{bold}📊 LLM Router — Current Routing Configuration{reset}\n")
    print("─" * min(w, 70))

    # 1. Available providers
    print(f"\n{bold}Available Providers{reset}")
    providers = []
    try:
        if is_codex_available():
            providers.append(_green("✓") + " Codex (OpenAI subscription)")
    except Exception:
        pass
    try:
        if is_gemini_cli_available():
            providers.append(_green("✓") + " Gemini CLI (Google One AI Pro)")
    except Exception:
        pass

    # Check for other providers from config
    try:
        cfg = get_config()
        if cfg.openai_api_key:
            providers.append(_green("✓") + " OpenAI (gpt-4o, o3, gpt-4o-mini)")
        if cfg.gemini_api_key:
            providers.append(_green("✓") + " Gemini (Flash, Pro)")
        if cfg.ollama_base_url:
            providers.append(_green("✓") + " Ollama (local, free)")
    except Exception:
        pass

    if not providers:
        print("  (no providers configured)")
    for p in providers:
        print(f"  {p}")

    # 2. Claude quota pressure
    print(f"\n{bold}Claude Subscription Quota{reset}")
    try:
        pressure = get_claude_pressure()
        pct = int(pressure * 100)
        bar_width = 20
        filled = int(bar_width * pressure)
        bar = "█" * filled + "░" * (bar_width - filled)
        print(f"  Pressure: {pct}% [{bar}]")

        if pressure >= 0.99:
            status_text = _red("⚠️  HARD CAP") + " — Claude models disabled"
        elif pressure >= 0.85:
            status_text = _yellow("⚡ TIGHT") + " — Free models prioritized, Claude at end"
        else:
            status_text = _green("✓ Available") + " — Claude models lead chains"
        print(f"  {status_text}")
    except Exception:
        print("  (unavailable)")

    # 3. Sample routing chains
    print(f"\n{bold}Sample Routing Chains{reset}")
    print(_dim("Showing BALANCED profile for moderate tasks"))

    # Get pressure for reordering
    try:
        pressure = get_claude_pressure()
    except Exception:
        pressure = 0.0

    for task_name, task_type in [
        ("CODE", TaskType.CODE),
        ("QUERY", TaskType.QUERY),
        ("ANALYZE", TaskType.ANALYZE),
    ]:
        try:
            chain = ROUTING_TABLE.get((RoutingProfile.BALANCED, task_type), [])
            if not chain:
                continue

            # Apply pressure reordering
            reordered = reorder_for_pressure(chain, pressure, RoutingProfile.BALANCED)

            print(f"\n  {bold}{task_name}{reset}")

            # Show selected model (first in reordered chain)
            if reordered:
                selected = reordered[0]
                selected_indicator = ""
                if selected in _FREE_EXTERNAL_MODELS:
                    selected_indicator = " " + _green("✓ FREE")
                elif "ollama" in selected:
                    selected_indicator = " " + _green("✓ LOCAL")
                elif "claude" in selected:
                    selected_indicator = " " + _bold("✓ SUB")
                else:
                    selected_indicator = " ✓"
                print(f"    {_bold('→ Selected:')} {selected}{selected_indicator}")

            # Show fallback chain
            print(f"    {_dim('Fallback chain:')}")
            for i, model in enumerate(reordered[:5], 1):
                cost_indicator = ""
                if model in _FREE_EXTERNAL_MODELS:
                    cost_indicator = " " + _green("FREE")
                elif "ollama" in model:
                    cost_indicator = " " + _green("LOCAL")
                elif "claude" in model:
                    cost_indicator = " " + _bold("SUB")
                else:
                    cost_indicator = " 💰"

                print(f"    {i}. {model}{cost_indicator}")
        except Exception:
            pass

    print(f"\n{'─' * min(w, 70)}")
    print(_dim("Run: llm-router status     # Usage & savings"))
    print(_dim("Run: llm-router routing    # This command"))
    print(_dim("Run: llm-router budget     # Budget management\n"))


# ── Entry point ─────────────────────────────────────────────────────────────

def cmd_routing(args: list[str]) -> int:
    """Execute: llm-router routing

    Display current routing configuration and model chains.
    """
    _run_routing()
    return 0
