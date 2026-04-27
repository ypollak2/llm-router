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

import json
import sys
from pathlib import Path


# ── Helper functions: JSON MCP config management ────────────────────────────────


def _write_json_idempotent(file_path: Path | str, data: dict) -> str:
    """Write JSON file idempotently, returning action message."""
    file_path = Path(file_path)
    file_path.parent.mkdir(parents=True, exist_ok=True)

    # Check if file exists with same content
    if file_path.exists():
        existing = json.loads(file_path.read_text())
        if existing == data:
            return f"skipped: {file_path.name} already has current content"

    file_path.write_text(json.dumps(data, indent=2))
    return f"Created: {file_path}"


def _merge_json_mcp_block(
    config_path: Path | str,
    server_name: str,
    config_dict: dict,
    root_key: str = "mcpServers",
) -> list[str]:
    """Merge MCP server config into JSON file, idempotently.

    Args:
        config_path: Path to JSON config file
        server_name: Name of MCP server (e.g., "llm-router")
        config_dict: Server config dict (e.g., {"command": "uvx"})
        root_key: Root key for servers (default "mcpServers", VS Code uses "servers")

    Returns:
        List of action strings describing what was done
    """
    config_path = Path(config_path)
    actions = []

    # Create parent directories if needed
    config_path.parent.mkdir(parents=True, exist_ok=True)

    # Load existing config or create new
    if config_path.exists():
        data = json.loads(config_path.read_text())
    else:
        data = {}

    # Ensure root_key exists
    if root_key not in data:
        data[root_key] = {}

    # Check if already present (idempotency)
    if server_name in data[root_key]:
        if data[root_key][server_name] == config_dict:
            actions.append(f"skipped: {server_name} already configured in {config_path.name}")
            config_path.write_text(json.dumps(data, indent=2))
            return actions

    # Add/update server config
    data[root_key][server_name] = config_dict
    config_path.write_text(json.dumps(data, indent=2))
    actions.append(f"Added: {server_name} to {config_path}")

    return actions


def _append_routing_rules(
    dest_path: Path | str,
    rules_filename: str,
) -> list[str]:
    """Append routing rules from template file, idempotently.

    Args:
        dest_path: Destination file path
        rules_filename: Name of rules file in src/llm_router/rules/ (e.g., "vscode-rules.md")

    Returns:
        List of action strings describing what was done
    """
    dest_path = Path(dest_path)
    actions = []

    # Load template rules
    rules_dir = Path(__file__).parent / "rules"
    rules_file = rules_dir / rules_filename

    if not rules_file.exists():
        actions.append(f"warning: {rules_filename} not found in {rules_dir}")
        return actions

    rules_content = rules_file.read_text()

    # Create parent directories if needed
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    # Check if already present (idempotency)
    if dest_path.exists():
        existing = dest_path.read_text()
        if "llm-router" in existing:
            actions.append(f"skipped: {dest_path.name} already contains llm-router rules")
            return actions
        # Append to existing file
        with open(dest_path, "a") as f:
            f.write("\n\n" + rules_content)
        actions.append(f"Appended: routing rules to {dest_path}")
    else:
        # Create new file
        dest_path.write_text(rules_content)
        actions.append(f"Created: {dest_path} with routing rules")

    return actions


# ── Platform-specific install functions ────────────────────────────────────────


def _install_vscode_files() -> list[str]:
    """Install llm-router MCP config for VS Code."""
    actions = []
    home = Path.home()

    # VS Code mcp.json location (platform-specific)
    if sys.platform == "darwin":
        mcp_json = home / "Library" / "Application Support" / "Code" / "User" / "mcp.json"
    elif sys.platform == "win32":
        mcp_json = home / "AppData" / "Roaming" / "Code" / "User" / "mcp.json"
    else:
        mcp_json = home / ".config" / "Code" / "User" / "mcp.json"

    # VS Code uses "servers" key, not "mcpServers"
    actions.extend(
        _merge_json_mcp_block(
            mcp_json,
            "llm-router",
            {"command": "uvx", "args": ["claude-code-llm-router"]},
            root_key="servers",
        )
    )

    # Add copilot-instructions.md if in current directory
    github_dir = Path.cwd() / ".github"
    if github_dir.exists():
        instructions = github_dir / "copilot-instructions.md"
        actions.extend(_append_routing_rules(instructions, "vscode-rules.md"))

    return actions


def _install_cursor_files() -> list[str]:
    """Install llm-router MCP config for Cursor IDE."""
    actions = []
    home = Path.home()

    # Cursor mcp.json location
    mcp_json = home / ".cursor" / "mcp.json"
    actions.extend(
        _merge_json_mcp_block(
            mcp_json,
            "llm-router",
            {"command": "uvx", "args": ["claude-code-llm-router"]},
            root_key="mcpServers",
        )
    )

    # Add cursor rules
    cursor_rules = home / ".cursor" / "rules" / "llm-router.md"
    actions.extend(_append_routing_rules(cursor_rules, "cursor-rules.md"))

    return actions


def _install_opencode_files() -> list[str]:
    """Install llm-router MCP config for OpenCode."""
    actions = []
    home = Path.home()

    # OpenCode config
    config = home / ".config" / "opencode" / "config.json"
    actions.extend(
        _merge_json_mcp_block(
            config,
            "llm-router",
            {"command": "uvx", "args": ["claude-code-llm-router"]},
        )
    )

    # OpenCode instructions
    instructions = home / ".config" / "opencode" / "instructions.md"
    actions.extend(_append_routing_rules(instructions, "opencode-rules.md"))

    return actions


def _install_gemini_cli_files() -> list[str]:
    """Install llm-router MCP config for Gemini CLI."""
    actions = []
    home = Path.home()

    # Gemini settings.json
    settings = home / ".gemini" / "settings.json"
    actions.extend(
        _merge_json_mcp_block(
            settings,
            "llm-router",
            {"command": "uvx", "args": ["claude-code-llm-router"]},
        )
    )

    # Gemini extension manifest
    ext_dir = home / ".gemini" / "extensions" / "llm-router"
    manifest = ext_dir / "gemini-extension.json"

    manifest_data = {
        "name": "llm-router",
        "version": "7.6.0",
        "description": "Multi-LLM routing MCP server",
    }
    actions.append(_write_json_idempotent(manifest, manifest_data))

    # Gemini hooks.json
    hooks_file = ext_dir / "hooks" / "hooks.json"

    hooks_data = {
        "hooks": {
            "PostToolUse": {
                "enabled": True,
            }
        }
    }
    actions.append(_write_json_idempotent(hooks_file, hooks_data))

    # Gemini instructions
    instructions = ext_dir / "INSTRUCTIONS.md"
    actions.extend(_append_routing_rules(instructions, "gemini-rules.md"))

    return actions


def _install_copilot_cli_files() -> list[str]:
    """Install llm-router MCP config for GitHub Copilot CLI."""
    actions = []
    home = Path.home()

    # Copilot mcp.json
    mcp_json = home / ".config" / "gh" / "copilot" / "mcp.json"
    actions.extend(
        _merge_json_mcp_block(
            mcp_json,
            "llm-router",
            {"command": "uvx", "args": ["claude-code-llm-router"]},
        )
    )

    # Copilot instructions
    instructions = home / ".config" / "gh" / "copilot" / "instructions.md"
    actions.extend(_append_routing_rules(instructions, "copilot-rules.md"))

    return actions


def _install_openclaw_files() -> list[str]:
    """Install llm-router MCP config for OpenClaw."""
    actions = []
    home = Path.home()

    # OpenClaw mcp.json
    mcp_json = home / ".openclaw" / "mcp.json"
    actions.extend(
        _merge_json_mcp_block(
            mcp_json,
            "llm-router",
            {"command": "uvx", "args": ["claude-code-llm-router"]},
        )
    )

    # OpenClaw instructions
    instructions = home / ".openclaw" / "instructions.md"
    actions.extend(_append_routing_rules(instructions, "openclaw-rules.md"))

    return actions


def _install_trae_files() -> list[str]:
    """Install llm-router MCP config for Trae IDE."""
    actions = []
    home = Path.home()

    # Trae mcp.json (location varies by Trae version, try common location)
    mcp_json = home / ".trae" / "mcp.json"
    actions.extend(
        _merge_json_mcp_block(
            mcp_json,
            "llm-router",
            {"command": "uvx", "args": ["claude-code-llm-router"]},
        )
    )

    return actions


def _install_codex_cli_files() -> list[str]:
    """Install llm-router MCP config for Codex CLI."""
    actions = []
    home = Path.home()

    # Codex CLI config location
    config_json = home / ".codex" / "config.json"
    actions.extend(
        _merge_json_mcp_block(
            config_json,
            "llm-router",
            {"command": "uvx", "args": ["claude-code-llm-router"]},
        )
    )

    # Add Codex rules
    rules_file = home / ".codex" / "rules" / "llm-router.md"
    actions.extend(_append_routing_rules(rules_file, "codex-rules.md"))

    return actions


def _print_claude_desktop_config() -> list[str]:
    """Print Claude Desktop config snippet."""
    config = {
        "mcpServers": {
            "llm-router": {
                "command": "uvx",
                "args": ["claude-code-llm-router"]
            }
        }
    }
    print("Add this to your claude_desktop_config.json:")
    print(json.dumps(config, indent=2))
    return ["Config snippet for claude_desktop_config.json"]


def _print_vs_code_copilot_config() -> list[str]:
    """Print VS Code / Copilot config snippet."""
    config = {
        "servers": {
            "llm-router": {
                "command": "uvx",
                "args": ["claude-code-llm-router"]
            }
        }
    }
    print("Add this to your VS Code mcp.json:")
    print(json.dumps(config, indent=2))
    return ["Config snippet for mcp.json"]


def _install_host(host: str) -> None:
    """Dispatch to appropriate install function based on host."""
    host = host.lower()

    if host in ("vscode", "vs-code"):
        actions = _install_vscode_files()
        print("VS Code configuration:")
        for action in actions:
            print(f"  {action}")
    elif host == "cursor":
        actions = _install_cursor_files()
        print("Cursor IDE configuration:")
        for action in actions:
            print(f"  {action}")
    elif host == "opencode":
        actions = _install_opencode_files()
        print("OpenCode configuration:")
        for action in actions:
            print(f"  {action}")
    elif host == "gemini-cli":
        actions = _install_gemini_cli_files()
        print("Gemini CLI configuration:")
        for action in actions:
            print(f"  {action}")
    elif host == "copilot-cli":
        actions = _install_copilot_cli_files()
        print("GitHub Copilot CLI configuration:")
        for action in actions:
            print(f"  {action}")
    elif host == "openclaw":
        actions = _install_openclaw_files()
        print("OpenClaw configuration:")
        for action in actions:
            print(f"  {action}")
    elif host == "trae":
        actions = _install_trae_files()
        print("Trae IDE configuration:")
        for action in actions:
            print(f"  {action}")
    elif host == "codex":
        actions = _install_codex_cli_files()
        print("Codex CLI configuration:")
        for action in actions:
            print(f"  {action}")
    elif host == "desktop":
        print("Claude Desktop configuration:")
        actions = _print_claude_desktop_config()
        for action in actions:
            print(f"  {action}")
    elif host == "copilot":
        print("VS Code / Copilot configuration:")
        actions = _print_vs_code_copilot_config()
        for action in actions:
            print(f"  {action}")
    elif host == "all":
        for h in ["vscode", "cursor", "opencode", "gemini-cli", "copilot-cli", "openclaw", "trae", "codex", "desktop", "copilot"]:
            _install_host(h)
            print()
    else:
        print(f"Unknown host: {host}")


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
