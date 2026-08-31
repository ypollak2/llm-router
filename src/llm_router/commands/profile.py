"""Profile command — show or auto-generate routing profile."""

from __future__ import annotations

import os
import sys


# ── ANSI helpers (respect NO_COLOR / non-tty) ─────────────────────────────────

def _color_enabled() -> bool:
    return sys.stdout.isatty() and not os.getenv("NO_COLOR")


def _bold(s: str) -> str:
    return f"\033[1m{s}\033[0m" if _color_enabled() else s


def _dim(s: str) -> str:
    return f"\033[2m{s}\033[0m" if _color_enabled() else s


# ── Command entry point ────────────────────────────────────────────────────────

def cmd_profile(args: list[str]) -> int:
    """Entry point for profile command."""
    subcmd = args[0] if args else "show"
    _run_profile(subcmd)
    return 0


# ── Implementation ─────────────────────────────────────────────────────────────

def _run_profile(subcmd: str = "show") -> None:
    """Show or auto-generate token-wise routing profile."""
    from llm_router.auto_profile import (
        auto_generate_profile, detect_services, display_detected_services, PROFILE_PATH
    )

    if subcmd == "auto":
        # Auto-detect and generate profile
        print(f"\n{_bold('🔍 Auto-Detecting Services')}\n")
        detected = detect_services()
        print(display_detected_services(detected))

        print(f"{_bold('💾 Generating Profile')}\n")
        profile_path = auto_generate_profile()
        print(f"✓ Profile saved to {profile_path}\n")
        print("Review and edit the profile to customize priorities:")
        print(f"  {_dim(f'nano {profile_path}')}\n")

    elif subcmd == "show":
        # Show current profile or auto-detect
        if PROFILE_PATH.exists():
            print(f"\n{_bold('📋 Current Profile')}\n")
            print(PROFILE_PATH.read_text())
        else:
            print(f"\n{_bold('No profile found.')}")
            print(f"Run: {_dim('llm-router profile auto')} to generate one.\n")

    else:
        print(f"Unknown profile command: {subcmd}")
        print("Try: llm-router profile {auto|show}")
