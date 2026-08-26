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

def _remove_derived_state() -> list[str]:
    """Delete regenerable caches from ~/.llm-router/. Returns action strings.

    GH#45: `agentic_models.json` is written by the background model-capability
    probe that install kicks off. Nothing removed it, so a plain uninstall left
    it behind; only `--purge` cleared it, and that deletes the whole state dir.

    The state directory deliberately survives a plain uninstall because it holds
    `usage.db` (cost history) and `.env` (API keys) — things a user would not
    want silently destroyed, and which no reinstall could reconstruct. That
    reasoning does not extend to a derived cache: the next probe regenerates it,
    it holds nothing the user authored, and it is stale the moment the model set
    changes. Preserving it protects nothing and leaves uninstall not quite a
    no-op on the filesystem.

    So the line this draws is user data vs regenerable cache, INSIDE a state
    directory that still survives — not "keep everything" vs "delete everything".

    The issue suggested routing this through the install manifest, the way the
    ~/.claude.json husk fix went in 13.0.3. Deleting here instead, deliberately:
    the probe runs in the BACKGROUND and may write the cache long after install
    has finished and `apply_uninstall()` has already called `clear()`. A manifest
    record would then exist only when the probe happened to finish in time,
    which is a race, and a cleanup that works most of the time is the shape of
    bug this file keeps finding. The path is a fixed, llm_router-owned name in an
    llm_router-owned directory, so removing it directly needs no bookkeeping.
    """
    from llm_router import agentic_registry

    actions: list[str] = []
    # Each entry is read through its owning module's attribute rather than
    # rebuilt from Path.home(), so a redirected path stays redirected.
    targets = [agentic_registry.CACHE_PATH]

    for target in targets:
        if not target.exists():
            continue
        try:
            target.unlink()
            actions.append(f"Removed regenerable cache {target}")
        except OSError as e:
            # GH#42's lesson: a cleanup failure must be reported, never raised
            # out of uninstall and never swallowed.
            actions.append(f"WARN could not remove {target}: {e}")
    return actions


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
    # GH#45: regenerable caches in ~/.llm-router/ — the state dir itself and the
    # user data in it are kept; only derived files go.
    try:
        actions.extend(_remove_derived_state())
    except Exception as e:
        actions.append(f"derived-state cleanup skipped: {e}")
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
