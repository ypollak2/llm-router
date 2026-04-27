"""Install command — global hook and MCP server installation."""

from __future__ import annotations

import os
import shutil
import sys


# ── ANSI helpers (respect NO_COLOR / non-tty) ─────────────────────────────────

def _color_enabled() -> bool:
    return sys.stdout.isatty() and not os.getenv("NO_COLOR")


def _bold(s: str) -> str:
    return f"\033[1m{s}\033[0m" if _color_enabled() else s


def _green(s: str) -> str:
    return f"\033[32m{s}\033[0m" if _color_enabled() else s


def _yellow(s: str) -> str:
    return f"\033[33m{s}\033[0m" if _color_enabled() else s


def _red(s: str) -> str:
    return f"\033[31m{s}\033[0m" if _color_enabled() else s


def _dim(s: str) -> str:
    return f"\033[2m{s}\033[0m" if _color_enabled() else s


def _ok(label: str) -> str:
    return f"  {_green('✓')}  {label}"


def _warn(label: str) -> str:
    return f"  {_yellow('⚠')}  {label}"


def _fail(label: str, fix: str | None = None) -> str:
    line = f"  {_red('✗')}  {label}"
    if fix:
        line += f"\n       {_yellow('→')} {fix}"
    return line


# ── Command entry point ────────────────────────────────────────────────────────

def cmd_install(args: list[str]) -> int:
    """Entry point for install command."""
    _run_install(args)
    return 0


# ── Main install logic ──────────────────────────────────────────────────────────

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

    "vscode": """\
{bold}VS Code (MCP native){reset}  — writing config files…
""",

    "cursor": """\
{bold}Cursor IDE{reset}  — writing config files…
""",
}


# ── Host-specific install functions ────────────────────────────────────────────

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
    src_hook = pathlib.Path(__file__).parent.parent / "hooks" / "codex-post-tool.py"
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
    rules_src = pathlib.Path(__file__).parent.parent / "rules" / "codex-rules.md"
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


def _merge_json_mcp_block(
    config_path, server_name: str, server_entry: dict, root_key: str = "mcpServers"
) -> list[str]:
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

    servers = existing.setdefault(root_key, {})
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
    rules_src = pathlib.Path(__file__).parent.parent / "rules" / rules_filename
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
    src = pathlib.Path(__file__).parent.parent / "hooks" / hook_filename
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


def _install_vscode_files() -> list[str]:
    """Write VS Code MCP config and routing rules. Returns list of actions taken."""
    import pathlib
    import sys

    actions: list[str] = []
    home = pathlib.Path.home()

    # Platform-specific user mcp.json location
    if sys.platform == "darwin":
        mcp_json = home / "Library" / "Application Support" / "Code" / "User" / "mcp.json"
    elif sys.platform == "win32":
        appdata = pathlib.Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
        mcp_json = appdata / "Code" / "User" / "mcp.json"
    else:
        mcp_json = home / ".config" / "Code" / "User" / "mcp.json"

    server_entry = {"command": "uvx", "args": ["claude-code-llm-router"]}
    actions += _merge_json_mcp_block(mcp_json, "llm-router", server_entry, root_key="servers")

    # Append routing guidance to .github/copilot-instructions.md in cwd (if it exists)
    copilot_instructions = pathlib.Path.cwd() / ".github" / "copilot-instructions.md"
    actions += _append_routing_rules(copilot_instructions, "vscode-rules.md")

    return actions


def _install_cursor_files() -> list[str]:
    """Write Cursor IDE MCP config and routing rules. Returns list of actions taken."""
    import pathlib

    actions: list[str] = []
    home = pathlib.Path.home()

    # Global Cursor MCP config (applies across all projects)
    mcp_json = home / ".cursor" / "mcp.json"
    server_entry = {"command": "uvx", "args": ["claude-code-llm-router"]}
    actions += _merge_json_mcp_block(mcp_json, "llm-router", server_entry, root_key="mcpServers")

    # Append routing rules to ~/.cursor/rules/llm-router.md
    cursor_rules = home / ".cursor" / "rules" / "llm-router.md"
    actions += _append_routing_rules(cursor_rules, "cursor-rules.md")

    return actions


def _install_host(host: str) -> None:
    """Install config for non-Claude Code hosts (writes files for Codex; prints snippets for others)."""
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
        "vscode":     (_install_vscode_files,       "Restart VS Code and enable MCP in Copilot settings."),
        "cursor":     (_install_cursor_files,       "Restart Cursor and run llm_savings to verify."),
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
