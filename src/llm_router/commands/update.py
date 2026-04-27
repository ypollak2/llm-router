"""Update command — update hooks, rules, and version."""

from __future__ import annotations

import importlib.metadata
import json
import os
import sys
import urllib.request


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


def _ok(label: str) -> str:
    """Formatted success message."""
    return f"  {_green('✓')}  {label}"


def _warn(label: str) -> str:
    """Formatted warning message."""
    return f"  {_yellow('⚠')}  {label}"


# ── Update command ──────────────────────────────────────────────────────────

def _run_update() -> None:
    """Re-install hooks + rules, check for newer PyPI version."""
    from llm_router.install_hooks import install

    print(f"\n{_bold('llm-router update')}\n")

    # ── 1. Re-copy hooks & rules ──────────────────────────────────────
    print(_bold("  Hooks & rules"))
    actions = install(force=True)
    updated = [a for a in actions if "→" in a or "Updated" in a or "Registered" in a]
    if updated:
        for a in updated:
            print(_ok(f"  {a}"))
    else:
        print(_ok("  All hooks and rules are up to date"))

    # ── 2. Check PyPI for newer version ──────────────────────────────
    print(f"\n{_bold('  Version')}")
    try:
        current = importlib.metadata.version("claude-code-llm-router")
    except importlib.metadata.PackageNotFoundError:
        current = "unknown"

    try:
        with urllib.request.urlopen(
            "https://pypi.org/pypi/claude-code-llm-router/json", timeout=4
        ) as resp:
            data = json.loads(resp.read())
        latest = data["info"]["version"]
        if latest == current:
            print(_ok(f"  claude-code-llm-router {current} is up to date"))
        else:
            print(_warn(f"  {current} installed, {latest} available"))
            print(f"  {_yellow('→')} Run: {_bold('pip install --upgrade claude-code-llm-router')}")
    except Exception:
        print(_warn(f"  {current} installed (could not check PyPI)"))

    print()


# ── Entry point ─────────────────────────────────────────────────────────────

def cmd_update(args: list[str]) -> int:
    """Execute: llm-router update

    Re-install hooks and rules, check for newer version.
    """
    _run_update()
    return 0
