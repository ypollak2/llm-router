"""CLI entry point for llm_router.

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
    llm-router install --host pi          — write Pi coding agent (pi.dev) config files
    llm-router install --host factory     — confirm Factory Droid plugin manifest
    llm-router install --host desktop     — print Claude Desktop config snippet
    llm-router install --host copilot     — install VS Code / GitHub Copilot pull-routing configs
    llm-router install --host windsurf    — install Windsurf / Cascade pull-routing configs
    llm-router install --host kimi        — install Kimi Code (Moonshot AI) pull-routing configs
    llm-router install --host all         — install / print all host configs
    llm-router uninstall        — remove hooks and MCP registration
    llm-router uninstall --purge — also delete ~/.llm-router/ (usage DB, .env, logs)
    llm-router setup            — interactive wizard: configure providers and API keys
    llm-router init-policy      — interactive wizard: choose or create a routing policy (v7.5.0)
    llm-router status           — show routing status, today's savings, subscription pressure
    llm-router savings-report   — detailed token/cost breakdown (all-time, by model/provider)
    llm-router savings-report --period week  — weekly savings report
    llm-router doctor           — check that everything is wired up correctly
    llm-router okf status       — what knowledge is stored and injected for this project
    llm-router okf gc           — find stored model prose; --apply quarantines it
    llm-router demo             — show routing decisions for sample prompts
    llm-router dashboard        — launch interactive TUI dashboard (real-time monitoring)
    llm-router dashboard --web [--port 7338]  — legacy web dashboard at localhost:7337
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
    llm-router stats [--period recent] — show combined download stats (llm-routing + claude-code-llm_router)
    llm-router verify                — end-to-end health check (30 seconds)
    llm-router gc [--ttl-days N] [--apply] — sweep stale session shards from ~/.llm-router (dry-run by default)
    llm-router soak [--use-gold-complexity] [--full] [--out PATH] — replay the realized-savings soak corpus and write soak/report.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from llm_router.tool_surface import localize# CHZ-SURF-01


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
        server_name: Name of MCP server (e.g., "llm_router")
        config_dict: Server config dict (e.g., {"command": "llm-router"})
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
    """Append routing rules from a template file, idempotently.

    RED1-20 / RED8-06 (WP-08): this WAS a second, independent implementation of
    the same installer, and it differed from the one in `commands/install.py` in
    two ways that both mattered:

    * it did not localize tool names against the active surface, so every
      non-Claude host it served got a rules file naming tools that are not
      registered — the defect CHZ-SURF-01 fixed for Claude Code only;
    * it never recorded to the install manifest, so the rules files it wrote
      survived `llm_router uninstall` entirely.

    Two copies is how one gets fixed and the other does not. There is now one
    implementation; this delegates to it. Kept as a thin alias rather than
    deleted because eight call sites in this module reference it by name, and
    renaming them would be churn with no reader benefit.
    """
    from llm_router.commands.install import _append_routing_rules as _impl

    return _impl(Path(dest_path), rules_filename)


# ── Platform-specific install functions ────────────────────────────────────────


_COPILOT_AGENT_CONTENT = localize("""\
---
description: Cost-optimized assistant that routes every task through LLM Router first.
tools: ['llm_router']
---
You are a cost-aware coding assistant powered by LLM Router routing.

For EVERY request, call the most specific LLM Router tool BEFORE producing your own answer,
then relay the routed result to the user. Do NOT generate your own answer first.

| Task | Tool |
|---|---|
| Code generation, refactors, tests | `llm_code` |
| Questions, explanations, how-to | `llm_query` |
| Debug, review, analysis | `llm_analyze` |
| Research, current events, docs | `llm_research` |
| Prose, docs, email | `llm_generate` |
| Deep reasoning, proofs, root cause | `llm_reason` |

Never skip routing for non-trivial tasks. LLM Router routes to the cheapest capable
model (Ollama → Flash → GPT-4o-mini → Claude), conserving premium quota.
""")


def _install_vscode_files() -> list[str]:
    """Install llm_router MCP config for VS Code / GitHub Copilot (pull routing).

    Installs three things:
    1. User-level mcp.json (global, all workspaces) — the canonical global config.
    2. Workspace .vscode/mcp.json in cwd — project-scoped, committed to repo.
    3. .github/copilot-instructions.md — biases Copilot to call LLM Router tools first.
    4. .github/agents/llm_router.agent.md — tool-first custom agent (strongest lever).

    Pull routing note: Copilot has no UserPromptSubmit hook. These configs make
    LLM Router tools available and instruct the model to call them first, but
    invocation is non-deterministic (model decides). Use Claude Code for
    guaranteed push routing on every turn.
    """
    actions = []
    home = Path.home()

    # ── 1. User-level MCP config (global, all workspaces) ────────────────────
    if sys.platform == "darwin":
        user_mcp = home / "Library" / "Application Support" / "Code" / "User" / "mcp.json"
    elif sys.platform == "win32":
        user_mcp = home / "AppData" / "Roaming" / "Code" / "User" / "mcp.json"
    else:
        user_mcp = home / ".config" / "Code" / "User" / "mcp.json"

    # VS Code uses "servers" key (NOT "mcpServers" — that's the Cursor/Claude Desktop key)
    actions.extend(
        _merge_json_mcp_block(
            user_mcp,
            "llm_router",
            {"type": "stdio", "command": "llm-router", "args": []},
            root_key="servers",
        )
    )

    # ── 2. Workspace .vscode/mcp.json (project-scoped, commit to repo) ───────
    workspace_mcp = Path.cwd() / ".vscode" / "mcp.json"
    workspace_mcp.parent.mkdir(parents=True, exist_ok=True)
    actions.extend(
        _merge_json_mcp_block(
            workspace_mcp,
            "llm_router",
            {"type": "stdio", "command": "llm-router", "args": []},
            root_key="servers",
        )
    )

    # ── 3. .github/copilot-instructions.md ───────────────────────────────────
    github_dir = Path.cwd() / ".github"
    github_dir.mkdir(parents=True, exist_ok=True)
    instructions = github_dir / "copilot-instructions.md"
    actions.extend(_append_routing_rules(instructions, "vscode-rules.md"))

    # ── 4. .github/agents/llm_router.agent.md (tool-first custom agent) ──────────
    agents_dir = github_dir / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    agent_file = agents_dir / "llm_router.agent.md"
    if not agent_file.exists():
        agent_file.write_text(_COPILOT_AGENT_CONTENT, encoding="utf-8")
        actions.append(f"Wrote {agent_file}")
    else:
        actions.append(f"Already exists: {agent_file}")

    actions.append(
        "NOTE (pull routing): Copilot has no hook mechanism. Tools are available "
        "in Agent mode; the model decides when to call them. Enable Agent mode and "
        "select the 'llm_router' agent for best results. For guaranteed routing use "
        "Claude Code (llm_router-install-hooks)."
    )

    return actions


def _install_cursor_files() -> list[str]:
    """Install llm_router MCP config for Cursor IDE."""
    actions = []
    home = Path.home()

    # Cursor mcp.json location
    mcp_json = home / ".cursor" / "mcp.json"
    actions.extend(
        _merge_json_mcp_block(
            mcp_json,
            "llm_router",
            {"command": "llm-router", "args": []},
            root_key="mcpServers",
        )
    )

    # Add cursor rules
    cursor_rules = home / ".cursor" / "rules" / "llm_router.md"
    actions.extend(_append_routing_rules(cursor_rules, "cursor-rules.md"))

    return actions


def _install_opencode_files() -> list[str]:
    """Install llm_router MCP config for OpenCode."""
    actions = []
    home = Path.home()

    # OpenCode config
    config = home / ".config" / "opencode" / "config.json"
    actions.extend(
        _merge_json_mcp_block(
            config,
            "llm_router",
            {"command": "llm-router", "args": []},
        )
    )

    # OpenCode instructions
    instructions = home / ".config" / "opencode" / "instructions.md"
    actions.extend(_append_routing_rules(instructions, "opencode-rules.md"))

    return actions


def _install_gemini_cli_files() -> list[str]:
    """Install llm_router MCP config for Gemini CLI."""
    actions = []
    home = Path.home()

    # Gemini settings.json
    settings = home / ".gemini" / "settings.json"
    actions.extend(
        _merge_json_mcp_block(
            settings,
            "llm_router",
            {"command": "llm-router", "args": []},
        )
    )

    # Gemini extension manifest
    ext_dir = home / ".gemini" / "extensions" / "llm_router"
    manifest = ext_dir / "gemini-extension.json"

    manifest_data = {
        "name": "llm_router",
        "version": "9.0.1",
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
    actions.extend(_append_routing_rules(instructions, "gemini-cli-rules.md"))

    return actions


def _install_copilot_cli_files() -> list[str]:
    """Install llm_router MCP config for GitHub Copilot CLI."""
    actions = []
    home = Path.home()

    # Copilot mcp.json
    mcp_json = home / ".config" / "gh" / "copilot" / "mcp.json"
    actions.extend(
        _merge_json_mcp_block(
            mcp_json,
            "llm_router",
            {"command": "llm-router", "args": []},
        )
    )

    # Copilot instructions
    instructions = home / ".config" / "gh" / "copilot" / "instructions.md"
    actions.extend(_append_routing_rules(instructions, "copilot-cli-rules.md"))

    return actions


def _install_windsurf_files() -> list[str]:
    """Install llm_router MCP config for Windsurf / Cascade (pull routing)."""
    actions = []
    home = Path.home()

    # Windsurf global MCP config
    if sys.platform == "darwin":
        mcp_json = home / "Library" / "Application Support" / "Windsurf" / "User" / "mcp.json"
    elif sys.platform == "win32":
        mcp_json = home / "AppData" / "Roaming" / "Windsurf" / "User" / "mcp.json"
    else:
        mcp_json = home / ".config" / "Windsurf" / "User" / "mcp.json"

    # Windsurf uses "mcpServers" key
    actions.extend(
        _merge_json_mcp_block(
            mcp_json,
            "llm_router",
            {"command": "llm-router", "args": []},
            root_key="mcpServers",
        )
    )

    # Workspace .windsurf/mcp.json (project-scoped)
    workspace_mcp = Path.cwd() / ".windsurf" / "mcp.json"
    workspace_mcp.parent.mkdir(parents=True, exist_ok=True)
    actions.extend(
        _merge_json_mcp_block(
            workspace_mcp,
            "llm_router",
            {"command": "llm-router", "args": []},
            root_key="mcpServers",
        )
    )

    # Windsurf instructions (.github/copilot-instructions.md is also read by Windsurf)
    github_dir = Path.cwd() / ".github"
    github_dir.mkdir(parents=True, exist_ok=True)
    instructions = github_dir / "copilot-instructions.md"
    actions.extend(_append_routing_rules(instructions, "vscode-rules.md"))

    actions.append(
        "NOTE (pull routing): Windsurf/Cascade has no hook mechanism. "
        "Tools are available in Cascade agent mode; the model decides when to call them. "
        "For guaranteed routing use Claude Code (llm_router-install-hooks)."
    )

    return actions


def _install_kimi_files() -> list[str]:
    """Install llm_router MCP config for Kimi Code CLI (Moonshot AI) — pull routing.

    Kimi Code is an MCP client (like Claude Code) but has no UserPromptSubmit hook,
    so routing is pull-based: the model chooses when to call LLM Router tools.

    Installs:
    1. Global ~/.kimi/mcp.json — available in all Kimi Code sessions.
    2. Workspace .kimi/mcp.json — project-scoped, committable to repo.
    3. KIMI.md in cwd — Kimi Code reads this as project-level instructions,
       like CLAUDE.md for Claude Code.
    """
    actions = []
    home = Path.home()

    # Global Kimi Code MCP config
    global_mcp = home / ".kimi" / "mcp.json"
    global_mcp.parent.mkdir(parents=True, exist_ok=True)
    actions.extend(
        _merge_json_mcp_block(
            global_mcp,
            "llm_router",
            {"command": "llm-router", "args": [], "description": (
                "LLM Router smart LLM router — routes tasks to the cheapest capable model. "
                "Call before answering to route to a cheaper capable model."
            )},
            root_key="mcpServers",
        )
    )

    # Workspace .kimi/mcp.json (project-scoped, commit to repo)
    workspace_mcp = Path.cwd() / ".kimi" / "mcp.json"
    workspace_mcp.parent.mkdir(parents=True, exist_ok=True)
    actions.extend(
        _merge_json_mcp_block(
            workspace_mcp,
            "llm_router",
            {"command": "llm-router", "args": []},
            root_key="mcpServers",
        )
    )

    # KIMI.md — project-level instructions (Kimi Code's equivalent of CLAUDE.md)
    kimi_md = Path.cwd() / "KIMI.md"
    kimi_rules = localize("""\

## LLM Router routing — cost-aware LLM routing

LLM Router MCP tools route each task to the cheapest capable model (Ollama → Flash → GPT-4o → Claude).
Call the relevant tool BEFORE generating your own answer — relay the routed result to the user.

> **Pull routing note:** Unlike Claude Code (which intercepts prompts via hooks automatically),
> Kimi Code uses pull routing: YOU must call the tool. The tool returns the response.

| Task | Tool |
|---|---|
| Code generation, refactors, tests | `llm_code` |
| Questions, explanations, how-to | `llm_query` |
| Debug, review, analysis | `llm_analyze` |
| Research, current events, docs | `llm_research` |
| Prose, docs, email | `llm_generate` |
| Deep reasoning, proofs, root cause | `llm_reason` |

Never skip routing for non-trivial tasks. LLM Router routes to the cheapest capable model.
""")
    # The guard must look for something the block ACTUALLY contains. It used to
    # test for "llm_router", a token that appears nowhere in kimi_rules — the
    # text says "LLM Router" with a space throughout, and localize() rewrites
    # the tool names to llm(task="…"). So the guard was always true and every
    # install appended another copy; the committed KIMI.md in this repo reached
    # 53 of them. Anchor on the section heading instead, which is part of the
    # written text and therefore cannot drift away from it.
    _KIMI_MARKER = "## LLM Router routing"
    if kimi_md.exists():
        content = kimi_md.read_text()
        if _KIMI_MARKER not in content:
            kimi_md.write_text(content + kimi_rules)
            actions.append(f"Appended: LLM Router routing rules to {kimi_md}")
            # It also recorded nothing, so uninstall had no way to strip what it
            # wrote and every copy survived `llm_router uninstall`.
            try:
                from llm_router import install_manifest
                install_manifest.record("text_block", kimi_md, block=kimi_rules)
            except Exception:
                pass  # a manifest write must never break install
        else:
            actions.append(f"Skipped: {kimi_md} already has LLM Router rules")
    else:
        kimi_md.write_text(f"# Project Instructions\n{kimi_rules}")
        actions.append(f"Created: {kimi_md} with LLM Router routing rules")
        try:
            from llm_router import install_manifest
            install_manifest.record("created_file", kimi_md, block=kimi_rules)
        except Exception:
            pass

    actions.append(
        "NOTE (pull routing): Kimi Code has no UserPromptSubmit hook. "
        "LLM Router tools are available in the MCP tool menu; the model decides when to call them. "
        "For guaranteed routing, use Claude Code (llm_router-install-hooks)."
    )

    return actions


def _install_openclaw_files() -> list[str]:
    """Install llm_router MCP config for OpenClaw."""
    actions = []
    home = Path.home()

    # OpenClaw mcp.json
    mcp_json = home / ".openclaw" / "mcp.json"
    actions.extend(
        _merge_json_mcp_block(
            mcp_json,
            "llm_router",
            {"command": "llm-router", "args": []},
        )
    )

    # OpenClaw instructions
    instructions = home / ".openclaw" / "instructions.md"
    actions.extend(_append_routing_rules(instructions, "openclaw-rules.md"))

    return actions


def _install_trae_files() -> list[str]:
    """Install llm_router MCP config for Trae IDE."""
    actions = []
    home = Path.home()

    # Trae mcp.json (location varies by Trae version, try common location)
    mcp_json = home / ".trae" / "mcp.json"
    actions.extend(
        _merge_json_mcp_block(
            mcp_json,
            "llm_router",
            {"command": "llm-router", "args": []},
        )
    )

    # Trae routing rules
    rules_dest = home / ".trae" / "rules" / "llm_router.md"
    actions.extend(_append_routing_rules(rules_dest, "trae-rules.md"))

    return actions


def _install_pi_files() -> list[str]:
    """Install llm_router MCP config for Pi coding agent (pi.dev)."""
    actions = []
    home = Path.home()

    # Pi agent MCP config: ~/.pi/agent/mcp.json
    mcp_json = home / ".pi" / "agent" / "mcp.json"
    actions.extend(
        _merge_json_mcp_block(
            mcp_json,
            "llm_router",
            {
                "command": "llm-router",
                "args": [],
                "lifecycle": "lazy",
            },
        )
    )

    # Pi agent instructions
    instructions = home / ".pi" / "agent" / "INSTRUCTIONS.md"
    actions.extend(_append_routing_rules(instructions, "pi-rules.md"))

    return actions


def _print_claude_desktop_config() -> list[str]:
    """Print Claude Desktop config snippet."""
    config = {
        "mcpServers": {
            "llm_router": {
                "command": "llm-router",
                "args": []
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
            "llm_router": {
                "command": "llm-router",
                "args": []
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
    elif host == "pi":
        actions = _install_pi_files()
        print("Pi coding agent (pi.dev) configuration:")
        for action in actions:
            print(f"  {action}")
    elif host == "codex":
        # One implementation only (RED8-06): the real writer lives in
        # commands/install.py and targets config.toml, hooks.json, AGENTS.md.
        from llm_router.commands.install import _install_codex_files
        actions = _install_codex_files()
        print("Codex CLI configuration:")
        for action in actions:
            print(f"  {action}")
    elif host == "desktop":
        print("Claude Desktop configuration:")
        actions = _print_claude_desktop_config()
        for action in actions:
            print(f"  {action}")
    elif host in ("copilot", "vscode-copilot", "github-copilot"):
        # --host copilot: full install of VS Code/Copilot pull-routing configs
        actions = _install_vscode_files()
        print("GitHub Copilot / VS Code configuration (pull routing):")
        for action in actions:
            print(f"  {action}")
    elif host in ("windsurf", "cascade"):
        actions = _install_windsurf_files()
        print("Windsurf / Cascade configuration (pull routing):")
        for action in actions:
            print(f"  {action}")
    elif host in ("kimi", "kimi-code", "moonshot"):
        actions = _install_kimi_files()
        print("Kimi Code / Moonshot AI configuration (pull routing):")
        for action in actions:
            print(f"  {action}")
    elif host == "all":
        for h in ["vscode", "cursor", "windsurf", "kimi", "opencode", "gemini-cli", "copilot-cli", "openclaw", "trae", "pi", "codex", "desktop", "copilot"]:
            _install_host(h)
            print()
    else:
        print(f"Unknown host: {host}")


# ── Main dispatcher ────────────────────────────────────────────────────────────

def isolation_test_command() -> None:
    """Run the isolation test suite for router health verification.

    Validates: cache isolation, routing logic, dashboard accuracy, database persistence.
    """
    from llm_router.cli_help import handle_help

    # GH#51: this ignored --help and ran the full health check instead.
    handle_help(
        "llm-router-isolation-test",
        "Run the router isolation suite: cache isolation, routing logic, "
        "dashboard accuracy and database persistence.",
        notes="Runs real checks against ~/.llm-router/ and may take a minute.",
    )

    import subprocess
    from pathlib import Path

    # Try to find the bash script first (for repo installations)
    package_dir = Path(__file__).parent.parent.parent
    script_path = package_dir / "scripts" / "router_isolation_test.sh"

    if script_path.exists():
        # Run via bash script if available
        result = subprocess.run(
            ["bash", str(script_path)] + sys.argv[1:],
            cwd=Path.home() / ".llm-router"
        )
        sys.exit(result.returncode)

    # For tool installations, pytest may not have access to tests directory
    # Run a simple health check instead
    print("Running llm_router health check...")
    print()

    # Quick health checks without pytest
    try:
        from llm_router.commands.status import cmd_status
        print("✓ Status check:")
        cmd_status([])
        print()
        print("✅ Router health check passed!")
        print()
        print("For comprehensive isolation tests, run from a llm_router repo clone:")
        print("  pytest tests/test_isolation_routing.py -v")
        sys.exit(0)
    except Exception as e:
        print(f"✗ Health check failed: {e}")
        sys.exit(1)


def _make_output_encoding_safe() -> None:
    """Stop the CLI dying on Windows because its own output has emoji in it.

    CHZ-WIN-01. `llm_router doctor` prints ✓ / ✗ / ⚡ / 💰. On Windows the console
    default is cp1252, which cannot encode any of them, so the first status glyph
    raises UnicodeEncodeError and the command exits non-zero with a traceback —
    on a machine where nothing is actually wrong.

    CI did not catch this because the windows smoke job sets PYTHONUTF8=1 and
    PYTHONIOENCODING=utf-8 at the JOB level. Those env vars make the suite pass
    and do nothing for a user, who has neither. Adding them to the new
    docs-command job would have turned CI green while leaving every Windows user
    with a crashing `doctor` — fixing the CI signal instead of the defect.

    `errors="replace"` rather than a strict re-encode: a console that genuinely
    cannot represent a glyph should print `?` and carry on. A diagnostic command
    that refuses to run because it cannot draw a tick is worse than one that
    draws the wrong character.

    No-op where stdout is already UTF-8, and guarded because a detached or
    redirected stream may not support reconfigure at all — this must never be
    the reason a command fails.
    """
    for stream in (sys.stdout, sys.stderr):
        enc = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
        if enc in ("utf8", "utf8mb4"):
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, OSError, ValueError):
            pass  # nothing to do; better a mangled glyph than a dead command


# Every literal subcommand name dispatched below (GH#61). Used only to power
# a "did you mean" suggestion for typos — NOT a second source of truth for
# dispatch itself, so it can drift without breaking anything except the
# suggestion quality. Keep it roughly in sync with the if/elif chain.
_KNOWN_SUBCOMMANDS = frozenset(
    {
        "install",
        "uninstall",
        "update",
        "setup",
        "status",
        "probe",
        "welcome",
        "dev-refresh",
        "serve",
        "doctor",
        "init-policy",
        "demo",
        "dashboard",
        "set-enforce",
        "team",
        "budget",
        "config",
        "profile",
        "routing",
        "routing-report",
        "broker",
        "gateway",
        "invoice",
        "cp",
        "share",
        "summary",
        "onboard",
        "quickstart",
        "init-claude-memory",
        "okf",
        "tui",
        "test",
        "verify",
        "audit",
        "last",
        "replay",
        "gc",
        "soak",
        "retrospect",
        "snapshot",
        "stats",
        "savings-report",
        "benchmark",
        "test-delta",
        "migrate",
        "team-sync",
        "policy",
        "explain-dashboard",
    }
)


def main() -> None:
    """Unified CLI: dispatches to MCP server or subcommands."""
    _make_output_encoding_safe()
    args = sys.argv[1:]

    if args and args[0] in ("-h", "--help"):
        print(__doc__)
        return

    if args and args[0] in ("-v", "--version"):
        from llm_router import __version__
        print(f"llm_router v{__version__}")
        return

    # `run-hook <path>` — execute a hook script in this interpreter.
    #
    # Hooks are registered as `<interpreter> <hook>.py`, which assumes a Python
    # on the machine. A standalone binary has none: sys.executable IS the
    # binary, and handing it a .py path would just be an unknown argument. This
    # gives the frozen build a way to run its own hooks, so a user without
    # Python still gets auto-routing rather than only the MCP tools.
    #
    # Deliberately not argparse'd or advertised in --help: it is an internal
    # entry point that install_hooks writes into settings.json, not something to
    # invoke by hand.
    if args and args[0] == "run-hook":
        if len(args) < 2:
            print("llm-router run-hook: expected a hook path", file=sys.stderr)
            sys.exit(2)
        import runpy

        hook_path = args[1]
        # The hook reads sys.argv itself, so present it the argv it would have
        # seen had it been launched directly.
        sys.argv = [hook_path, *args[2:]]
        try:
            runpy.run_path(hook_path, run_name="__main__")
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001
            # A hook that raises must not take the host's turn down with it —
            # the same fail-open contract the hooks apply internally.
            print(f"llm-router run-hook: {hook_path} failed: {exc}", file=sys.stderr)
            sys.exit(0)
        return

    if args and args[0] == "install":
        from llm_router.commands.install import cmd_install
        sys.exit(cmd_install(args[1:]))
    elif args and args[0] == "uninstall":
        from llm_router.commands.uninstall import cmd_uninstall
        sys.exit(cmd_uninstall(args[1:]))
    elif args and args[0] == "update":
        from llm_router.commands.update import cmd_update
        sys.exit(cmd_update(args[1:]))
    elif args and args[0] == "setup":
        from llm_router.commands.setup import cmd_setup
        sys.exit(cmd_setup(args[1:]))
    elif args and args[0] == "status":
        from llm_router.commands.status import cmd_status
        sys.exit(cmd_status(args[1:]))
    elif args and args[0] == "probe":
        # Probe which locally-installed Ollama models can actually drive the
        # agentic tool-loop, cache the verdicts, and show the dynamic pick.
        from llm_router.commands.probe import cmd_probe
        sys.exit(cmd_probe(args[1:]))
    elif args and args[0] == "welcome":
        # Print the painterly LLM Router banner on demand. Use this from your
        # shell rc (e.g., `claude` wrapper function in ~/.zshrc) to put the
        # welcome in your terminal scrollback before Claude Code's TUI takes
        # over — Claude Code's SessionStart hooks cannot surface output to
        # the user's terminal directly.
        from llm_router.commands.welcome import cmd_welcome
        sys.exit(cmd_welcome(args[1:]))
    elif args and args[0] == "dev-refresh":
        # Full dev refresh: reinstall package, sync hooks, restart MCP
        # servers — all three layers that need updating after a source
        # edit. Wraps the three-step pipeline that historically caused
        # "I reinstalled but my change isn't live" confusion when any
        # one layer was skipped.
        from llm_router.commands.dev_refresh import cmd_dev_refresh
        sys.exit(cmd_dev_refresh(args[1:]))
    elif args and args[0] == "serve":
        # E3: run llm_router as a long-lived HTTP service (container / systemd
        # entrypoint). Default = secured SSE MCP server; --admin = admin API.
        from llm_router.commands.serve import cmd_serve
        sys.exit(cmd_serve(args[1:]))
    elif args and args[0] == "gateway":
        # OpenAI-compatible HTTP gateway: any litellm/openai client routes through
        # LLM Router by pointing OPENAI_BASE_URL at it. The Surface-C fix.
        from llm_router.gateway import main as gateway_main
        gateway_main()
    elif args and args[0] == "broker":
        # Session broker: run from an INTERACTIVE terminal so the headless gateway
        # daemon can delegate gated backends (Codex/Gemini CLI) that need the
        # session's live auth. See llm_router.session_broker.
        import asyncio as _asyncio
        from llm_router.session_broker import run_broker_server
        try:
            _asyncio.run(run_broker_server())
        except KeyboardInterrupt:
            print("\nsession broker stopped.")
    elif args and args[0] == "routing-report":
        # Observability: deep-dive report of what routed (tokens / latency / savings).
        from llm_router.routing_report import main as report_main
        report_main()
    elif args and args[0] == "invoice":
        from llm_router.commands.invoice import cmd_invoice
        sys.exit(cmd_invoice(args[1:]))
    elif args and args[0] == "cp":
        from llm_router.commands.cp import cmd_cp
        sys.exit(cmd_cp(args[1:]))
    elif args and args[0] == "routing":
        from llm_router.commands.routing import cmd_routing
        sys.exit(cmd_routing(args[1:]))
    elif args and args[0] == "profile":
        from llm_router.commands.profile import cmd_profile
        sys.exit(cmd_profile(args[1:]))
    elif args and args[0] == "init-claude-memory":
        from llm_router.cli_init_memory import run_init_claude_memory
        run_init_claude_memory()
    elif args and args[0] == "okf":
        from llm_router.commands.okf import cmd_okf
        cmd_okf(args[1:])
    elif args and args[0] == "doctor":
        from llm_router.commands.doctor import cmd_doctor
        sys.exit(cmd_doctor(args[1:]))
    elif args and args[0] == "quickstart":
        from llm_router.quickstart import main as _qs_main
        _qs_main()
    elif args and args[0] == "demo":
        from llm_router.commands.demo import cmd_demo
        sys.exit(cmd_demo(args[1:]))
    elif args and args[0] == "dashboard":
        from llm_router.commands.dashboard import cmd_dashboard
        sys.exit(cmd_dashboard(args[1:]))
    elif args and args[0] == "summary":
        # Session Summary Dashboard — rich terminal overview.
        # Flags: --since-hours N, --limit N, --markdown, --watch,
        #        --watch-interval N (seconds, default 5)
        from llm_router.observability.summary import cli_summary
        rest = args[1:]
        since = 24.0
        limit = 5000
        markdown = False
        watch = False
        watch_interval = 5.0
        i = 0
        while i < len(rest):
            tok = rest[i]
            if tok == "--since-hours" and i + 1 < len(rest):
                since = float(rest[i + 1])
                i += 2
                continue
            if tok == "--limit" and i + 1 < len(rest):
                limit = int(rest[i + 1])
                i += 2
                continue
            if tok == "--markdown":
                markdown = True
                i += 1
                continue
            if tok == "--watch":
                watch = True
                i += 1
                continue
            if tok == "--watch-interval" and i + 1 < len(rest):
                watch_interval = float(rest[i + 1])
                i += 2
                continue
            i += 1
        raise SystemExit(cli_summary(
            since_hours=since, limit=limit, markdown=markdown,
            watch=watch, watch_interval=watch_interval,
        ))
    elif args and args[0] == "tui":
        from llm_router.dashboard.tui import run as _tui_run
        _tui_run()
    elif args and args[0] == "share":
        from llm_router.commands.share import cmd_share
        sys.exit(cmd_share(args[1:]))
    elif args and args[0] == "test":
        from llm_router.commands.test import cmd_test
        sys.exit(cmd_test(args[1:]))
    elif args and args[0] == "onboard":
        from llm_router.commands.onboard import cmd_onboard
        sys.exit(cmd_onboard(args[1:]))
    elif args and args[0] == "config":
        from llm_router.commands.config import cmd_config
        sys.exit(cmd_config(args[1:]))
    elif args and args[0] == "init-policy":
        from llm_router.cli_init_policy import run_init_policy_wizard
        run_init_policy_wizard()
    elif args and args[0] == "set-enforce":
        from llm_router.commands.set_enforce import cmd_set_enforce
        sys.exit(cmd_set_enforce(args[1:]))
    elif args and args[0] == "team":
        from llm_router.commands.team import cmd_team
        sys.exit(cmd_team(args[1:]))
    elif args and args[0] == "budget":
        from llm_router.commands.budget import cmd_budget
        sys.exit(cmd_budget(args[1:]))
    elif args and args[0] == "replay":
        from llm_router.commands.replay import main as _replay_main
        _replay_main(args[1:])
    elif args and args[0] == "verify":
        # CHZ-PKG-005: propagate verify's exit code. main() returns 1 when any
        # health check fails; discarding it made `llm_router verify` always exit 0,
        # so a CI/install gate keying on the exit code treated a broken install
        # as healthy.
        from llm_router.commands.verify import main as _verify_main
        sys.exit(_verify_main(args[1:]))
    elif args and args[0] == "audit":
        from llm_router.commands.audit import main as _audit_main
        sys.exit(_audit_main(args[1:]))
    elif args and args[0] == "last":
        from llm_router.commands.last import main as _last_main
        _last_main(args[1:])
    elif args and args[0] == "gc":
        from llm_router.commands.gc import main as _gc_main
        sys.exit(_gc_main(args[1:]))
    elif args and args[0] == "soak":
        from llm_router.commands.soak import cmd_soak
        sys.exit(cmd_soak(args[1:]))
    elif args and args[0] == "retrospect":
        from llm_router.commands.retrospect import main as _retrospect_main
        _retrospect_main(args[1:])
    elif args and args[0] == "snapshot":
        from llm_router.commands.snapshot import main as _snapshot_main
        _snapshot_main(args[1:])
    elif args and args[0] == "stats":
        from llm_router.commands.stats import cmd_stats
        sys.exit(cmd_stats(args[1:]))
    elif args and args[0] == "savings-report":
        from llm_router.commands.savings_report import main as _savings_report_main
        sys.exit(_savings_report_main(args[1:]))
    elif args and args[0] == "benchmark":
        from llm_router.commands.benchmark import cmd_benchmark
        sys.exit(cmd_benchmark(args[1:]))
    elif args and args[0] == "test-delta":
        from llm_router.test_delta import main as _td_main
        sys.exit(_td_main(args[1:]))
    elif args and args[0] == "migrate":
        from llm_router.commands.migrate import main as _migrate_main
        sys.exit(_migrate_main(args[1:]))
    elif args and args[0] == "team-sync":
        from llm_router.commands.team_sync import main as _team_sync_main
        sys.exit(_team_sync_main(args[1:]))
    elif args and args[0] == "policy":
        from llm_router.commands.policy import cmd_policy
        sys.exit(cmd_policy(args[1:]))
    elif args and args[0] == "explain-dashboard":
        from llm_router.commands.explain_dashboard import cmd_explain_dashboard
        sys.exit(cmd_explain_dashboard(args[1:]))
    elif args:
        # GH#61: an unrecognized args[0] used to fall through to the final
        # `else` below and silently start the MCP stdio server, which then
        # hangs forever waiting for JSON-RPC input on a bare terminal. Any
        # non-empty, unmatched subcommand belongs here instead — never in
        # the server-startup branch.
        import difflib

        suggestion = difflib.get_close_matches(args[0], _KNOWN_SUBCOMMANDS, n=1)
        msg = f"llm-router: unknown command '{args[0]}' — see 'llm-router --help'"
        if suggestion:
            msg += f" (did you mean '{suggestion[0]}'?)"
        print(msg, file=sys.stderr)
        sys.exit(2)
    else:
        # Default: start the MCP server (original behavior). Only reached
        # when no CLI arguments were given at all — this is the documented
        # `llm-router` (no args) behavior.
        from llm_router.server import main as _mcp_main
        _mcp_main()


if __name__ == "__main__":
    main()
