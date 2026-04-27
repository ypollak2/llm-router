"""Uninstall command — remove hooks and MCP registration."""

from __future__ import annotations

import os
import sys
from pathlib import Path


# ── ANSI helpers (respect NO_COLOR / non-tty) ─────────────────────────────────

def _color_enabled() -> bool:
    return sys.stdout.isatty() and not os.getenv("NO_COLOR")


def _bold(s: str) -> str:
    return f"\033[1m{s}\033[0m" if _color_enabled() else s


def _green(s: str) -> str:
    return f"\033[32m{s}\033[0m" if _color_enabled() else s


def _red(s: str) -> str:
    return f"\033[31m{s}\033[0m" if _color_enabled() else s


def _yellow(s: str) -> str:
    return f"\033[33m{s}\033[0m" if _color_enabled() else s


def _dim(s: str) -> str:
    return f"\033[2m{s}\033[0m" if _color_enabled() else s


# ── Command entry point ────────────────────────────────────────────────────────

def cmd_uninstall(args: list[str]) -> int:
    """Entry point for uninstall command."""
    _run_uninstall(args)
    return 0


# ── Implementation ─────────────────────────────────────────────────────────────

def _run_uninstall(flags: list[str] | None = None) -> None:
    import shutil

    purge = "--purge" in (flags or [])
    from llm_router.install_hooks import uninstall

    print(f"\n{_bold('Uninstalling LLM Router...')}\n")
    actions = uninstall()
    for a in actions:
        print(f"  {a}")

    if purge:
        state_dir = Path.home() / ".llm-router"
        if state_dir.exists():
            # Warn and confirm before destroying usage history + .env
            print(f"\n  {_red(_bold('⚠  Purge will permanently delete:'))}")
            print(f"     {state_dir}/")
            for item in sorted(state_dir.iterdir()):
                print(f"       {item.name}")
            print()
            try:
                ans = input("  Type 'yes' to confirm permanent deletion: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = ""
            if ans == "yes":
                shutil.rmtree(state_dir)
                print(_green(f"\n  ✓ Deleted {state_dir}"))
            else:
                print(_yellow("\n  Purge cancelled — ~/.llm-router/ kept intact."))
        else:
            print(_dim(f"  {Path.home() / '.llm-router'} does not exist — nothing to purge."))

    print("\nDone. Restart Claude Code to apply changes.\n")
