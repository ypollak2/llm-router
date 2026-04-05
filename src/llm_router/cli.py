"""CLI entry point for llm-router.

Usage:
    llm-router                  — start the MCP server (stdio transport)
    llm-router install          — install hooks, rules, and MCP server config globally
    llm-router install --check  — show what would be installed without doing it
    llm-router install --force  — reinstall even if already present
    llm-router uninstall        — remove hooks and MCP registration
    llm-router status           — show routing status, today's savings, subscription pressure
    llm-router dashboard        — start the web dashboard at localhost:7337
    llm-router dashboard --port 7338  — use a custom port
"""

from __future__ import annotations

import sys


def main() -> None:
    """Unified CLI: dispatches to MCP server or subcommands."""
    args = sys.argv[1:]

    if args and args[0] == "install":
        _run_install(flags=args[1:])
    elif args and args[0] == "uninstall":
        _run_uninstall()
    elif args and args[0] == "status":
        _run_status()
    elif args and args[0] == "dashboard":
        _run_dashboard(flags=args[1:])
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
        check_api_keys, claude_desktop_config_path,
        install,
    )

    if check_only:
        print("\n[llm-router] Install preview (--check, no changes made)\n")

        print("  Hooks & rules:")
        for src_name, dst_name, event, _ in _HOOK_DEFS:
            src = _HOOKS_SRC / src_name
            dst = _HOOKS_DST / dst_name
            exists = "✓ exists" if dst.exists() else "⬜ missing"
            src_ok = "✓" if src.exists() else "✗ SOURCE MISSING"
            print(f"    {src_ok}  {src_name} → {dst}  [{exists}]")
        rules_src = _RULES_SRC / "llm-router.md"
        rules_dst = _RULES_DST / "llm-router.md"
        r_exists = "✓ exists" if rules_dst.exists() else "⬜ missing"
        print(f"    {'✓' if rules_src.exists() else '✗'}  llm-router.md → {rules_dst}  [{r_exists}]")

        print("\n  Claude Desktop:")
        desktop_path = claude_desktop_config_path()
        if desktop_path is None:
            print("    ⬜  unsupported platform")
        else:
            import json
            desktop_exists = "✓ exists" if desktop_path.exists() else "⬜ not found"
            registered = False
            if desktop_path.exists():
                try:
                    cfg = json.loads(desktop_path.read_text())
                    registered = "llm-router" in cfg.get("mcpServers", {})
                except Exception:
                    pass
            status = "✓ registered" if registered else "⬜ not registered"
            print(f"    {status}  {desktop_path}  [{desktop_exists}]")

        print("\n  Provider keys:")
        for line in check_api_keys():
            print(f"  {line}")

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
    print("  Restart Claude Code (and Claude Desktop if installed) to activate.\n")

    print("  Provider keys:")
    for line in check_api_keys():
        print(f" {line}")

    print("\n  Subcommands:")
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


def _run_status() -> None:
    import json
    import os
    import sqlite3
    import time
    from datetime import datetime, timezone, date

    state_dir  = os.path.expanduser("~/.llm-router")
    usage_json = os.path.join(state_dir, "usage.json")
    db_path    = os.path.join(state_dir, "usage.db")
    WIDTH      = 60

    print("\n" + "─" * WIDTH)
    print("  llm-router status")
    print("─" * WIDTH)

    # ── Subscription pressure ──
    pressure_data: dict = {}
    try:
        with open(usage_json) as f:
            pressure_data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    if pressure_data:
        age_s = time.time() - pressure_data.get("updated_at", 0)
        age_label = f"{int(age_s / 60)}m ago" if age_s < 3600 else "stale"
        print(f"\n  Claude Code subscription  ({age_label})")
        print(f"    session (5h)   {pressure_data.get('session_pct', 0.0):.1f}%")
        print(f"    weekly (all)   {pressure_data.get('weekly_pct',  0.0):.1f}%")
        n = pressure_data.get("sonnet_pct", 0.0)
        if n > 0:
            print(f"    weekly sonnet  {n:.1f}%")
    else:
        print("\n  Claude Code subscription  (no data — run llm_check_usage first)")

    # ── Today's routing summary ──
    today_start = datetime.combine(date.today(), datetime.min.time()).replace(tzinfo=timezone.utc)
    today_iso   = today_start.strftime("%Y-%m-%d %H:%M:%S")

    total_calls = 0
    total_cost  = 0.0
    total_in    = 0
    total_out   = 0
    models: dict[str, int] = {}

    if os.path.exists(db_path):
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT model, input_tokens, output_tokens, cost_usd FROM usage "
                "WHERE timestamp >= ? AND success = 1 AND provider != 'subscription'",
                (today_iso,),
            ).fetchall()
            conn.close()
            for r in rows:
                total_calls += 1
                total_cost  += r["cost_usd"] or 0.0
                total_in    += r["input_tokens"]  or 0
                total_out   += r["output_tokens"] or 0
                m = r["model"] or "?"
                models[m] = models.get(m, 0) + 1
        except Exception:
            pass

    SONNET_INPUT_PER_M  = 3.0
    SONNET_OUTPUT_PER_M = 15.0
    baseline = (total_in * SONNET_INPUT_PER_M + total_out * SONNET_OUTPUT_PER_M) / 1_000_000
    saved    = max(0.0, baseline - total_cost)

    print("\n  Today's external routing")
    if total_calls:
        print(f"    calls    {total_calls}")
        print(f"    cost     ${total_cost:.4f}")
        print(f"    saved    ~${saved:.4f} vs Sonnet")
        if models:
            top = sorted(models.items(), key=lambda x: -x[1])[:3]
            for model, count in top:
                short = model.split("/")[-1] if "/" in model else model
                if len(short) > 32:
                    short = short[:30] + "…"
                print(f"    {short:<34} {count}×")
    else:
        print("    no external calls today")

    print("\n  Subcommands:")
    print("    llm-router install     — install hooks globally")
    print("    llm-router dashboard   — web dashboard (localhost:7337)")
    print("─" * WIDTH + "\n")


def _run_dashboard(flags: list[str]) -> None:
    import asyncio

    port = 7337
    for i, flag in enumerate(flags):
        if flag == "--port" and i + 1 < len(flags):
            try:
                port = int(flags[i + 1])
            except ValueError:
                print(f"Invalid port: {flags[i + 1]}")
                sys.exit(1)

    from llm_router.dashboard.server import run
    asyncio.run(run(port=port))


if __name__ == "__main__":
    main()
