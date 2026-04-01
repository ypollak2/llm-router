"""CLI entry point for llm-router.

Usage:
    llm-router                  — start the MCP server (stdio transport)
    llm-router install          — install hooks, rules, and MCP server config globally
    llm-router install --check  — show what would be installed without doing it
    llm-router install --force  — reinstall even if already present
    llm-router uninstall        — remove hooks and MCP registration
"""

from __future__ import annotations

import sys


def main() -> None:
    """Unified CLI: dispatches to MCP server or install subcommands."""
    args = sys.argv[1:]

    if args and args[0] == "install":
        _run_install(flags=args[1:])
    elif args and args[0] == "uninstall":
        _run_uninstall()
    else:
        # Default: start the MCP server (original behavior)
        from llm_router.server import main as _mcp_main
        _mcp_main()


def _run_install(flags: list[str]) -> None:
    check_only = "--check" in flags
    force = "--force" in flags

    from llm_router.install_hooks import (
        _HOOKS_DST, _HOOKS_SRC, _HOOK_DEFS,
        _RULES_DST, _RULES_SRC,
        install,
    )

    if check_only:
        print("\n[llm-router] Install preview (--check, no changes made)\n")
        for src_name, dst_name, event, _ in _HOOK_DEFS:
            src = _HOOKS_SRC / src_name
            dst = _HOOKS_DST / dst_name
            exists = "✓ exists" if dst.exists() else "⬜ missing"
            src_ok = "✓" if src.exists() else "✗ SOURCE MISSING"
            print(f"  {src_ok}  {src_name} → {dst}  [{exists}]")
        rules_src = _RULES_SRC / "llm-router.md"
        rules_dst = _RULES_DST / "llm-router.md"
        r_exists = "✓ exists" if rules_dst.exists() else "⬜ missing"
        print(f"  {'✓' if rules_src.exists() else '✗'}  llm-router.md → {rules_dst}  [{r_exists}]")
        print("\nRun `llm-router install` to apply.\n")
        return

    if force:
        # With --force, unregister MCP first so it gets re-registered with current path
        from llm_router.install_hooks import _load_settings, _save_settings
        settings = _load_settings()
        settings.get("mcpServers", {}).pop("llm-router", None)
        _save_settings(settings)

    print("\n╔══════════════════════════════════════════╗")
    print("║   LLM Router — One-Command Install        ║")
    print("╚══════════════════════════════════════════╝\n")

    actions = install()
    for a in actions:
        print(f"  {a}")

    print("\n✓ LLM Router installed globally.")
    print("  Every Claude Code session will now auto-route tasks.")
    print("  Restart Claude Code to activate.\n")
    print("  Subcommands:")
    print("    llm-router install --check   — preview only")
    print("    llm-router install --force   — reinstall / update paths")
    print("    llm-router uninstall         — remove\n")


def _run_uninstall() -> None:
    from llm_router.install_hooks import uninstall

    print("\nUninstalling LLM Router...\n")
    actions = uninstall()
    for a in actions:
        print(f"  {a}")
    print("\nDone. Restart Claude Code to apply changes.\n")


if __name__ == "__main__":
    main()
