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
    from llm_router.install_hooks import (
        uninstall,
        uninstall_claw_code,
        uninstall_ide_configs,
    )

    print(f"\n{_bold('Uninstalling LLM Router...')}\n")
    actions = uninstall()
    # RED2-6-02: uninstall must clean up everything install could have created,
    # not only the primary Claude Code surfaces. install auto-detects claw-code
    # and IDE integrations; uninstall previously never called their removers, so a
    # full parallel claw-code install (hooks + sidecars + a live MCP registration +
    # the LLM_ROUTER_CLAW_CODE flag) and project IDE configs survived the documented
    # `llm_router uninstall`. Both removers are no-ops when nothing was installed.
    try:
        actions.extend(uninstall_claw_code())
    except Exception as e:  # never let cleanup of an optional surface abort uninstall
        actions.append(f"claw-code cleanup skipped: {e}")
    try:
        actions.extend(uninstall_ide_configs())
    except Exception as e:
        actions.append(f"IDE-config cleanup skipped: {e}")
    # RED2-9-*: replay the install manifest — every artifact a write recorded is
    # reversed here (JSON MCP entries, TOML tables, appended blocks, created
    # files, copied hook scripts, dirs). This is the authoritative, coverage-safe
    # path for anything installed since the manifest landed; new host surfaces are
    # cleaned automatically as long as their write records.
    try:
        from llm_router import install_manifest
        actions.extend(install_manifest.apply_uninstall())
    except Exception as e:
        actions.append(f"manifest cleanup skipped: {e}")
    # RED2-8-01: enumerated host cleanup — a legacy fallback for installs that
    # predate the manifest (no records to replay). Idempotent with the manifest
    # replay above (both no-op once an entry is gone).
    try:
        from llm_router.commands.install import uninstall_host_integrations
        actions.extend(uninstall_host_integrations())
    except Exception as e:
        actions.append(f"host-integration cleanup skipped: {e}")
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
