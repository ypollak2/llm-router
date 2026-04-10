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
    llm-router status           — show routing status, today's savings, subscription pressure
    llm-router doctor           — check that everything is wired up correctly
    llm-router demo             — show routing decisions for sample prompts
    llm-router dashboard        — start the web dashboard at localhost:7337
    llm-router dashboard --port 7338  — use a custom port
    llm-router set-enforce <mode>  — switch enforcement mode (smart|soft|hard|off)
    llm-router team report [period]  — show team savings report (default: week)
    llm-router team push [period]    — push report to Slack/Discord/Telegram/webhook
    llm-router team setup            — interactively configure team endpoint
"""

from __future__ import annotations

import os
import sys


# ── ANSI helpers (respect NO_COLOR / non-tty) ─────────────────────────────────

def _color_enabled() -> bool:
    return sys.stdout.isatty() and not os.getenv("NO_COLOR")


def _green(s: str) -> str:
    return f"\033[32m{s}\033[0m" if _color_enabled() else s


def _red(s: str) -> str:
    return f"\033[31m{s}\033[0m" if _color_enabled() else s


def _yellow(s: str) -> str:
    return f"\033[33m{s}\033[0m" if _color_enabled() else s


def _bold(s: str) -> str:
    return f"\033[1m{s}\033[0m" if _color_enabled() else s


def _dim(s: str) -> str:
    return f"\033[2m{s}\033[0m" if _color_enabled() else s


def _visual_len(s: str) -> int:
    """Return visible character count, stripping ANSI escape codes."""
    import re
    return len(re.sub(r'\033\[[0-9;]*m', '', s))


def _pad(s: str, width: int) -> str:
    """Left-justify s to visual width (handles ANSI-colored strings correctly)."""
    return s + " " * max(0, width - _visual_len(s))


def _ok(label: str) -> str:
    return f"  {_green('✓')}  {label}"


def _warn(label: str) -> str:
    return f"  {_yellow('⚠')}  {label}"


def _fail(label: str, fix: str | None = None) -> str:
    line = f"  {_red('✗')}  {label}"
    if fix:
        line += f"\n       {_yellow('→')} {fix}"
    return line


# ── Main dispatcher ────────────────────────────────────────────────────────────

def main() -> None:
    """Unified CLI: dispatches to MCP server or subcommands."""
    args = sys.argv[1:]

    if args and args[0] == "install":
        _run_install(flags=args[1:])
    elif args and args[0] == "uninstall":
        _run_uninstall(flags=args[1:])
    elif args and args[0] == "update":
        _run_update()
    elif args and args[0] == "setup":
        _run_setup()
    elif args and args[0] == "status":
        _run_status()
    elif args and args[0] == "doctor":
        _run_doctor()
    elif args and args[0] == "demo":
        _run_demo()
    elif args and args[0] == "dashboard":
        _run_dashboard(flags=args[1:])
    elif args and args[0] == "share":
        _run_share()
    elif args and args[0] == "test":
        _run_test(prompt=" ".join(args[1:]))
    elif args and args[0] == "onboard":
        _run_onboard()
    elif args and args[0] == "config":
        _run_config(flags=args[1:])
    elif args and args[0] == "set-enforce":
        _run_set_enforce(mode=args[1] if len(args) > 1 else "")
    elif args and args[0] == "team":
        _run_team(subcmd=args[1] if len(args) > 1 else "report", flags=args[2:])
    else:
        # Default: start the MCP server (original behavior)
        from llm_router.server import main as _mcp_main
        _mcp_main()


# ── install ────────────────────────────────────────────────────────────────────

def _run_install(flags: list[str]) -> None:
    # --host <name> is handled before the regular install path — it prints
    # config snippets only (no file modifications to external tools).
    if "--host" in flags:
        idx = flags.index("--host")
        host = flags[idx + 1] if idx + 1 < len(flags) else "all"
        _install_host(host)
        return

    check_only = "--check" in flags
    force = "--force" in flags

    from llm_router.install_hooks import (
        _HOOKS_DST, _HOOKS_SRC, _HOOK_DEFS,
        _RULES_DST, check_api_keys, claude_desktop_config_path,
        install,
    )

    if check_only:
        print(f"\n{_bold('[llm-router] Install check')}  (no changes made)\n")

        print(_bold("  Hooks & rules"))
        all_ok = True
        for src_name, dst_name, event, _ in _HOOK_DEFS:
            src = _HOOKS_SRC / src_name
            dst = _HOOKS_DST / dst_name
            if not src.exists():
                print(_fail(f"{src_name}  {_yellow('(source missing in package)')}"))
                all_ok = False
            elif dst.exists():
                print(_ok(f"{dst_name}  ({event})"))
            else:
                print(_fail(
                    f"{dst_name}  ({event})  — not installed",
                    fix="llm-router install",
                ))
                all_ok = False

        rules_dst = _RULES_DST / "llm-router.md"
        if rules_dst.exists():
            print(_ok("llm-router.md  (routing rules)"))
        else:
            print(_fail("llm-router.md  (routing rules)  — not installed",
                        fix="llm-router install"))
            all_ok = False

        print(f"\n{_bold('  Claude Desktop')}")
        desktop_path = claude_desktop_config_path()
        if desktop_path is None:
            print(_warn("unsupported platform"))
        else:
            import json as _json
            registered = False
            if desktop_path.exists():
                try:
                    cfg = _json.loads(desktop_path.read_text())
                    registered = "llm-router" in cfg.get("mcpServers", {})
                except Exception:
                    pass
            if registered:
                print(_ok(f"registered  ({desktop_path})"))
            elif desktop_path.exists():
                print(_fail(
                    f"not registered  ({desktop_path})",
                    fix="llm-router install",
                ))
                all_ok = False
            else:
                print(_warn(f"config not found  ({desktop_path})"))

        print(f"\n{_bold('  Provider keys')}")
        for line in check_api_keys():
            print(f"  {line}")

        print()
        if all_ok:
            print(_green("  Everything looks good."))
        else:
            print(_yellow("  Run `llm-router install` to fix the issues above."))
        print()
        return

    claw_code = "--claw-code" in flags
    headless  = "--headless"  in flags

    if headless:
        _run_install_headless()
        return

    if force:
        from llm_router.install_hooks import _load_settings, _save_settings
        settings = _load_settings()
        settings.get("mcpServers", {}).pop("llm-router", None)
        _save_settings(settings)

    print(f"\n{_bold('╔══════════════════════════════════════════╗')}")
    print(f"{_bold('║   LLM Router — One-Command Install        ║')}")
    print(f"{_bold('╚══════════════════════════════════════════╝')}\n")

    actions = install()
    for a in actions:
        print(f"  {_green('✓')}  {a}")

    # ── claw-code (explicit flag or auto-detect) ──────────────────────────
    from llm_router.install_hooks import install_claw_code, claw_code_settings_path
    cc_detected = claw_code_settings_path() is not None
    if claw_code or cc_detected:
        if cc_detected and not claw_code:
            print(f"\n{_bold('  claw-code detected — installing hooks...')}")
        cc_actions = install_claw_code()
        for a in cc_actions:
            ok = not a.startswith("SKIP")
            marker = _green('✓') if ok else _yellow('⚠')
            print(f"  {marker}  {a}")

    print(f"\n{_green('✓')} {_bold('LLM Router installed globally.')}")
    print("  Every Claude Code session will now auto-route tasks.")
    print("  Restart Claude Code (and Claude Desktop if installed) to activate.\n")

    print(_bold("  Provider keys (optional — router works without any):"))
    for line in check_api_keys():
        print(f"  {line}")

    print(f"\n{_bold('  Try it:')}")
    print('    In Claude Code, ask: "What does os.path.join do?"')
    print("    You'll see: ⚡ ROUTE → Haiku (simple query)\n")

    print(_bold("  Subcommands:"))
    print("    llm-router doctor               — verify everything is wired up")
    print("    llm-router status               — today's cost & savings")
    print("    llm-router dashboard            — web dashboard (localhost:7337)")
    print("    llm-router install --check      — preview install state")
    print("    llm-router install --force      — reinstall / update paths")
    print("    llm-router install --claw-code  — also install into claw-code")
    print("    llm-router uninstall            — remove\n")


# ── install --headless ─────────────────────────────────────────────────────────

def _run_install_headless() -> None:
    """Install for Docker / CI / agent environments (API-key mode, no OAuth).

    Runs the standard hook + MCP install, then prints a Dockerfile snippet showing
    the complete wiring needed for a Claude Code agent container.
    """
    from llm_router.install_hooks import install

    print(f"\n{_bold('╔══════════════════════════════════════════════╗')}")
    print(f"{_bold('║   LLM Router — Headless / Agent Install       ║')}")
    print(f"{_bold('╚══════════════════════════════════════════════╝')}\n")
    print(_dim("  API-key mode — no subscription, no OAuth, routes directly to external providers.\n"))

    actions = install()
    for a in actions:
        print(f"  {_green('✓')}  {a}")

    print(f"\n{_green('✓')} {_bold('Hooks and MCP server installed.')}\n")

    print(_bold("  Dockerfile snippet (bake hooks into agent image):"))
    print(_dim("  ─────────────────────────────────────────────────────────────"))
    snippet = """\
  FROM python:3.12-slim

  # Install llm-router and wire in hooks
  RUN pip install claude-code-llm-router && llm-router install

  # Route to API providers — no Anthropic subscription in CI
  ENV LLM_ROUTER_CLAUDE_SUBSCRIPTION=false

  # At least one provider key required for the fallback chain
  # (pass at runtime via --env or K8s secret)
  ENV GEMINI_API_KEY=""
  ENV OPENAI_API_KEY=""
  ENV GROQ_API_KEY=""
  ENV DEEPSEEK_API_KEY=""
"""
    print(snippet)

    print(_bold("  .claude/settings.json — MCP + hooks merge example:"))
    print(_dim("  (llm-router install does this automatically; shown for reference)"))
    import json as _json
    example = {
        "mcpServers": {"llm-router": {"command": "llm-router", "args": []}},
        "hooks": {
            "UserPromptSubmit": [{"matcher": "", "hooks": [{"type": "command", "command": "python3 ~/.claude/hooks/llm-router-auto-route.py"}]}],
            "Stop":             [{"matcher": "", "hooks": [{"type": "command", "command": "python3 ~/.claude/hooks/llm-router-session-end.py"}]}],
        }
    }
    print("  " + _dim(_json.dumps(example, indent=2).replace("\n", "\n  ")))

    print(f"\n{_bold('  Verification (run inside the container after a job):')}")
    print("    grep ROUTE /proc/1/fd/1            # look for routing log lines")
    print("    cat ~/.llm-router/usage.json        # routing stats")
    print("    sqlite3 ~/.llm-router/usage.db \\")
    print('      "SELECT model, COUNT(*) FROM usage GROUP BY model"\n')


# ── install --host (print config snippets for non-Claude Code hosts) ──────────

_HOST_SNIPPETS: dict[str, str] = {
    "codex": """\
{bold}Codex CLI{reset}  (capability extension — no cost-routing)
──────────────────────────────────────────────────────────────────
1. Add to ~/.codex/config.yaml:

   mcp:
     servers:
       llm-router:
         command: uvx
         args: [claude-code-llm-router]

2. Copy routing rules so Codex knows when to call llm_auto:

   cp "$(python3 -c "import llm_router; import pathlib; print(pathlib.Path(llm_router.__file__).parent / 'rules' / 'codex-rules.md')")" \\
      ~/.codex/instructions.md

3. Restart Codex — run llm_savings to verify the DB is shared.
""",

    "desktop": """\
{bold}Claude Desktop{reset}  (capability extension — no cost-routing)
──────────────────────────────────────────────────────────────────
Edit ~/Library/Application Support/Claude/claude_desktop_config.json
(Linux: ~/.config/Claude/claude_desktop_config.json)
(Windows: %APPDATA%\\Claude\\claude_desktop_config.json)

Add inside the top-level object:

  "mcpServers": {{
    "llm-router": {{
      "command": "uvx",
      "args": ["claude-code-llm-router"],
      "env": {{
        "LLM_ROUTER_PROFILE": "balanced"
      }}
    }}
  }}

Restart Claude Desktop. Run llm_savings to confirm DB is shared.
Note: cost-routing is not available in Desktop (no hook system).
""",

    "copilot": """\
{bold}GitHub Copilot (VS Code){reset}  (capability extension — no cost-routing)
──────────────────────────────────────────────────────────────────
1. Create or edit .vscode/mcp.json in your workspace:

   {{
     "servers": {{
       "llm-router": {{
         "command": "uvx",
         "args": ["claude-code-llm-router"]
       }}
     }}
   }}

2. Optionally add routing guidance to .github/copilot-instructions.md:

   When a task requires live web search, call the llm_research MCP tool.
   When a task requires image generation, call the llm_image MCP tool.
   For auto-routing with savings tracking, call llm_auto.

3. Enable MCP in VS Code settings (Copilot > MCP: Enable).
   Restart VS Code. Run @llm-router llm_savings to verify.
Note: cost-routing is not available in Copilot (no hook system).
""",

    "opencode": """\
{bold}OpenCode{reset}  — writing config files…
""",

    "gemini-cli": """\
{bold}Gemini CLI{reset}  — writing config files…
""",

    "copilot-cli": """\
{bold}GitHub Copilot CLI{reset}  — writing config files…
""",

    "openclaw": """\
{bold}OpenClaw{reset}  — writing config files…
""",

    "trae": """\
{bold}Trae IDE{reset}  — writing config files…
""",

    "factory": """\
{bold}Factory Droid{reset}  — writing config files…
""",
}


def _install_codex_files() -> list[str]:
    """Write Codex-specific config files and return a list of actions taken."""
    import pathlib
    import shutil as _shutil

    actions: list[str] = []
    home = pathlib.Path.home()

    # 1. MCP server entry in ~/.codex/config.yaml
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True, exist_ok=True)
    config_yaml = codex_dir / "config.yaml"

    mcp_block = (
        "\nmcp:\n"
        "  servers:\n"
        "    llm-router:\n"
        "      command: uvx\n"
        "      args: [claude-code-llm-router]\n"
    )
    existing = config_yaml.read_text() if config_yaml.exists() else ""
    if "llm-router" not in existing:
        with config_yaml.open("a") as f:
            f.write(mcp_block)
        actions.append(f"✓ Added llm-router MCP server to {config_yaml}")
    else:
        actions.append(f"  llm-router already in {config_yaml} (skipped)")

    # 2. PostToolUse hook in ~/.codex/hooks.json
    hooks_json = codex_dir / "hooks.json"
    hook_script = home / ".llm-router" / "hooks" / "codex-post-tool.py"

    # Copy the hook script to ~/.llm-router/hooks/
    hook_script.parent.mkdir(parents=True, exist_ok=True)
    src_hook = pathlib.Path(__file__).parent / "hooks" / "codex-post-tool.py"
    if src_hook.exists():
        _shutil.copy2(src_hook, hook_script)
        hook_script.chmod(0o755)
        actions.append(f"✓ Installed hook script to {hook_script}")

    hook_entry = {
        "hooks": {
            "PostToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": str(hook_script)}],
                }
            ]
        }
    }
    if hooks_json.exists():
        try:
            import json as _json
            current = _json.loads(hooks_json.read_text())
            existing_hooks = current.get("hooks", {}).get("PostToolUse", [])
            already = any(
                str(hook_script) in str(h)
                for entry in existing_hooks
                for h in entry.get("hooks", [])
            )
            if not already:
                existing_hooks.append(hook_entry["hooks"]["PostToolUse"][0])
                current.setdefault("hooks", {})["PostToolUse"] = existing_hooks
                hooks_json.write_text(_json.dumps(current, indent=2))
                actions.append(f"✓ Added PostToolUse hook to {hooks_json}")
            else:
                actions.append(f"  Hook already in {hooks_json} (skipped)")
        except Exception as e:
            actions.append(f"  Could not update {hooks_json}: {e}")
    else:
        import json as _json
        hooks_json.write_text(_json.dumps(hook_entry, indent=2))
        actions.append(f"✓ Created {hooks_json} with PostToolUse hook")

    # 3. Copy routing rules to ~/.codex/instructions.md (append if exists)
    instructions = codex_dir / "instructions.md"
    rules_src = pathlib.Path(__file__).parent / "rules" / "codex-rules.md"
    if rules_src.exists():
        rules_text = rules_src.read_text()
        if instructions.exists():
            existing_inst = instructions.read_text()
            if "llm-router" not in existing_inst:
                with instructions.open("a") as f:
                    f.write(f"\n\n{rules_text}")
                actions.append(f"✓ Appended routing rules to {instructions}")
            else:
                actions.append(f"  Routing rules already in {instructions} (skipped)")
        else:
            instructions.write_text(rules_text)
            actions.append(f"✓ Created {instructions} with routing rules")

    return actions


def _merge_json_mcp_block(config_path, server_name: str, server_entry: dict) -> list[str]:
    """Merge an MCP server entry into a JSON config file. Returns list of action strings."""
    import json as _json
    import pathlib

    actions: list[str] = []
    config_path = pathlib.Path(config_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)

    existing = {}
    if config_path.exists():
        try:
            existing = _json.loads(config_path.read_text())
        except Exception:
            existing = {}

    mcp_key = "mcpServers"
    servers = existing.setdefault(mcp_key, {})
    if server_name not in servers:
        servers[server_name] = server_entry
        config_path.write_text(_json.dumps(existing, indent=2))
        actions.append(f"✓ Added llm-router MCP server to {config_path}")
    else:
        actions.append(f"  llm-router already in {config_path} (skipped)")
    return actions


def _append_routing_rules(dest_path, rules_filename: str) -> list[str]:
    """Append routing rules from src/rules/ to dest_path. Returns list of action strings."""
    import pathlib

    actions: list[str] = []
    dest_path = pathlib.Path(dest_path)
    rules_src = pathlib.Path(__file__).parent / "rules" / rules_filename
    if not rules_src.exists():
        return actions
    rules_text = rules_src.read_text()
    if dest_path.exists():
        existing = dest_path.read_text()
        if "llm-router" not in existing:
            with dest_path.open("a") as f:
                f.write(f"\n\n{rules_text}")
            actions.append(f"✓ Appended routing rules to {dest_path}")
        else:
            actions.append(f"  Routing rules already in {dest_path} (skipped)")
    else:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_text(rules_text)
        actions.append(f"✓ Created {dest_path} with routing rules")
    return actions


def _copy_hook_script(hook_filename: str, dest_dir) -> tuple[str, list[str]]:
    """Copy a hook script from src/hooks/ to dest_dir. Returns (dest_path_str, actions)."""
    import pathlib
    import shutil as _shutil

    actions: list[str] = []
    dest_dir = pathlib.Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    src = pathlib.Path(__file__).parent / "hooks" / hook_filename
    dest = dest_dir / hook_filename
    if src.exists():
        _shutil.copy2(src, dest)
        dest.chmod(0o755)
        actions.append(f"✓ Installed hook script to {dest}")
    return str(dest), actions


def _install_opencode_files() -> list[str]:
    """Write OpenCode-specific config files and return a list of actions taken."""
    import pathlib

    actions: list[str] = []
    home = pathlib.Path.home()
    opencode_dir = home / ".config" / "opencode"
    opencode_dir.mkdir(parents=True, exist_ok=True)

    # 1. MCP server entry
    server_entry = {"command": "uvx", "args": ["claude-code-llm-router"]}
    actions += _merge_json_mcp_block(opencode_dir / "config.json", "llm-router", server_entry)

    # 2. Hook script
    hook_dest, hook_actions = _copy_hook_script(
        "opencode-post-tool.py", home / ".llm-router" / "hooks"
    )
    actions += hook_actions

    # 3. Routing rules
    actions += _append_routing_rules(opencode_dir / "instructions.md", "opencode-rules.md")

    return actions


def _install_gemini_cli_files() -> list[str]:
    """Write Gemini CLI-specific config files and return a list of actions taken."""
    import json as _json
    import pathlib

    actions: list[str] = []
    home = pathlib.Path.home()
    gemini_dir = home / ".gemini"
    gemini_dir.mkdir(parents=True, exist_ok=True)

    # 1. MCP server entry in ~/.gemini/settings.json
    server_entry = {"command": "uvx", "args": ["claude-code-llm-router"]}
    actions += _merge_json_mcp_block(gemini_dir / "settings.json", "llm-router", server_entry)

    # 2. Extension manifest + hooks directory
    ext_dir = gemini_dir / "extensions" / "llm-router"
    ext_dir.mkdir(parents=True, exist_ok=True)

    ext_manifest = ext_dir / "gemini-extension.json"
    if not ext_manifest.exists():
        manifest = {
            "name": "llm-router",
            "version": "3.5.0",
            "description": "Route tasks to cheapest capable model — 20+ providers",
            "mcpServers": {"llm-router": server_entry},
        }
        ext_manifest.write_text(_json.dumps(manifest, indent=2))
        actions.append(f"✓ Created Gemini CLI extension manifest at {ext_manifest}")
    else:
        actions.append(f"  Extension manifest already exists at {ext_manifest} (skipped)")

    # 3. Hook script
    hook_dest, hook_actions = _copy_hook_script(
        "gemini-cli-post-tool.py", home / ".llm-router" / "hooks"
    )
    actions += hook_actions

    # 4. Extension hooks.json
    hooks_dir = ext_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hooks_json = hooks_dir / "hooks.json"
    hook_entry = {
        "hooks": {
            "PostToolUse": [
                {"matcher": "*", "command": hook_dest}
            ]
        }
    }
    if not hooks_json.exists():
        hooks_json.write_text(_json.dumps(hook_entry, indent=2))
        actions.append(f"✓ Created Gemini CLI hooks.json at {hooks_json}")
    else:
        actions.append(f"  hooks.json already exists at {hooks_json} (skipped)")

    # 5. Routing rules
    actions += _append_routing_rules(ext_dir / "INSTRUCTIONS.md", "gemini-cli-rules.md")

    return actions


def _install_copilot_cli_files() -> list[str]:
    """Write GitHub Copilot CLI config files and return a list of actions taken."""
    import pathlib

    actions: list[str] = []
    home = pathlib.Path.home()

    # Copilot CLI MCP config — ~/.config/gh/copilot/mcp.json
    copilot_dir = home / ".config" / "gh" / "copilot"
    copilot_dir.mkdir(parents=True, exist_ok=True)

    server_entry = {"command": "uvx", "args": ["claude-code-llm-router"]}
    actions += _merge_json_mcp_block(copilot_dir / "mcp.json", "llm-router", server_entry)

    # Routing rules → ~/.config/gh/copilot/instructions.md
    actions += _append_routing_rules(
        copilot_dir / "instructions.md", "copilot-cli-rules.md"
    )

    return actions


def _install_openclaw_files() -> list[str]:
    """Write OpenClaw config files and return a list of actions taken."""
    import pathlib

    actions: list[str] = []
    home = pathlib.Path.home()
    openclaw_dir = home / ".openclaw"
    openclaw_dir.mkdir(parents=True, exist_ok=True)

    server_entry = {"command": "uvx", "args": ["claude-code-llm-router"]}
    actions += _merge_json_mcp_block(openclaw_dir / "mcp.json", "llm-router", server_entry)
    actions += _append_routing_rules(openclaw_dir / "instructions.md", "openclaw-rules.md")

    return actions


def _install_trae_files() -> list[str]:
    """Write Trae IDE config files and return a list of actions taken."""
    import pathlib
    import sys

    actions: list[str] = []
    home = pathlib.Path.home()

    # Trae config location differs by platform
    if sys.platform == "darwin":
        trae_dir = home / "Library" / "Application Support" / "Trae"
    elif sys.platform == "win32":
        trae_dir = pathlib.Path(home / "AppData" / "Roaming" / "Trae")
    else:
        trae_dir = home / ".config" / "Trae"
    trae_dir.mkdir(parents=True, exist_ok=True)

    server_entry = {"command": "uvx", "args": ["claude-code-llm-router"]}
    actions += _merge_json_mcp_block(trae_dir / "mcp.json", "llm-router", server_entry)

    # .rules file in current project directory (Trae-specific pattern)
    rules_dest = pathlib.Path(".rules")
    actions += _append_routing_rules(rules_dest, "trae-rules.md")

    return actions


def _install_factory_files() -> list[str]:
    """Factory Droid uses .claude-plugin/ format natively — just confirm it's present."""
    import pathlib

    actions: list[str] = []
    plugin_dir = pathlib.Path(__file__).parent.parent.parent.parent / ".factory-plugin"
    if plugin_dir.exists():
        actions.append("✓ .factory-plugin/ manifest present — Factory Droid will auto-load it")
        actions.append("  Install via: factory plugin install ypollak2/llm-router")
    else:
        actions.append("  .factory-plugin/ not found in repo root — run from the llm-router repo dir")
    actions.append("  Or install .claude-plugin/ directly: factory plugin install ypollak2/llm-router")
    return actions


def _install_host(host: str) -> None:
    """Install config for non-Claude Code hosts (writes files for Codex; prints snippets for others)."""
    import shutil

    bold = "\033[1m" if _color_enabled() else ""
    reset = "\033[0m" if _color_enabled() else ""

    hosts_to_show = list(_HOST_SNIPPETS.keys()) if host == "all" else [host]
    unknown = [h for h in hosts_to_show if h not in _HOST_SNIPPETS]
    if unknown:
        print(f"Unknown host(s): {', '.join(unknown)}")
        print(f"Valid options: {', '.join(_HOST_SNIPPETS)} or 'all'")
        return

    w = shutil.get_terminal_size((80, 24)).columns
    print(f"\n{bold}llm-router install --host {host}{reset}\n")
    print("─" * min(w, 70))

    # Hosts that write files; all others print snippets
    _FILE_WRITERS = {
        "codex":      (_install_codex_files,       "Restart Codex and run llm_savings to verify."),
        "opencode":   (_install_opencode_files,     "Restart OpenCode and run llm_savings to verify."),
        "gemini-cli": (_install_gemini_cli_files,   "Restart Gemini CLI and run llm_savings to verify."),
        "copilot-cli":(_install_copilot_cli_files,  "Restart Copilot CLI and run llm_savings to verify."),
        "openclaw":   (_install_openclaw_files,     "Restart OpenClaw and run llm_savings to verify."),
        "trae":       (_install_trae_files,         "Restart Trae IDE and run llm_savings to verify."),
        "factory":    (_install_factory_files,      "Run: factory plugin install ypollak2/llm-router"),
    }

    for h in hosts_to_show:
        if h in _FILE_WRITERS:
            install_fn, verify_hint = _FILE_WRITERS[h]
            label = _HOST_SNIPPETS[h].format(bold=bold, reset=reset).strip()
            print(f"{label}\n")
            actions = install_fn()
            for action in actions:
                print(f"  {action}")
            print()
            print(f"  {verify_hint}")
        else:
            snippet = _HOST_SNIPPETS[h].format(bold=bold, reset=reset)
            print(snippet)
        print("─" * min(w, 70))

    print(
        f"\nFor Claude Code (hooks + full cost-routing): {bold}llm-router install{reset}\n"
        f"See docs/hosts/ for setup guides and trade-off explanations.\n"
    )


# ── uninstall ──────────────────────────────────────────────────────────────────

def _run_uninstall(flags: list[str] | None = None) -> None:
    import shutil
    from pathlib import Path

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


# ── doctor ─────────────────────────────────────────────────────────────────────

def _run_doctor() -> None:
    """Comprehensive health check — verify every component is wired up."""
    import json
    import time
    import urllib.request
    from pathlib import Path

    from llm_router.install_hooks import (
        _HOOKS_DST, _HOOKS_SRC, _HOOK_DEFS,
        _RULES_DST, _SETTINGS_PATH,
        check_api_keys, claude_desktop_config_path,
    )

    issues: list[str] = []

    print(f"\n{_bold('llm-router doctor')}\n")

    # ── 1. Hooks ──────────────────────────────────────────────────────────────
    print(_bold("  Hooks"))
    for src_name, dst_name, event, _ in _HOOK_DEFS:
        dst = _HOOKS_DST / dst_name
        src = _HOOKS_SRC / src_name
        if dst.exists():
            # Check version freshness
            src_v = _hook_version_num(src)
            dst_v = _hook_version_num(dst)
            if src_v > dst_v:
                print(_warn(f"{dst_name}  v{dst_v} installed, v{src_v} available"))
                issues.append(f"Hook {dst_name} is outdated — run `llm-router install --force`")
            else:
                print(_ok(f"{dst_name}  ({event})"))
        else:
            print(_fail(f"{dst_name}  ({event})  — not installed",
                        fix="llm-router install"))
            issues.append(f"Hook {dst_name} not installed")

    # ── 2. Routing rules ──────────────────────────────────────────────────────
    print(f"\n{_bold('  Routing rules')}")
    rules_dst = _RULES_DST / "llm-router.md"
    if rules_dst.exists():
        print(_ok("llm-router.md"))
    else:
        print(_fail("llm-router.md — not installed", fix="llm-router install"))
        issues.append("Routing rules not installed")

    # ── 3. Claude Code MCP registration ──────────────────────────────────────
    print(f"\n{_bold('  Claude Code MCP')}")
    settings: dict = {}
    if _SETTINGS_PATH.exists():
        try:
            settings = json.loads(_SETTINGS_PATH.read_text())
        except Exception:
            pass
    registered_cc = "llm-router" in settings.get("mcpServers", {})
    if registered_cc:
        print(_ok("MCP server registered in ~/.claude/settings.json"))
    else:
        print(_fail("MCP server not registered",
                    fix="llm-router install"))
        issues.append("MCP server not registered in Claude Code")

    # ── 4. Claude Desktop ────────────────────────────────────────────────────
    print(f"\n{_bold('  Claude Desktop')}")
    desktop_path = claude_desktop_config_path()
    if desktop_path is None:
        print(_warn("not supported on this platform"))
    elif not desktop_path.exists():
        print(_warn(f"config not found ({desktop_path}) — Claude Desktop may not be installed"))
    else:
        try:
            cfg = json.loads(desktop_path.read_text())
            if "llm-router" in cfg.get("mcpServers", {}):
                print(_ok(f"registered ({desktop_path})"))
            else:
                print(_fail("not registered in Claude Desktop",
                            fix="llm-router install"))
                issues.append("MCP server not registered in Claude Desktop")
        except Exception as e:
            print(_fail(f"could not read config: {e}"))

    # ── 5. Ollama ─────────────────────────────────────────────────────────────
    print(f"\n{_bold('  Ollama (optional — free local classifier)')}")
    ollama_url = os.getenv("OLLAMA_URL", "http://localhost:11434")
    try:
        req = urllib.request.Request(f"{ollama_url}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read())
            model_names = [m.get("name", "") for m in data.get("models", [])]
            if model_names:
                preview = ", ".join(model_names[:3])
                if len(model_names) > 3:
                    preview += f" +{len(model_names) - 3} more"
                print(_ok(f"running — {len(model_names)} model(s): {preview}"))
            else:
                print(_warn("running but no models pulled — run `ollama pull qwen2.5:0.5b`"))
    except Exception:
        print(_warn(f"not reachable at {ollama_url} — optional, but saves API cost"))

    # ── 6. Usage data freshness ───────────────────────────────────────────────
    print(f"\n{_bold('  Usage data (Claude subscription pressure)')}")
    usage_path = Path.home() / ".llm-router" / "usage.json"
    if not usage_path.exists():
        print(_warn("usage.json not found — run `llm_check_usage` in Claude Code to populate"))
    else:
        try:
            data = json.loads(usage_path.read_text())
            age_s = time.time() - data.get("updated_at", 0)
            if age_s < 1800:
                print(_ok(f"fresh ({int(age_s / 60)}m old)"))
            elif age_s < 3600:
                print(_warn(f"getting stale ({int(age_s / 60)}m old) — run `llm_check_usage`"))
            else:
                print(_fail(f"stale ({int(age_s / 3600)}h old) — routing may use wrong pressure",
                            fix="Run llm_check_usage in Claude Code"))
                issues.append("Usage data is stale")
        except Exception as e:
            print(_fail(f"could not read usage.json: {e}"))

    # ── 7. Provider keys ──────────────────────────────────────────────────────
    print(f"\n{_bold('  Provider API keys')}")
    for line in check_api_keys():
        print(f"  {line}")

    # ── 8. claw-code ─────────────────────────────────────────────────────────
    print(f"\n{_bold('  claw-code (optional — open-source Claude Code alternative)')}")
    from llm_router.install_hooks import (
        _CLAW_CODE_HOOK_DEFS, _claw_code_dir,
    )
    cc_dir = _claw_code_dir()
    if cc_dir is None:
        print(_dim("  not detected (install at github.com/claw-code/claw-code)"))
    else:
        cc_hooks_dst = cc_dir / "hooks"
        cc_settings = {}
        cc_settings_path = cc_dir / "settings.json"
        if cc_settings_path.exists():
            try:
                cc_settings = json.loads(cc_settings_path.read_text())
            except Exception:
                pass
        for _, dst_name, event, _ in _CLAW_CODE_HOOK_DEFS:
            dst = cc_hooks_dst / dst_name
            if dst.exists():
                print(_ok(f"{dst_name}  ({event})"))
            else:
                print(_fail(f"{dst_name}  — not installed",
                            fix="llm-router install --claw-code"))
                issues.append(f"claw-code hook {dst_name} not installed")
                pass
        if "llm-router" in cc_settings.get("mcpServers", {}):
            print(_ok("MCP server registered in claw-code settings.json"))
        else:
            print(_fail("MCP server not registered in claw-code",
                        fix="llm-router install --claw-code"))
            issues.append("MCP server not registered in claw-code")

    # ── 9. Version ────────────────────────────────────────────────────────────
    print(f"\n{_bold('  Version')}")
    try:
        from importlib.metadata import version
        v = version("claude-code-llm-router")
        print(_ok(f"claude-code-llm-router {v}"))
    except Exception:
        print(_warn("could not determine installed version"))

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    if not issues:
        print(_green(_bold("  ✓ All checks passed. LLM Router is healthy.")))
    else:
        print(_red(_bold(f"  {len(issues)} issue(s) found:")))
        for issue in issues:
            print(f"    {_red('•')} {issue}")
    print()


def _hook_version_num(path) -> int:
    """Read the version number embedded in a hook file header."""
    import re
    _re = re.compile(r"#\s*llm-router-hook-version:\s*(\d+)")
    try:
        for line in path.read_text(encoding="utf-8").splitlines()[:5]:
            m = _re.search(line)
            if m:
                return int(m.group(1))
    except OSError:
        pass
    return 0


# ── setup ──────────────────────────────────────────────────────────────────────

_PROVIDERS_WIZARD = [
    ("GEMINI_API_KEY",       "Google Gemini",  "Gemini 2.5 Pro/Flash + Imagen — 1M tokens/day FREE tier",   "aistudio.google.com/apikey"),
    ("PERPLEXITY_API_KEY",   "Perplexity",     "Web-grounded research (live search results)",               "perplexity.ai/settings/api"),
    ("OPENAI_API_KEY",       "OpenAI",         "GPT-4o, o3, DALL-E, Whisper",                              "platform.openai.com/api-keys"),
    ("GROQ_API_KEY",         "Groq",           "Ultra-fast inference — generous FREE tier",                 "console.groq.com/keys"),
    ("DEEPSEEK_API_KEY",     "DeepSeek",       "High-quality coding at 10x lower cost than GPT-4o",        "platform.deepseek.com/api-keys"),
    ("MISTRAL_API_KEY",      "Mistral",        "EU-hosted, GDPR-friendly, strong European models",         "console.mistral.ai/api-keys"),
    ("ANTHROPIC_API_KEY",    "Anthropic API",  "Direct API access (distinct from CC subscription)",        "console.anthropic.com/settings/keys"),
]


def _run_setup() -> None:
    """Interactive wizard: configure providers and write API keys to ~/.llm-router/.env."""
    from pathlib import Path

    env_path = Path.home() / ".llm-router" / ".env"
    env_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"\n{_bold('LLM Router — Setup Wizard')}\n")
    print("This wizard configures your provider API keys.")
    print("Keys are saved to ~/.llm-router/.env and loaded automatically by the router.\n")

    # ── Step 1: Claude Code subscription ──────────────────────────────────────
    print(_bold("Step 1: Claude Code subscription"))
    cc_mode = os.getenv("LLM_ROUTER_CLAUDE_SUBSCRIPTION", "")
    if cc_mode.lower() in ("true", "1", "yes"):
        print(_ok("LLM_ROUTER_CLAUDE_SUBSCRIPTION is already set — Claude models routed via subscription."))
        enable_cc = True
    else:
        ans = input("  Do you have a Claude Code subscription (Pro/Max)? [Y/n]: ").strip().lower()
        enable_cc = ans in ("", "y", "yes")
        if enable_cc:
            print(_green("  ✓ Claude subscription mode enabled — Claude models used for free via subscription."))
        else:
            print("  Skipping — Claude models will be used via API (requires ANTHROPIC_API_KEY).")

    # ── Step 2: External providers ─────────────────────────────────────────────
    print(f"\n{_bold('Step 2: External providers')}  (all optional — skip with Enter)\n")

    new_keys: dict[str, str] = {}
    if enable_cc:
        new_keys["LLM_ROUTER_CLAUDE_SUBSCRIPTION"] = "true"

    for env_var, name, description, url in _PROVIDERS_WIZARD:
        existing = os.getenv(env_var, "")
        if existing:
            print(_ok(f"{name} — already configured"))
            continue
        print(f"  {_bold(name)}")
        print(f"  {description}")
        print(f"  Get key: {url}")
        key = input(f"  {env_var}: ").strip()
        if key:
            new_keys[env_var] = key
            print(_green(f"  ✓ {env_var} saved"))
        else:
            print(f"  {_yellow('→')} skipped")
        print()

    # ── Write .env ──────────────────────────────────────────────────────────────
    if new_keys:
        # Load existing .env keys to merge
        existing_env: dict[str, str] = {}
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    existing_env[k.strip()] = v.strip().strip("\"'")
        merged = {**existing_env, **new_keys}

        lines = ["# LLM Router provider keys — auto-generated by `llm-router setup`", ""]
        for k, v in merged.items():
            lines.append(f"{k}={v}")
        env_path.write_text("\n".join(lines) + "\n")

        print(_green(_bold(f"\n  ✓ Saved {len(new_keys)} key(s) to {env_path}")))
        print(f"\n  {_bold('To load in current shell:')}")
        print(f"    source {env_path}")
        print(f"\n  {_bold('To load automatically (add to ~/.zshrc or ~/.bashrc):')}")
        print(f"    [ -f {env_path} ] && source {env_path}")
    else:
        print(_yellow("  No new keys entered."))

    # ── Step 3: Install hooks ──────────────────────────────────────────────────
    print()
    ans = input("Run `llm-router install` now? [Y/n]: ").strip().lower()
    if ans in ("", "y", "yes"):
        _run_install(flags=[])
    else:
        print(f"\n  Run {_bold('llm-router install')} when ready to activate routing.\n")


# ── demo ───────────────────────────────────────────────────────────────────────

def _load_real_routing_history(db_path: str, limit: int = 8) -> list[tuple]:
    """Return the last *limit* real routing decisions from usage.db.

    Returns list of (prompt_snippet, task_type, complexity, model, cost_str).
    Empty list if DB missing or table has no external calls.
    """
    import sqlite3
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT prompt, task_type, complexity, model, cost_usd "
            "FROM usage WHERE success=1 AND provider!='subscription' "
            "AND prompt IS NOT NULL "
            "ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
        conn.close()
    except Exception:
        return []

    result = []
    for r in rows:
        prompt = (r["prompt"] or "").strip()[:44]
        if len(r["prompt"] or "") > 44:
            prompt = prompt[:43] + "…"
        task   = (r["task_type"] or "query")[:8]
        compl  = (r["complexity"] or "moderate")[:12]
        model  = (r["model"] or "?").split("/")[-1][:18]
        cost   = f"${r['cost_usd']:.5f}" if (r["cost_usd"] or 0) < 0.001 else f"${r['cost_usd']:.4f}"
        result.append((f'"{prompt}"', task, compl, model, cost))
    return result


def _run_demo() -> None:
    """Show routing decisions — real history if available, examples otherwise."""

    db_path = os.path.expanduser("~/.llm-router/usage.db")
    real_rows = _load_real_routing_history(db_path)
    using_real = bool(real_rows)

    # Fallback static examples
    cc_mode = os.getenv("LLM_ROUTER_CLAUDE_SUBSCRIPTION", "").lower() in ("true", "1", "yes")
    has_perplexity = bool(os.getenv("PERPLEXITY_API_KEY"))
    has_openai     = bool(os.getenv("OPENAI_API_KEY"))
    has_gemini     = bool(os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"))

    EXAMPLE_CASES = [
        ('"what does os.path.join do?"',          "query",    "simple",     "Claude Haiku",    "$0.00001"),
        ('"why is my async code slow?"',           "analyze",  "moderate",   "Claude Sonnet",   "$0.003"),
        ('"implement a Redis-backed rate limiter"',"code",     "complex",    "Claude Opus",     "$0.015"),
        ('"prove the halting problem is undecidable"',"analyze","deep_reason","Opus+thinking",  "$0.030"),
        ('"research latest Gemini 2.5 benchmarks"',"research", "moderate",  "Perplexity" if has_perplexity else "Claude Sonnet", "$0.002"),
        ('"write a hero section for a SaaS landing"',"generate","moderate",  "Claude Sonnet",   "$0.001"),
        ('"generate a dashboard screenshot mockup"',"image",   "—",          "Flux Pro",        "$0.040"),
    ]

    cases = real_rows if using_real else EXAMPLE_CASES
    col_w = [44, 8, 12, 18, 9]
    sep = "─" * (sum(col_w) + len(col_w) * 2 + 2)

    title = "your last routing decisions" if using_real else "how smart routing handles your prompts"
    print(f"\n{_bold('llm-router demo')}  — {title}\n")

    if not using_real:
        config_parts = []
        if cc_mode:
            config_parts.append("Claude Code subscription")
        if has_perplexity:
            config_parts.append("Perplexity")
        if has_openai:
            config_parts.append("OpenAI")
        if has_gemini:
            config_parts.append("Gemini")
        if not config_parts:
            config_parts.append("no external APIs configured")
        print(f"  Active config: {', '.join(config_parts)}")
        print(f"  {_dim('(no routing history yet — showing examples)')}\n")
    else:
        print(f"  {_dim('Source: ~/.llm-router/usage.db  (your actual routing decisions)')}\n")

    print(f"  {'Prompt':<{col_w[0]}}  {'Task':<{col_w[1]}}  {'Complexity':<{col_w[2]}}  {'Model':<{col_w[3]}}  {'Cost'}")
    print("  " + sep)

    total_routed = 0.0
    total_opus   = 0.0
    for prompt, task, complexity, model, cost_str in cases:
        if complexity == "simple":
            compl_label = _green(complexity)
        elif complexity in ("moderate", "—"):
            compl_label = _yellow(complexity)
        elif complexity in ("complex", "deep_reason", "deep_reasoning"):
            compl_label = _red(complexity)
        else:
            compl_label = complexity

        try:
            cost_val = float(cost_str.lstrip("$"))
            cost_label = _green(cost_str) if cost_val < 0.002 else (
                _yellow(cost_str) if cost_val < 0.01 else _red(cost_str))
        except ValueError:
            cost_label = cost_str

        prompt_disp = prompt if len(prompt) <= col_w[0] else prompt[:col_w[0] - 1] + "…"
        print(
            f"  {_pad(prompt_disp, col_w[0])}"
            f"  {_pad(task, col_w[1])}"
            f"  {_pad(compl_label, col_w[2])}"
            f"  {_pad(model, col_w[3])}"
            f"  {cost_label}"
        )
        try:
            total_routed += float(cost_str.lstrip("$"))
            total_opus   += 0.015
        except ValueError:
            pass

    print("  " + sep)

    if total_opus > 0:
        savings_pct = 100 * (1 - total_routed / total_opus)
        savings_str = f"${total_opus:.3f} → ${total_routed:.3f}  ({savings_pct:.0f}% cheaper)"
        print(f"\n  {_bold('Savings vs always-Opus:')}  {_green(savings_str)}")

    if not using_real:
        print(f"\n  {_yellow('→')} Run {_bold('llm-router setup')} to configure API keys for more providers.")
    print(f"  {_yellow('→')} Run {_bold('llm-router status')} for cumulative savings.")
    print(f"  {_yellow('→')} Run {_bold('llm-router dashboard')} to see live routing decisions.\n")


# ── status ─────────────────────────────────────────────────────────────────────

def _savings_bar(saved: float, cost: float, width: int = 28) -> str:
    """Return a colored ASCII bar showing saved vs spent fractions."""
    total = saved + cost
    if total <= 0:
        return "  " + "─" * width
    save_w = max(1, round(saved / total * width))
    cost_w = max(0, width - save_w)
    return "  " + _green("█" * save_w) + _yellow("░" * cost_w)


def _query_routing_period(db_path: str, since_iso: str) -> tuple[int, float, float]:
    """Return (calls, cost_usd, baseline_usd) for calls since *since_iso*."""
    import sqlite3
    SONNET_IN  = 3.0   # $/M tokens
    SONNET_OUT = 15.0
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT input_tokens, output_tokens, cost_usd FROM usage "
            "WHERE timestamp >= ? AND success = 1 AND provider != 'subscription'",
            (since_iso,),
        ).fetchall()
        conn.close()
        calls, cost, baseline = 0, 0.0, 0.0
        for r in rows:
            calls   += 1
            cost    += r["cost_usd"] or 0.0
            baseline += ((r["input_tokens"] or 0) * SONNET_IN
                         + (r["output_tokens"] or 0) * SONNET_OUT) / 1_000_000
        return calls, cost, baseline
    except Exception:
        return 0, 0.0, 0.0


def _query_free_model_savings(db_path: str) -> list[dict]:
    """Return per-provider savings rows for zero-cost providers (Ollama, Codex).

    Each row: {provider, calls, in_tok, out_tok, cost_usd, baseline_usd, saved_usd}
    ``baseline_usd`` is what those tokens would cost at Sonnet-3.5 API rates.
    Codex tokens are not tracked (always 0); its baseline is estimated from the
    average tokens/call across all tracked providers.
    """
    import sqlite3
    SONNET_IN, SONNET_OUT = 3.0, 15.0
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        # Average tokens per call across paid providers (used to estimate Codex)
        avg_row = conn.execute(
            "SELECT AVG(input_tokens), AVG(output_tokens) FROM usage "
            "WHERE success=1 AND provider NOT IN ('subscription','ollama','codex') "
            "AND input_tokens > 0"
        ).fetchone()
        avg_in  = float(avg_row[0] or 0)
        avg_out = float(avg_row[1] or 0)

        rows = conn.execute(
            "SELECT provider, COUNT(*) as calls, "
            "COALESCE(SUM(input_tokens),0) as in_tok, "
            "COALESCE(SUM(output_tokens),0) as out_tok, "
            "COALESCE(SUM(cost_usd),0) as cost_usd "
            "FROM usage WHERE success=1 AND provider IN ('ollama','codex') "
            "GROUP BY provider ORDER BY calls DESC"
        ).fetchall()
        conn.close()

        result = []
        for r in rows:
            in_t = r["in_tok"] or 0
            out_t = r["out_tok"] or 0
            calls = r["calls"]

            # If tokens not tracked (Codex), estimate from avg paid-provider tokens
            if in_t == 0 and out_t == 0:
                in_t  = int(avg_in  * calls)
                out_t = int(avg_out * calls)
                estimated = True
            else:
                estimated = False

            baseline = (in_t * SONNET_IN + out_t * SONNET_OUT) / 1_000_000
            saved    = max(0.0, baseline - (r["cost_usd"] or 0.0))
            result.append({
                "provider":  r["provider"],
                "calls":     calls,
                "in_tok":    in_t,
                "out_tok":   out_t,
                "cost_usd":  r["cost_usd"] or 0.0,
                "baseline":  baseline,
                "saved":     saved,
                "estimated": estimated,
            })
        return result
    except Exception:
        return []


def _run_status() -> None:
    import json
    import os
    import sqlite3
    import time
    from datetime import datetime, timezone, timedelta

    state_dir  = os.path.expanduser("~/.llm-router")
    usage_json = os.path.join(state_dir, "usage.json")
    db_path    = os.path.join(state_dir, "usage.db")
    WIDTH      = 62

    print("\n" + "─" * WIDTH)
    print(f"  {_bold('llm-router status')}")
    print("─" * WIDTH)

    # ── Subscription pressure ──────────────────────────────────────────
    pressure_data: dict = {}
    try:
        with open(usage_json) as f:
            pressure_data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    print(f"\n  {_bold('Claude Code subscription')}", end="")
    if pressure_data:
        age_s = time.time() - pressure_data.get("updated_at", 0)
        age_label = f"{int(age_s / 60)}m ago" if age_s < 3600 else "stale"
        print(f"  ({age_label})")

        def _bar(pct: float, label: str) -> None:
            filled = max(0, min(20, round(pct / 5)))
            bar = _green("█" * filled) + "░" * (20 - filled)
            color = _green if pct < 70 else (_yellow if pct < 90 else _red)
            print(f"    {label:<16} {bar}  {color(f'{pct:.1f}%')}")

        _bar(pressure_data.get("session_pct", 0.0), "session (5h)")
        _bar(pressure_data.get("weekly_pct",  0.0), "weekly")
        if pressure_data.get("sonnet_pct", 0.0) > 0:
            _bar(pressure_data["sonnet_pct"], "weekly sonnet")
    else:
        print()
        print(_warn("    no data — run: llm_check_usage"))

    # ── Savings summary ────────────────────────────────────────────────
    print(f"\n  {_bold('Routing savings')}")

    if not os.path.exists(db_path):
        print("    no data yet — route some tasks first\n")
    else:
        now = datetime.now(timezone.utc)
        periods = [
            ("today",    (now.replace(hour=0, minute=0, second=0, microsecond=0)
                          .strftime("%Y-%m-%d %H:%M:%S"))),
            ("7 days",   ((now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S"))),
            ("30 days",  ((now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S"))),
            ("all time", "1970-01-01 00:00:00"),
        ]

        any_data = False
        for label, since_iso in periods:
            calls, cost, baseline = _query_routing_period(db_path, since_iso)
            if calls == 0:
                continue
            any_data = True
            saved = max(0.0, baseline - cost)
            pct   = 100 * saved / baseline if baseline > 0 else 0.0
            bar   = _savings_bar(saved, cost)
            print(f"    {_bold(label):<20}  {_green(f'${saved:.3f}')} saved  "
                  f"({_green(f'{pct:.0f}%')} cheaper)")
            print(f"  {bar}  {calls} calls")

        if not any_data:
            print("    no external routing yet — route some tasks first")

        # Top models used
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT model, COUNT(*) as n, COALESCE(SUM(cost_usd),0) as c "
                "FROM usage WHERE success=1 AND provider!='subscription' "
                "GROUP BY model ORDER BY n DESC LIMIT 4"
            ).fetchall()
            conn.close()
            if rows:
                print(f"\n  {_bold('Top models used')}")
                for r in rows:
                    short = r["model"].split("/")[-1] if "/" in r["model"] else r["model"]
                    short = short[:30] + "…" if len(short) > 30 else short
                    print(f"    {short:<32}  {r['n']:>4}×  ${r['c']:.4f}")
        except Exception:
            pass

        # Free model savings (Ollama + Codex)
        free_rows = _query_free_model_savings(db_path)
        if free_rows:
            total_free_saved = sum(r["saved"] for r in free_rows)
            total_free_calls = sum(r["calls"] for r in free_rows)
            print(f"\n  {_bold('Free-model savings')}  "
                  f"{_dim('(Ollama / Codex — $0 API cost)')}")
            for r in free_rows:
                est_tag = _dim(" ~est") if r["estimated"] else ""
                in_k  = f"{r['in_tok'] // 1000}k"  if r["in_tok"]  >= 1000 else str(r["in_tok"])
                out_k = f"{r['out_tok'] // 1000}k" if r["out_tok"] >= 1000 else str(r["out_tok"])
                tok_str = f"{in_k}↑ {out_k}↓{est_tag}"
                saved_str = f"${r['saved']:.4f}"
                print(f"    {r['provider']:<10}  {r['calls']:>4} calls  "
                      f"{tok_str:<14}  {_green(saved_str)} saved")
            bar = _savings_bar(total_free_saved, 0.0)
            print(f"  {bar}  {total_free_calls} free calls  "
                  f"{_green(f'${total_free_saved:.4f}')} total saved vs Sonnet")

    print(f"\n  {_bold('Subcommands')}")
    print(f"    {_bold('llm-router update')}     — update hooks to latest version")
    print(f"    {_bold('llm-router doctor')}     — full health check")
    print(f"    {_bold('llm-router dashboard')}  — web dashboard (localhost:7337)")
    print("─" * WIDTH + "\n")


# ── update ─────────────────────────────────────────────────────────────────────

def _run_update() -> None:
    """Re-install hooks + rules, check for newer PyPI version."""
    import importlib.metadata
    import urllib.request
    import json

    from llm_router.install_hooks import (
        install,
    )

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


# ── share ──────────────────────────────────────────────────────────────────────

def _run_share() -> None:
    """Generate a shareable savings card and open a one-click tweet."""
    import os
    import sqlite3
    import urllib.parse

    state_dir = os.path.expanduser("~/.llm-router")
    db_path   = os.path.join(state_dir, "usage.db")

    SONNET_IN, SONNET_OUT = 3.0, 15.0
    FREE_PROVIDERS = {"ollama", "codex"}

    # ── Query all-time stats ──────────────────────────────────────────
    total_calls = paid_calls = free_calls = 0
    total_saved = 0.0
    top_model   = "—"

    if os.path.exists(db_path):
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row

            rows = conn.execute(
                "SELECT provider, input_tokens, output_tokens, cost_usd FROM usage "
                "WHERE success=1"
            ).fetchall()
            for r in rows:
                prov   = r["provider"] or ""
                in_tok = r["input_tokens"]  or 0
                out_tok = r["output_tokens"] or 0
                cost    = r["cost_usd"]      or 0.0
                total_calls += 1
                base = (in_tok * SONNET_IN + out_tok * SONNET_OUT) / 1_000_000
                if prov in FREE_PROVIDERS:
                    free_calls  += 1
                    total_saved += base
                elif prov != "subscription":
                    paid_calls  += 1
                    total_saved += max(0.0, base - cost)

            # Top model by call count (paid only)
            top_row = conn.execute(
                "SELECT model, COUNT(*) as n FROM usage "
                "WHERE success=1 AND provider NOT IN ('subscription','ollama','codex') "
                "GROUP BY model ORDER BY n DESC LIMIT 1"
            ).fetchone()
            if top_row:
                m = top_row["model"]
                top_model = m.split("/")[-1] if "/" in m else m
                if len(top_model) > 24:
                    top_model = top_model[:22] + "…"

            conn.close()
        except Exception:
            pass

    savings_pct = 0
    if total_saved > 0 and (paid_calls + free_calls) > 0:
        # rough pct: saved / (saved + actual_cost)
        try:
            conn2 = sqlite3.connect(db_path)
            actual = conn2.execute(
                "SELECT COALESCE(SUM(cost_usd),0) FROM usage WHERE success=1 "
                "AND provider NOT IN ('subscription','ollama','codex')"
            ).fetchone()[0] or 0.0
            conn2.close()
            total_baseline = total_saved + actual
            savings_pct = round(total_saved / total_baseline * 100) if total_baseline > 0 else 0
        except Exception:
            pass

    # ── Build the card ────────────────────────────────────────────────
    WIDTH = 54
    def _box_line(text: str) -> str:
        pad = WIDTH - 2 - len(text)
        return f"│ {text}{' ' * max(0, pad)} │"

    border = "─" * WIDTH
    card_lines = [
        f"┌{border}┐",
        _box_line(""),
        _box_line(f"  🤖 llm-router saved me ${total_saved:.2f} (lifetime)"),
        _box_line(f"     {savings_pct}% cheaper than always-Sonnet"),
        _box_line(""),
        _box_line(f"  {total_calls:,} total calls tracked"),
        _box_line(f"  {free_calls:,} free  (Ollama / Codex)  ·  {paid_calls:,} paid API"),
        _box_line(f"  Top model: {top_model}"),
        _box_line(""),
        _box_line("  ⭐ github.com/ypollak2/llm-router"),
        _box_line(""),
        f"└{border}┘",
    ]

    print()
    for line in card_lines:
        print(f"  {line}")
    print()

    # ── Copy plain text to clipboard ─────────────────────────────────
    plain = (
        f"🤖 llm-router saved me ${total_saved:.2f} (lifetime)\n"
        f"{savings_pct}% cheaper than always-Sonnet\n\n"
        f"{total_calls:,} calls tracked  ·  {free_calls:,} free (Ollama/Codex)  ·  {paid_calls:,} paid API\n"
        f"Top model: {top_model}\n\n"
        f"⭐ github.com/ypollak2/llm-router"
    )
    _copy_to_clipboard(plain)

    # ── Twitter/X intent URL ──────────────────────────────────────────
    tweet = (
        f"🤖 llm-router saved me ${total_saved:.2f} so far "
        f"({savings_pct}% cheaper than always-Sonnet)\n\n"
        f"{free_calls} free calls (Ollama/Codex) · {paid_calls} paid API calls\n\n"
        f"Open-source MCP router for Claude Code 👇\n"
        f"github.com/ypollak2/llm-router ⭐"
    )
    tweet_url = "https://twitter.com/intent/tweet?text=" + urllib.parse.quote(tweet)

    print(f"  {_green('✓')}  Card copied to clipboard")
    print(f"  {_yellow('→')}  Tweet it: {_dim(tweet_url[:72] + '…')}")
    print()

    import webbrowser
    try:
        webbrowser.open(tweet_url)
        print(f"  {_dim('(opened in browser)')}")
    except Exception:
        pass
    print()


def _copy_to_clipboard(text: str) -> None:
    """Copy *text* to the system clipboard. Silent on failure."""
    import subprocess
    import sys as _sys
    try:
        if _sys.platform == "darwin":
            subprocess.run(["pbcopy"], input=text.encode(), check=True)
        elif _sys.platform == "win32":
            subprocess.run(["clip"], input=text.encode("utf-16"), check=True)
        else:
            # Linux: try xclip then xsel
            for cmd in (["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]):
                try:
                    subprocess.run(cmd, input=text.encode(), check=True)
                    break
                except FileNotFoundError:
                    continue
    except Exception:
        pass


# ── dashboard ──────────────────────────────────────────────────────────────────

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


# ── test (route simulator) ────────────────────────────────────────────────────

def _run_test(prompt: str) -> None:
    """Dry-run route simulation: classify prompt + show model choice + cost estimate."""
    import asyncio

    if not prompt:
        print(_red("Usage: llm-router test \"<your prompt>\""))
        sys.exit(1)

    async def _simulate() -> None:
        from llm_router.classifier import classify_complexity
        from llm_router.types import MODEL_COST_PER_1K

        # Baseline: claude-sonnet-4-5 (what a non-routing user would pay)
        BASELINE = "claude-sonnet-4-5"
        BASELINE_IN = 3.0   # $/M tokens
        BASELINE_OUT = 15.0

        # Estimate token counts from prompt length (rough: 1 token ≈ 4 chars)
        est_in = max(50, len(prompt) // 4)
        est_out = 300  # typical completion

        result = await classify_complexity(prompt)
        complexity = result.complexity.value
        confidence = result.confidence
        task = result.inferred_task_type.value if result.inferred_task_type else "unknown"
        method = result.classifier_model or "heuristic"

        # Map complexity → cheapest appropriate model (mirrors router.py logic)
        complexity_model_map = {
            "simple": "claude-haiku-4-5-20251001",
            "moderate": "claude-sonnet-4-6",
            "complex": "claude-opus-4-6",
            "deep_reasoning": "claude-opus-4-6",
        }
        chosen = complexity_model_map.get(complexity, BASELINE)

        # Cost estimate for chosen model
        costs = MODEL_COST_PER_1K.get(chosen, {})
        in_rate = costs.get("input", 0.0) * 1000   # convert /1k to /M
        out_rate = costs.get("output", 0.0) * 1000
        chosen_cost = (est_in * in_rate + est_out * out_rate) / 1_000_000
        baseline_cost = (est_in * BASELINE_IN + est_out * BASELINE_OUT) / 1_000_000
        saved = max(0.0, baseline_cost - chosen_cost)
        savings_pct = round(saved / baseline_cost * 100) if baseline_cost > 0 else 0

        W = 56
        HR = "+" + "-" * W + "+"
        def row(text: str) -> str:
            return f"| {text:<{W - 1}}|"
        def section(title: str) -> str:
            return "|" + f" {title} ".center(W, "-") + "|"

        print()
        print(HR)
        print("|" + " Route Simulation (dry run) ".center(W) + "|")
        print(HR)
        print(row(f"  Prompt:      {prompt[:42]}{'…' if len(prompt) > 42 else ''}"))
        print(row(""))
        print(section("CLASSIFICATION"))
        print(row(f"  Task type:   {task}"))
        print(row(f"  Complexity:  {complexity}"))
        print(row(f"  Confidence:  {confidence:.0%}  (via {method})"))
        print(HR)
        print(section("ROUTING DECISION"))
        print(row(f"  Chosen:      {_green(chosen)}"))
        print(row(f"  Baseline:    {BASELINE}"))
        print(HR)
        print(section("COST ESTIMATE  (~{est_in}t in / {est_out}t out)".format(
            est_in=est_in, est_out=est_out)))
        print(row(f"  Chosen cost: ${chosen_cost:.5f}"))
        print(row(f"  Baseline:    ${baseline_cost:.5f}"))
        if saved > 0:
            print(row(f"  Saved:       {_green(f'${saved:.5f}  ({savings_pct}% cheaper)')}"))
        else:
            print(row("  Saved:       —"))
        print(HR)
        print()

    asyncio.run(_simulate())


# ── config ────────────────────────────────────────────────────────────────────

def _run_config(flags: list[str]) -> None:
    """llm-router config [lint|show|init]."""
    sub = flags[0] if flags else "show"

    from llm_router.repo_config import (
        effective_config, find_repo_config_path, fingerprint_repo,
    )
    from pathlib import Path

    if sub == "init":
        _run_config_init()
        return

    # ── show / lint ───────────────────────────────────────────────────────────
    merged   = effective_config()
    repo_type, suggested = fingerprint_repo()

    HR = "─" * 60

    print(f"\n{_bold('llm-router config')}\n")
    print(HR)

    # Sources
    user_path = Path.home() / ".llm-router" / "routing.yaml"
    repo_path = find_repo_config_path()
    print(f"  {_bold('User config:')}  {user_path}  {'✓' if user_path.exists() else _dim('(not found)')}")
    print(f"  {_bold('Repo config:')}  {repo_path or _dim('(none — no .llm-router.yml in tree)')}")
    print(f"  {_bold('Repo type:')}    {repo_type}  →  suggested profile: {_yellow(suggested)}")
    print()
    print(HR)

    # Effective settings
    enforce  = merged.effective_enforce()
    profile  = merged.effective_profile() or "balanced  (default)"
    print(f"  {_bold('Effective profile:')}  {_green(profile)}")
    print(f"  {_bold('Enforce mode:')}       {_yellow(enforce)}")

    if merged.block_providers:
        print(f"  {_bold('Blocked providers:')}  {', '.join(merged.block_providers)}")

    if merged.daily_caps:
        print(f"  {_bold('Daily caps:')}")
        for k, v in merged.daily_caps.items():
            label = "total" if k == "_total" else k
            print(f"    {label:<12}  ${v:.2f}")

    if merged.routing:
        print(f"  {_bold('Routing pins:')}")
        for task, override in merged.routing.items():
            parts = []
            if override.model:
                parts.append(f"model={override.model}")
            if override.provider:
                parts.append(f"provider={override.provider}")
            print(f"    {task:<12}  {', '.join(parts)}")

    print()
    print(HR)

    # Lint warnings
    warnings: list[str] = []
    if not user_path.exists() and repo_path is None:
        warnings.append("No config files found — using defaults. Run `llm-router config init` to create one.")
    if merged.effective_enforce() == "shadow":
        warnings.append("Enforce mode is 'shadow' — routing is observed only, not enforced. Switch to 'enforce' for maximum savings.")

    if sub == "lint":
        # Validate YAML schema if files exist
        import yaml
        for label, path in [("user", user_path), ("repo", repo_path)]:
            if path and Path(path).exists():
                try:
                    yaml.safe_load(Path(path).read_text())
                    print(_ok(f"{label} config YAML is valid  ({path})"))
                except yaml.YAMLError as e:
                    print(_fail(f"{label} config has YAML errors", fix=str(e)))
                    warnings.append(f"{label} config YAML is invalid — fix before routing is affected")

    if warnings:
        print()
        for w in warnings:
            print(_warn(w))
    elif sub == "lint":
        print(_ok("Config looks good"))

    print()


def _run_config_init() -> None:
    """Create a starter .llm-router.yml in the current directory."""
    from llm_router.repo_config import fingerprint_repo
    path = ".llm-router.yml"
    if os.path.exists(path):
        print(_warn(f"{path} already exists — not overwriting. Edit it directly."))
        return
    repo_type, suggested = fingerprint_repo()
    template = f"""\
# .llm-router.yml — repo-level routing config
# Docs: https://github.com/ypollak2/llm-router
version: 1

# Routing profile: budget | balanced | premium
profile: {suggested}

# Enforcement mode: shadow (observe) | suggest (hints) | enforce (block violations)
enforce: enforce

# Block specific providers (comment out to allow all)
# block_providers:
#   - openai

# Pin specific task types to a model or provider
# routing:
#   code:
#     provider: ollama        # prefer local for code tasks
#   research:
#     provider: perplexity   # always use web-grounded search

# Per-task daily spend caps (USD)
# daily_caps:
#   image: 2.00
#   _total: 5.00
"""
    with open(path, "w") as f:
        f.write(template)
    print(_ok(f"Created {path}  (repo type: {repo_type}, suggested profile: {suggested})"))
    print(f"  Edit it, then run {_bold('llm-router config lint')} to validate.\n")


# ── set-enforce ───────────────────────────────────────────────────────────────

_ENFORCE_MODES = ("smart", "soft", "hard", "off")

_ENFORCE_DESCRIPTIONS = {
    "smart": "Hard block for Q&A tasks (query/research/generate/analyze), soft for code. >80% routing compliance without blocking file editing.",
    "soft":  "Route hints in context, never blocks. Lowest friction — routing is suggested but not enforced.",
    "hard":  "All Bash/Edit/Write blocked until an llm_* tool is called. Maximum cost savings, highest friction.",
    "off":   "Enforcement disabled. Routing hints appear but nothing is enforced.",
}


def _run_set_enforce(mode: str) -> None:
    """Switch the enforcement mode and persist to ~/.llm-router/routing.yaml."""
    import re
    from pathlib import Path

    if not mode or mode not in _ENFORCE_MODES:
        print(f"\n{_bold('Usage:')} llm-router set-enforce <mode>\n")
        print("Available modes:\n")
        for m in _ENFORCE_MODES:
            marker = " (default)" if m == "smart" else ""
            print(f"  {_bold(m):<12}{marker}")
            print(f"  {_dim(_ENFORCE_DESCRIPTIONS[m])}")
            print()
        return

    routing_yaml = Path.home() / ".llm-router" / "routing.yaml"
    routing_yaml.parent.mkdir(parents=True, exist_ok=True)

    if routing_yaml.exists():
        content = routing_yaml.read_text()
        # Update existing enforce line or add it
        if re.search(r"^enforce:", content, re.MULTILINE):
            content = re.sub(r"^enforce:.*$", f"enforce: {mode}", content, flags=re.MULTILINE)
        else:
            content = f"enforce: {mode}\n" + content
    else:
        content = f"enforce: {mode}\n"

    routing_yaml.write_text(content)

    # Also write to .env for hooks that read it
    env_path = Path.home() / ".llm-router" / ".env"
    if env_path.exists():
        env_content = env_path.read_text()
        if "LLM_ROUTER_ENFORCE=" in env_content:
            env_content = re.sub(
                r"LLM_ROUTER_ENFORCE=\S*", f"LLM_ROUTER_ENFORCE={mode}", env_content
            )
        else:
            env_content += f"\nLLM_ROUTER_ENFORCE={mode}\n"
        env_path.write_text(env_content)
    else:
        env_path.write_text(f"LLM_ROUTER_ENFORCE={mode}\n")

    print(f"\n{_green('✓')} Enforcement mode set to {_bold(mode)}")
    print(f"  {_dim(_ENFORCE_DESCRIPTIONS[mode])}")
    print(f"\n  Written to: {routing_yaml}")
    print(f"  Written to: {env_path}")
    print(f"\n  {_dim('Restart Claude Code for the change to take effect.')}\n")


# ── team ──────────────────────────────────────────────────────────────────────

def _run_team(subcmd: str, flags: list[str]) -> None:
    """llm-router team report|push|setup [period]."""
    import asyncio
    from llm_router.team import (
        build_team_report, detect_channel, get_project_id, get_user_id, push_report,
    )
    from llm_router.config import get_config

    config = get_config()
    period = flags[0] if flags else "week"

    if subcmd == "setup":
        _run_team_setup(config)
        return

    user_id = get_user_id(override=config.llm_router_user_id)
    project_id = get_project_id()

    print(f"\n{_bold('[llm-router] Team Report')}\n")
    print(f"  User:    {user_id}")
    print(f"  Project: {project_id}")
    print(f"  Period:  {period}\n")

    report = asyncio.run(build_team_report(user_id=user_id, project_id=project_id, period=period))

    calls = report["total_calls"]
    if calls == 0:
        print(_yellow("  No routing data found for this period."))
        print(f"  Try: {_bold('llm-router team report all')}\n")
        return

    saved = report["saved_usd"]
    actual = report["actual_usd"]
    free_pct = report["free_pct"]

    filled = round(free_pct * 10)
    bar = "█" * filled + "░" * (10 - filled)

    print(f"  Calls:     {_bold(str(calls))}")
    print(f"  Saved:     {_green(f'~${saved:.4f}')}  (paid ${actual:.4f})")
    print(f"  Free tier: {free_pct:.0%}  {bar}")

    top = report.get("top_models", [])
    if top:
        print(f"\n  {'Model':<28} {'Calls':>5}  {'Cost':>8}")
        print(f"  {'-'*28} {'-'*5}  {'-'*8}")
        for m in top[:8]:
            short = m["model"].split("/")[-1][:26] if "/" in m["model"] else m["model"][:26]
            free_tag = " (free)" if m["cost"] == 0 else ""
            print(f"  {short:<28} {m['calls']:>5}  ${m['cost']:>7.4f}{free_tag}")

    endpoint = config.llm_router_team_endpoint
    if subcmd == "push":
        if not endpoint:
            print(f"\n{_red('✗')} No endpoint configured.")
            print(f"  Run {_bold('llm-router team setup')} to configure Slack/Discord/Telegram.\n")
            return
        channel = detect_channel(endpoint)
        print(f"\n  Pushing to {_bold(channel)}...")
        success, msg = asyncio.run(push_report(
            report, endpoint, telegram_chat_id=config.llm_router_team_chat_id,
        ))
        if success:
            print(_green(f"  {msg}"))
        else:
            print(_red(f"  {msg}"))
    elif endpoint:
        channel = detect_channel(endpoint)
        print(f"\n  {_dim(f'Endpoint: {channel} configured — run llm-router team push to send.')}")
    else:
        print(f"\n  {_dim('Tip: run llm-router team setup to configure Slack/Discord/Telegram push.')}")
    print()


def _run_team_setup(config) -> None:
    """Interactive wizard to configure the team notification endpoint."""
    import re as _re
    from pathlib import Path

    print(f"\n{_bold('[llm-router] Team Notification Setup')}\n")
    print("Choose a notification channel:\n")
    print("  1. Slack    (paste your Incoming Webhook URL)")
    print("  2. Discord  (paste your Webhook URL)")
    print("  3. Telegram (paste your Bot API URL + enter chat ID)")
    print("  4. Generic  (paste any HTTP POST endpoint URL)")
    print("  5. Skip / disable\n")

    choice = input("Enter choice [1-5]: ").strip()
    if choice == "5" or not choice:
        print(_dim("  Setup skipped.\n"))
        return

    url = input("  Paste endpoint URL: ").strip()
    if not url:
        print(_red("  No URL entered — setup cancelled.\n"))
        return

    chat_id = ""
    if choice == "3" or "telegram" in url.lower():
        chat_id = input("  Telegram chat_id (e.g. -1001234567890): ").strip()

    # Write to routing.yaml
    routing_yaml = Path.home() / ".llm-router" / "routing.yaml"
    routing_yaml.parent.mkdir(parents=True, exist_ok=True)
    content = routing_yaml.read_text() if routing_yaml.exists() else ""

    def _set_or_add(key: str, value: str) -> None:
        nonlocal content
        if _re.search(rf"^{key}:", content, _re.MULTILINE):
            content = _re.sub(rf"^{key}:.*$", f"{key}: {value}", content, flags=_re.MULTILINE)
        else:
            content += f"\n{key}: {value}"

    _set_or_add("team_endpoint", url)
    if chat_id:
        _set_or_add("team_chat_id", chat_id)

    routing_yaml.write_text(content.strip() + "\n")

    # Also write to .env for immediate effect
    env_path = Path.home() / ".llm-router" / ".env"
    env_content = env_path.read_text() if env_path.exists() else ""
    for key, val in [("LLM_ROUTER_TEAM_ENDPOINT", url), ("LLM_ROUTER_TEAM_CHAT_ID", chat_id)]:
        if not val:
            continue
        if f"{key}=" in env_content:
            env_content = _re.sub(rf"{key}=\S*", f"{key}={val}", env_content)
        else:
            env_content += f"\n{key}={val}\n"
    env_path.write_text(env_content)

    from llm_router.team import detect_channel
    channel = detect_channel(url)
    print(f"\n{_green('✓')} Team endpoint configured: {_bold(channel)}")
    print(f"  Run {_bold('llm-router team push')} to send your first report.\n")


# ── onboard ───────────────────────────────────────────────────────────────────

def _run_onboard() -> None:
    """Zero-friction onboarding: detect capabilities, pick enforcement mode, write config, install."""
    import shutil
    import subprocess as sp

    STATE_DIR = os.path.expanduser("~/.llm-router")

    print(f"\n{_bold('╔══════════════════════════════════════════╗')}")
    print(f"{_bold('║   LLM Router — Onboarding Wizard          ║')}")
    print(f"{_bold('╚══════════════════════════════════════════╝')}\n")
    print("  Detecting your setup...\n")

    # ── 1. Detect Ollama ──────────────────────────────────────────────────────
    ollama_ok = False
    try:
        r = sp.run(["curl", "-sf", "http://localhost:11434/api/tags"],
                   capture_output=True, timeout=3)
        ollama_ok = r.returncode == 0
    except Exception:
        pass
    if ollama_ok:
        print(_ok("Ollama running  (free local tier, ~1–3s)"))
    else:
        print(_warn("Ollama not detected  — install from ollama.ai for free routing"))

    # ── 2. Detect Codex ───────────────────────────────────────────────────────
    codex_ok = shutil.which("codex") is not None
    if codex_ok:
        print(_ok("Codex CLI available  (free via OpenAI subscription)"))
    else:
        print(_warn("Codex CLI not found  — install from github.com/openai/codex"))

    # ── 3. Detect API keys ────────────────────────────────────────────────────
    key_vars = ["OPENAI_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY",
                "PERPLEXITY_API_KEY", "ANTHROPIC_API_KEY"]
    found_keys = [k for k in key_vars if os.getenv(k)]
    if found_keys:
        print(_ok(f"API keys:  {', '.join(found_keys)}"))
    else:
        print(_warn("No API keys found  — free-only routing (Ollama + Codex)"))

    # ── 4. Recommend profile ──────────────────────────────────────────────────
    print()
    if ollama_ok or codex_ok:
        profile = "budget"
        print(f"  {_bold('Recommended profile:')} {_green('budget')}  (free-first via Ollama/Codex)")
    elif found_keys:
        profile = "balanced"
        print(f"  {_bold('Recommended profile:')} {_yellow('balanced')}  (paid API routing)")
    else:
        print(f"\n  {_red('✗  No routing backends found!')}")
        print("     Install Ollama (ollama.ai) or set an API key to enable routing.\n")
        return

    # ── 5. Choose enforcement mode ────────────────────────────────────────────
    print(f"\n  {_bold('Enforcement mode:')}")
    print(f"    {_dim('[1] shadow')}   — observe routing decisions, no enforcement  {_dim('(safe start)')}")
    print(f"    {_dim('[2] suggest')}  — show routing hints, allow overrides")
    print(f"    {_dim('[3] enforce')}  — block Claude when routing is violated  {_dim('(maximum savings)')}")
    print()
    try:
        choice = input("  Choose [1/2/3, default=1]: ").strip() or "1"
    except (EOFError, KeyboardInterrupt):
        choice = "1"

    enforce = {"1": "shadow", "2": "suggest", "3": "hard"}.get(choice, "shadow")
    mode_label = {"shadow": _dim("shadow  (observation only)"),
                  "suggest": _yellow("suggest  (hints, no blocking)"),
                  "hard": _green("enforce  (maximum savings)")}.get(enforce, enforce)

    # ── 6. Write config to ~/.llm-router/.env ────────────────────────────────
    env_path = os.path.join(STATE_DIR, ".env")
    os.makedirs(STATE_DIR, exist_ok=True)
    env_lines = [
        f"LLM_ROUTER_ENFORCE={enforce}",
        f"LLM_ROUTER_PROFILE={profile}",
    ]
    try:
        # Merge with any existing .env (preserve user keys)
        existing: dict[str, str] = {}
        if os.path.exists(env_path):
            for line in open(env_path).read().splitlines():
                if "=" in line and not line.strip().startswith("#"):
                    k, _, v = line.partition("=")
                    existing[k.strip()] = v.strip()
        for line in env_lines:
            k, _, v = line.partition("=")
            existing[k.strip()] = v.strip()
        with open(env_path, "w") as fh:
            for k, v in existing.items():
                fh.write(f"{k}={v}\n")
        print(_ok(f"Config written to {env_path}"))
    except OSError as e:
        print(_fail(f"Failed to write config: {e}"))
        return

    # ── 7. Install hooks ──────────────────────────────────────────────────────
    print()
    from llm_router.install_hooks import install
    for action in install(force=True):
        print(f"  {_green('✓')}  {action}")

    # ── 8. Summary ────────────────────────────────────────────────────────────
    print(f"\n{_green('✓')} {_bold('Onboarding complete!')}")
    print(f"  Mode:    {mode_label}")
    print(f"  Profile: {_bold(profile)}")
    print()
    print("  Next steps:")
    print("    • Start a new Claude Code session to activate routing")
    print("    • Run `llm-router status` to see savings accumulate")
    if enforce == "shadow":
        print("    • Upgrade to suggest/enforce when ready: llm-router onboard")
    print()


if __name__ == "__main__":
    main()
