"""CLI entry point for llm-router.

Usage:
    llm-router                  — start the MCP server (stdio transport)
    llm-router install              — install hooks, rules, and MCP server config globally
    llm-router install --check      — show what would be installed without doing it
    llm-router install --force      — reinstall even if already present
    llm-router install --claw-code  — also install into claw-code (auto-detects ~/.claw-code/)
    llm-router install --headless   — install for Docker/agent/CI environments (API-key mode, no OAuth)
    llm-router install --host codex       — write Codex CLI config files
    llm-router install --host opencode    — write OpenCode config files
    llm-router install --host gemini-cli  — write Gemini CLI config files
    llm-router install --host copilot-cli — write GitHub Copilot CLI config files
    llm-router install --host openclaw    — write OpenClaw config files
    llm-router install --host trae        — write Trae IDE config files
    llm-router install --host factory     — confirm Factory Droid plugin manifest
    llm-router install --host desktop     — print Claude Desktop config snippet
    llm-router install --host copilot     — print VS Code / Copilot config snippet
    llm-router install --host all         — install / print all host configs
    llm-router uninstall        — remove hooks and MCP registration
    llm-router uninstall --purge — also delete ~/.llm-router/ (usage DB, .env, logs)
    llm-router setup            — interactive wizard: configure providers and API keys
    llm-router init-policy      — interactive wizard: choose or create a routing policy (v7.5.0)
    llm-router status           — show routing status, today's savings, subscription pressure
    llm-router doctor           — check that everything is wired up correctly
    llm-router demo             — show routing decisions for sample prompts
    llm-router dashboard        — start the web dashboard at localhost:7337
    llm-router dashboard --port 7338  — use a custom port
    llm-router set-enforce <mode>  — switch enforcement mode (smart|soft|hard|off)
    llm-router team report [period]  — show team savings report (default: week)
    llm-router team push [period]    — push report to Slack/Discord/Telegram/webhook
    llm-router team setup            — interactively configure team endpoint
    llm-router budget                — show all providers with spend, cap, pressure
    llm-router budget set <p> <amt>  — set monthly cap in USD for provider p
    llm-router budget remove <p>     — clear the cap for provider p
    llm-router last [--count N]      — show your last N routing decisions (default: 5)
    llm-router replay [--limit N]    — full transcript of routing decisions this session
    llm-router snapshot [--date DATE] — mid-session monitoring: accuracy trends and gap detection
    llm-router retrospect [--weekly] — IAF-style session debrief with routing directives
    llm-router verify                — end-to-end health check (30 seconds)
"""

from __future__ import annotations

import sys


# ── Main dispatcher ────────────────────────────────────────────────────────────

def main() -> None:
    """Unified CLI: dispatches to MCP server or subcommands."""
    args = sys.argv[1:]

    if args and args[0] == "install":
        from llm_router.commands.install import cmd_install
        cmd_install(args[1:])
    elif args and args[0] == "uninstall":
        from llm_router.commands.uninstall import cmd_uninstall
        cmd_uninstall(args[1:])
    elif args and args[0] == "update":
        from llm_router.commands.update import cmd_update
        cmd_update(args[1:])
    elif args and args[0] == "setup":
        from llm_router.commands.setup import cmd_setup
        cmd_setup(args[1:])
    elif args and args[0] == "status":
        from llm_router.commands.status import cmd_status
        cmd_status(args[1:])
    elif args and args[0] == "routing":
        from llm_router.commands.routing import cmd_routing
        cmd_routing(args[1:])
    elif args and args[0] == "profile":
        from llm_router.commands.profile import cmd_profile
        cmd_profile(args[1:])
    elif args and args[0] == "init-claude-memory":
        from llm_router.cli_init_memory import run_init_claude_memory
        run_init_claude_memory()
    elif args and args[0] == "doctor":
        from llm_router.commands.doctor import cmd_doctor
        cmd_doctor(args[1:])
    elif args and args[0] == "quickstart":
        from llm_router.quickstart import main as _qs_main
        _qs_main()
    elif args and args[0] == "demo":
        from llm_router.commands.demo import cmd_demo
        cmd_demo(args[1:])
    elif args and args[0] == "dashboard":
        from llm_router.commands.dashboard import cmd_dashboard
        cmd_dashboard(args[1:])
    elif args and args[0] == "share":
        from llm_router.commands.share import cmd_share
        cmd_share(args[1:])
    elif args and args[0] == "test":
        from llm_router.commands.test import cmd_test
        cmd_test(args[1:])
    elif args and args[0] == "onboard":
        from llm_router.commands.onboard import cmd_onboard
        cmd_onboard(args[1:])
    elif args and args[0] == "config":
        from llm_router.commands.config import cmd_config
        cmd_config(args[1:])
    elif args and args[0] == "init-policy":
        from llm_router.cli_init_policy import run_init_policy_wizard
        run_init_policy_wizard()
    elif args and args[0] == "set-enforce":
        from llm_router.commands.set_enforce import cmd_set_enforce
        cmd_set_enforce(args[1:])
    elif args and args[0] == "team":
        from llm_router.commands.team import cmd_team
        cmd_team(args[1:])
    elif args and args[0] == "budget":
        from llm_router.commands.budget import cmd_budget
        cmd_budget(args[1:])
    elif args and args[0] == "replay":
        from llm_router.commands.replay import main as _replay_main
        _replay_main(args[1:])
    elif args and args[0] == "verify":
        from llm_router.commands.verify import main as _verify_main
        _verify_main(args[1:])
    elif args and args[0] == "last":
        from llm_router.commands.last import main as _last_main
        _last_main(args[1:])
    elif args and args[0] == "retrospect":
        from llm_router.commands.retrospect import main as _retrospect_main
        _retrospect_main(args[1:])
    elif args and args[0] == "snapshot":
        from llm_router.commands.snapshot import main as _snapshot_main
        _snapshot_main(args[1:])
    else:
        # Default: start the MCP server (original behavior)
        from llm_router.server import main as _mcp_main
        _mcp_main()


if __name__ == "__main__":
    main()
