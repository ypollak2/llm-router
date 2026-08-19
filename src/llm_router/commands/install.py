"""Install command — global hook and MCP server installation."""

from __future__ import annotations

import os
import shutil
import sys
from llm_router.tool_surface import route_tool  # CHZ-SURF-01: never print a raw tool name


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

_INSTALL_HELP = """\
llm_router install — install LLM Router routing into a host

Usage:
  llm_router install                     Install for Claude Code (hooks + rules + MCP)
  llm_router install --host <name>       Install/print config for a specific host
                                     (claude-code, claude-desktop, cursor, copilot,
                                     windsurf, gemini-cli, codex, …)
  llm_router install --mode <mode>       Install mode (auto | gateway)
  llm_router install --help, -h          Show this help and exit (no changes made)
"""


def cmd_install(args: list[str]) -> int:
    """Entry point for install command."""
    # CHZ-PKG-007: `--help`/`-h` must be inert — previously it fell straight
    # through to a real install (file modifications) instead of printing usage.
    if any(a in ("--help", "-h", "help") for a in args):
        print(_INSTALL_HELP)
        return 0
    _run_install(args)
    return 0


# ── Main install logic ──────────────────────────────────────────────────────────

# README headline uses `--host claude-code` / `claude-desktop`, neither of
# which is a distinct snippet: claude-code IS the default LLM Router install
# target, and claude-desktop maps onto the existing `desktop` snippet.
_HOST_ALIASES = {"claude-desktop": "desktop", "claude_desktop": "desktop"}


def _run_install(flags: list[str]) -> None:
    # --host <name> is handled before the regular install path — it prints
    # config snippets only (no file modifications to external tools).
    if "--host" in flags:
        idx = flags.index("--host")
        raw_host = (flags[idx + 1] if idx + 1 < len(flags) else "all").strip().lower()
        mode = "gateway" if raw_host == "codex" else "auto"
        if "--mode" in flags:
            mode_idx = flags.index("--mode")
            mode = (flags[mode_idx + 1] if mode_idx + 1 < len(flags) else mode).strip().lower()
        host = _HOST_ALIASES.get(raw_host, raw_host)
        # claude-code = the default install (hooks + rules + MCP for Claude
        # Code), so route it through the full install path below rather than
        # the snippet printer — this makes the documented headline command work.
        if host != "claude-code":
            _install_host(host, mode=mode)
            return
        flags = [f for i, f in enumerate(flags) if i not in (idx, idx + 1)]

    check_only = "--check" in flags
    force = "--force" in flags

    from llm_router.install_hooks import (
        _HOOKS_DST, _HOOKS_SRC, _HOOK_DEFS,
        _RULES_DST, check_api_keys, claude_desktop_config_path,
        install,
    )

    if check_only:
        print(f"\n{_bold('[llm_router] Install check')}  (no changes made)\n")

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
                    fix="llm_router install",
                ))
                all_ok = False

        rules_dst = _RULES_DST / "llm_router.md"
        if rules_dst.exists():
            print(_ok("llm_router.md  (routing rules)"))
        else:
            print(_fail("llm_router.md  (routing rules)  — not installed",
                        fix="llm_router install"))
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
                    registered = "llm_router" in cfg.get("mcpServers", {})
                except Exception:
                    pass
            if registered:
                print(_ok(f"registered  ({desktop_path})"))
            elif desktop_path.exists():
                print(_fail(
                    f"not registered  ({desktop_path})",
                    fix="llm_router install",
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
            print(_yellow("  Run `llm_router install` to fix the issues above."))
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
        settings.get("mcpServers", {}).pop("llm_router", None)
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
    print("    llm_router doctor               — verify everything is wired up")
    print("    llm_router status               — today's cost & savings")
    print("    llm_router dashboard            — web dashboard (localhost:7337)")
    print("    llm_router install --check      — preview install state")
    print("    llm_router install --force      — reinstall / update paths")
    print("    llm_router install --claw-code  — also install into claw-code")
    print("    llm_router uninstall            — remove\n")


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

  # Install llm_router and wire in hooks
  RUN pip install llm-routing && llm_router install

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
    print(_dim("  (llm_router install does this automatically; shown for reference)"))
    import json as _json
    example = {
        "mcpServers": {"llm_router": {"command": "llm_router", "args": []}},
        "hooks": {
            "UserPromptSubmit": [{"matcher": "", "hooks": [{"type": "command", "command": "python3 ~/.claude/hooks/llm_router-auto-route.py"}]}],
            "Stop":             [{"matcher": "", "hooks": [{"type": "command", "command": "python3 ~/.claude/hooks/llm_router-session-end.py"}]}],
        }
    }
    print("  " + _dim(_json.dumps(example, indent=2).replace("\n", "\n  ")))

    print(f"\n{_bold('  Verification (run inside the container after a job):')}")
    print("    grep ROUTE /proc/1/fd/1            # look for routing log lines")
    print("    cat ~/.llm-router/usage.json        # routing stats")
    print("    sqlite3 ~/.llm-router/usage.db \\")
    print('      "SELECT model, COUNT(*) FROM usage GROUP BY model"\n')


# ── install --host (print config snippets for non-Claude Code hosts) ──────────

# CHZ-SURF-01: snippets are .format() templates, so tool names are placeholders
# resolved against the live tier rather than baked in as literals.
_SNIPPET_TOOLS = {
    "savings_tool": route_tool("llm_savings"),
    "research_tool": route_tool("llm_research"),
    "query_tool": route_tool("llm_query"),
}

_HOST_SNIPPETS: dict[str, str] = {
    "codex": """\
{bold}Codex CLI{reset}  (capability extension — no cost-routing)
──────────────────────────────────────────────────────────────────
1. Add to ~/.codex/config.yaml:

   mcp:
     servers:
       llm_router:
         command: llm_router
         args: []

2. Copy routing rules so Codex knows when to call llm_auto:

   cp "$(python3 -c "import llm_router; import pathlib; print(pathlib.Path(llm_router.__file__).parent / 'rules' / 'codex-rules.md')")" \\
      ~/.codex/instructions.md

3. Restart Codex — run {savings_tool} to verify the DB is shared.
""",

    "desktop": """\
{bold}Claude Desktop{reset}  (capability extension — no cost-routing)
──────────────────────────────────────────────────────────────────
Edit ~/Library/Application Support/Claude/claude_desktop_config.json
(Linux: ~/.config/Claude/claude_desktop_config.json)
(Windows: %APPDATA%\\Claude\\claude_desktop_config.json)

Add inside the top-level object:

  "mcpServers": {{
    "llm_router": {{
      "command": "llm_router",
      "args": [],
      "env": {{
        "LLM_ROUTER_PROFILE": "balanced"
      }}
    }}
  }}

Restart Claude Desktop. Run {savings_tool} to confirm DB is shared.
Note: cost-routing is not available in Desktop (no hook system).
""",

    "copilot": """\
{bold}GitHub Copilot (VS Code){reset}  (capability extension — no cost-routing)
──────────────────────────────────────────────────────────────────
1. Create or edit .vscode/mcp.json in your workspace:

   {{
     "servers": {{
       "llm_router": {{
         "command": "llm_router",
         "args": []
       }}
     }}
   }}

2. Optionally add routing guidance to .github/copilot-instructions.md:

   When a task requires live web search, call the {research_tool} MCP tool.
   When a task requires image generation, call the llm_image MCP tool.
   For auto-routing with savings tracking, call llm_auto.

3. Enable MCP in VS Code settings (Copilot > MCP: Enable).
   Restart VS Code. Run @llm_router {savings_tool} to verify.
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

    "windsurf": """\
{bold}Windsurf (MCP native){reset}  — writing config files…
""",
}


# ── Host-specific install functions ────────────────────────────────────────────

def _replace_or_insert_toml_scalar(text: str, key: str, value: str) -> str:
    """Replace a top-level TOML scalar, or insert it before the first table."""
    import re

    line = f'{key} = "{value}"'
    pattern = re.compile(rf"^{re.escape(key)}\s*=.*$", re.MULTILINE)
    if pattern.search(text):
        return pattern.sub(line, text, count=1)
    table_match = re.search(r"^\[", text, flags=re.MULTILINE)
    if table_match:
        return text[:table_match.start()] + line + "\n" + text[table_match.start():]
    return (text.rstrip() + "\n" + line + "\n") if text.strip() else line + "\n"


def _ensure_toml_table_block(text: str, header: str, block: str) -> str:
    """Append a TOML table block if the table is not already present."""
    if f"[{header}]" in text:
        return text
    sep = "" if text.endswith("\n") or not text else "\n"
    return f"{text}{sep}\n[{header}]\n{block.rstrip()}\n"


def _remove_toml_scalar_if_equals(text: str, key: str, value: str) -> str:
    """Remove a top-level TOML scalar line, but only if it currently equals
    `value` — used to self-heal a setting a previous llm_router install forced,
    without touching a value the user (or another tool) set independently."""
    import re

    pattern = re.compile(
        rf'^{re.escape(key)}\s*=\s*"{re.escape(value)}"\s*\n?', re.MULTILINE
    )
    return pattern.sub("", text, count=1)


def _install_codex_gateway_config(codex_dir) -> list[str]:
    """Register LLM Router as an available Codex model provider (opt-in).

    Earlier versions of this installer also force-set `model = "auto"` and
    `model_provider = "llm_router"` as Codex's GLOBAL defaults, silently looping
    every Codex call — including Codex's own interactive/agentic use, and
    LLM Router's own dispatch of Codex from its routing chain — through the local
    LLM Router gateway. The gateway does not yet speak the exact OpenAI
    "responses" wire shape Codex's client expects, so every such call failed
    with an undecodable-stream error. Concretely: this broke Codex CLI itself
    for any user who ran `llm_router install --host codex`, independent of
    whether they ever touched LLM Router's own routing.
    We now only REGISTER the provider (so it exists for future opt-in use,
    e.g. `codex -c model_provider=llm_router` once wire compatibility lands) and
    never force it as the default. If an earlier llm_router install already
    forced it, this run reverts those two lines so Codex falls back to its
    own built-in default — self-healing existing broken installs the next
    time this function runs (e.g. via `llm_router install --host codex --mode
    gateway` or `llm_router doctor --fix`).
    """
    import pathlib
    import re
    import shutil as _shutil

    from llm_router import presets

    actions: list[str] = []
    config_toml = pathlib.Path(codex_dir) / "config.toml"
    config_toml.parent.mkdir(parents=True, exist_ok=True)
    original = config_toml.read_text() if config_toml.exists() else ""
    updated = original

    # Self-heal: undo a previous install's forced global default, if present.
    had_forced_default = (
        re.search(r'^model_provider\s*=\s*"llm_router"\s*$', updated, re.MULTILINE) is not None
    )
    updated = _remove_toml_scalar_if_equals(updated, "model", "auto")
    updated = _remove_toml_scalar_if_equals(updated, "model_provider", "llm_router")

    gateway = presets.gateway_url()
    updated = _ensure_toml_table_block(
        updated,
        "model_providers.llm_router",
        "\n".join([
            'name = "LLM Router"',
            f'base_url = "{gateway}"',
            'env_key = "LLM_ROUTER_API_KEY"',
            'wire_api = "responses"',
        ]),
    )

    if updated != original:
        try:
            if config_toml.exists() and not (config_toml.parent / "config.toml.llm_router-bak").exists():
                _shutil.copy2(config_toml, config_toml.parent / "config.toml.llm_router-bak")
                actions.append(f"✓ Backed up Codex config to {config_toml.parent / 'config.toml.llm_router-bak'}")
            config_toml.write_text(updated)
            try:
                from llm_router import install_manifest
                install_manifest.record("toml_table", config_toml, header="model_providers.llm_router")
            except Exception:
                pass
        except OSError as e:
            actions.append(f"  Could not update Codex gateway config at {config_toml}: {e}")
            return actions
        if had_forced_default:
            actions.append(
                f"✓ Reverted Codex's default model_provider (was force-set to 'llm_router' by an "
                f"earlier install, which broke Codex CLI — see {config_toml})"
            )
        actions.append(f"✓ Registered LLM Router as an available Codex model provider in {config_toml}")
        actions.append(
            f"  Not set as default (gateway doesn't yet support Codex's wire format) — "
            f"opt in per-call with -c model_provider=llm_router ({gateway})"
        )
    else:
        actions.append(f"  Codex gateway provider already configured in {config_toml} (skipped)")
    return actions


def _install_codex_files(mode: str = "gateway") -> list[str]:
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
        "    llm_router:\n"
        "      command: llm_router\n"
        "      args: []\n"
    )
    existing = config_yaml.read_text() if config_yaml.exists() else ""
    if "llm_router" not in existing:
        with config_yaml.open("a") as f:
            f.write(mcp_block)
        actions.append(f"✓ Added llm_router MCP server to {config_yaml}")
        try:
            from llm_router import install_manifest
            install_manifest.record("text_block", config_yaml, block=mcp_block)
        except Exception:
            pass
    else:
        actions.append(f"  llm_router already in {config_yaml} (skipped)")

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
        try:
            from llm_router import install_manifest
            install_manifest.record("file", hook_script)
        except Exception:
            pass

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
            if "llm_router" not in existing_inst:
                block = f"\n\n{rules_text}"
                with instructions.open("a") as f:
                    f.write(block)
                actions.append(f"✓ Appended routing rules to {instructions}")
                try:
                    from llm_router import install_manifest
                    install_manifest.record("text_block", instructions, block=block)
                except Exception:
                    pass
            else:
                actions.append(f"  Routing rules already in {instructions} (skipped)")
        else:
            instructions.write_text(rules_text)
            actions.append(f"✓ Created {instructions} with routing rules")
            try:
                from llm_router import install_manifest
                install_manifest.record("created_file", instructions, block=rules_text)
            except Exception:
                pass

    if mode == "gateway":
        actions += _install_codex_gateway_config(codex_dir)
    elif mode not in {"companion", "mcp", "mcp-only", "rules", "auto"}:
        actions.append(f"  Unknown Codex mode {mode!r}; installed MCP companion only")

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
        actions.append(f"✓ Added llm_router MCP server to {config_path}")
        # RED2-9-*: record so uninstall reverses it automatically.
        try:
            from llm_router import install_manifest
            install_manifest.record("json_mcp", config_path, root_key=root_key, server=server_name)
        except Exception:
            pass
    else:
        actions.append(f"  llm_router already in {config_path} (skipped)")
    return actions


def _remove_json_mcp_block(
    config_path, server_name: str = "llm_router", root_key: str = "mcpServers"
) -> list[str]:
    """RED2-8-01: inverse of _merge_json_mcp_block — surgically remove the llm_router
    MCP entry from a host's JSON config, leaving every other server intact."""
    import json as _json
    import pathlib

    actions: list[str] = []
    config_path = pathlib.Path(config_path)
    if not config_path.exists():
        return actions
    try:
        existing = _json.loads(config_path.read_text())
    except Exception:
        return actions
    servers = existing.get(root_key)
    if isinstance(servers, dict) and server_name in servers:
        del servers[server_name]
        try:
            config_path.write_text(_json.dumps(existing, indent=2))
            actions.append(f"✓ Removed llm_router MCP server from {config_path}")
        except OSError as e:
            actions.append(f"  Could not update {config_path}: {e}")
    return actions


def _remove_toml_table_block(text: str, header: str) -> str:
    """Remove a `[header]` TOML table and its body (until the next table / EOF).

    RED1-9-02: the body must consume only lines that do NOT begin a new [table].
    The prior regex used a `(?!\\n\\[)` lookahead that, once the newline was
    consumed, no longer guarded the next line — so adjacent tables NOT separated
    by a blank line (valid TOML) were swallowed through EOF, destroying unrelated
    provider config. The corrected body anchors each line at ^ (MULTILINE) and
    stops at the first line starting with '['.
    """
    import re
    pattern = re.compile(
        rf'(?m)^\[{re.escape(header)}\][^\n]*\n(?:(?!\[).*(?:\n|$))*'
    )
    return pattern.sub("", text, count=1)


def uninstall_host_integrations() -> list[str]:
    """RED2-8-01: remove the live MCP registrations that `llm_router install --host
    <codex|cursor|gemini-cli|vscode|copilot-cli|openclaw|trae>` wrote, so a
    `llm_router uninstall` does not leave dangling `llm_router` entries that break those
    tools after `pip uninstall`. Each remover is home-scoped and defensive — a
    no-op when the host was never installed, and never aborts on one host's error.

    NOTE: this is the enumerated LEGACY fallback for the home-directory MCP
    registrations. New installs are covered authoritatively by the install
    manifest (install_manifest.apply_uninstall), which also handles the
    project-scoped writers (.github/copilot-instructions.md, Trae .rules) via
    their recorded created/appended entries. uninstall_ide_configs() removes the
    project MCP/rule files it knows (.vscode/mcp.json, .windsurf/mcp.json,
    .cursor/rules/use-llm_router.mdc) against the cwd.
    """
    import pathlib
    import shutil as _shutil
    import sys

    actions: list[str] = []
    home = pathlib.Path.home()

    # JSON MCP registrations (root_key varies by host).
    json_targets = [
        (home / ".gemini" / "settings.json", "mcpServers"),
        (home / ".cursor" / "mcp.json", "mcpServers"),
        (home / ".config" / "gh" / "copilot" / "mcp.json", "mcpServers"),
        (home / ".openclaw" / "mcp.json", "mcpServers"),
        (home / ".config" / "opencode" / "config.json", "mcpServers"),  # RED2-9-01
    ]
    if sys.platform == "darwin":
        json_targets.append((home / "Library" / "Application Support" / "Code" / "User" / "mcp.json", "servers"))
        json_targets.append((home / "Library" / "Application Support" / "Trae" / "mcp.json", "mcpServers"))
    elif sys.platform == "win32":
        appdata = pathlib.Path(home / "AppData" / "Roaming")
        json_targets.append((appdata / "Code" / "User" / "mcp.json", "servers"))
        json_targets.append((appdata / "Trae" / "mcp.json", "mcpServers"))
    else:
        json_targets.append((home / ".config" / "Code" / "User" / "mcp.json", "servers"))
        json_targets.append((home / ".config" / "Trae" / "mcp.json", "mcpServers"))

    for path, root_key in json_targets:
        try:
            actions += _remove_json_mcp_block(path, "llm_router", root_key)
        except Exception as e:  # noqa: BLE001 — one host must never abort the rest
            actions.append(f"  host cleanup skipped for {path}: {e}")

    # Gemini CLI extension directory (whole llm_router extension).
    try:
        ext_dir = home / ".gemini" / "extensions" / "llm_router"
        if ext_dir.exists():
            _shutil.rmtree(ext_dir, ignore_errors=True)
            actions.append(f"✓ Removed Gemini CLI extension dir {ext_dir}")
    except Exception as e:  # noqa: BLE001
        actions.append(f"  gemini extension cleanup skipped: {e}")

    # Cursor routing-rules file llm_router authored.
    try:
        cursor_rules = home / ".cursor" / "rules" / "llm_router.md"
        if cursor_rules.exists():
            cursor_rules.unlink()
            actions.append(f"✓ Removed {cursor_rules}")
    except Exception as e:  # noqa: BLE001
        actions.append(f"  cursor rules cleanup skipped: {e}")

    # Codex: remove the gateway TOML table and the appended config.yaml MCP block.
    try:
        codex_dir = home / ".codex"
        config_toml = codex_dir / "config.toml"
        if config_toml.exists():
            original = config_toml.read_text()
            updated = _remove_toml_table_block(original, "model_providers.llm_router")
            if updated != original:
                # RED2-10-05: no persistent .llm_router-bak (uninstall leaves nothing
                # llm_router-authored); the removal regex is ^-anchored + tested.
                config_toml.write_text(updated)
                actions.append(f"✓ Removed [model_providers.llm_router] from {config_toml}")
        config_yaml = codex_dir / "config.yaml"
        if config_yaml.exists():
            y = config_yaml.read_text()
            mcp_block = (
                "\nmcp:\n"
                "  servers:\n"
                "    llm_router:\n"
                "      command: llm_router\n"
                "      args: []\n"
            )
            if mcp_block in y:
                config_yaml.write_text(y.replace(mcp_block, ""))
                actions.append(f"✓ Removed llm_router MCP block from {config_yaml}")
        # RED2-9-02: strip the LIVE llm_router PostToolUse entry from ~/.codex/hooks.json
        # (it invokes codex-post-tool.py on every Bash call — a phantom hook after
        # uninstall). Filter out any PostToolUse entry that references llm_router's hook.
        import json as _json
        hooks_json = codex_dir / "hooks.json"
        if hooks_json.exists():
            try:
                cur = _json.loads(hooks_json.read_text())
                ptu = cur.get("hooks", {}).get("PostToolUse", [])
                kept = [
                    entry for entry in ptu
                    if not any(
                        "codex-post-tool.py" in str(h.get("command", "")) or ".llm_router/hooks" in str(h.get("command", ""))
                        for h in entry.get("hooks", [])
                    )
                ]
                if len(kept) != len(ptu):
                    cur.setdefault("hooks", {})["PostToolUse"] = kept
                    hooks_json.write_text(_json.dumps(cur, indent=2))
                    actions.append(f"✓ Removed llm_router PostToolUse hook from {hooks_json}")
            except (OSError, ValueError):
                pass
    except Exception as e:  # noqa: BLE001
        actions.append(f"  codex cleanup skipped: {e}")

    return actions


def _append_routing_rules(dest_path, rules_filename: str) -> list[str]:
    """Append routing rules from src/rules/ to dest_path. Returns action strings.

    RED1-20 (P0): tool names are LOCALIZED against the active surface before the
    file is written. They were not, and this is the function serving the ten
    NON-Claude hosts — Cursor, Windsurf, Copilot, Gemini CLI, opencode, Trae and
    the rest. The Claude Code path had already been fixed (CHZ-SURF-01, via
    `_localized_rules_text`); every other host still got the bundle verbatim, so
    a rules file naming `llm_code` taught those models to make a call that 404s,
    in every session, for the life of the file.

    That asymmetry is the finding, not a detail of it. Vendor-neutrality is the
    North Star's central claim and the vendor-neutral path was the unlocalized one.

    This is now the ONLY copy. `cli.py` held a second implementation that also
    lacked the resolver AND never recorded to the install manifest, so the rules
    files it wrote survived `llm_router uninstall`. Two implementations of one
    installer is the RED8-06 duplicate-source class, and is precisely why one got
    fixed and the other did not.
    """
    import pathlib

    from llm_router.install_hooks import _localized_rules_text

    actions: list[str] = []
    dest_path = pathlib.Path(dest_path)
    rules_src = pathlib.Path(__file__).parent.parent / "rules" / rules_filename
    if not rules_src.exists():
        return actions
    rules_text = _localized_rules_text(rules_src)
    if dest_path.exists():
        existing = dest_path.read_text()
        if "llm_router" not in existing:
            block = f"\n\n{rules_text}"
            with dest_path.open("a") as f:
                f.write(block)
            actions.append(f"✓ Appended routing rules to {dest_path}")
            try:
                from llm_router import install_manifest
                install_manifest.record("text_block", dest_path, block=block)
            except Exception:
                pass
        else:
            actions.append(f"  Routing rules already in {dest_path} (skipped)")
    else:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_text(rules_text)
        actions.append(f"✓ Created {dest_path} with routing rules")
        try:
            from llm_router import install_manifest
            # RED1-10-02: record the exact text so uninstall strips only llm_router's
            # content and preserves anything the user appended later.
            install_manifest.record("created_file", dest_path, block=rules_text)
        except Exception:
            pass
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
        try:
            from llm_router import install_manifest
            install_manifest.record("file", dest)
        except Exception:
            pass
    return str(dest), actions


def _install_opencode_files() -> list[str]:
    """Write OpenCode-specific config files and return a list of actions taken."""
    import pathlib

    actions: list[str] = []
    home = pathlib.Path.home()
    opencode_dir = home / ".config" / "opencode"
    opencode_dir.mkdir(parents=True, exist_ok=True)

    # 1. MCP server entry
    server_entry = {"command": "llm_router", "args": []}
    actions += _merge_json_mcp_block(opencode_dir / "config.json", "llm_router", server_entry)

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
    server_entry = {"command": "llm_router", "args": []}
    actions += _merge_json_mcp_block(gemini_dir / "settings.json", "llm_router", server_entry)

    # 2. Extension manifest + hooks directory
    ext_dir = gemini_dir / "extensions" / "llm_router"
    _ext_existed = ext_dir.exists()
    ext_dir.mkdir(parents=True, exist_ok=True)
    if not _ext_existed:
        try:
            from llm_router import install_manifest
            install_manifest.record("dir", ext_dir)
        except Exception:
            pass

    ext_manifest = ext_dir / "gemini-extension.json"
    if not ext_manifest.exists():
        manifest = {
            "name": "llm_router",
            "version": "9.0.1",
            "description": "Route tasks to cheapest capable model — 20+ providers",
            "mcpServers": {"llm_router": server_entry},
        }
        ext_manifest.write_text(_json.dumps(manifest, indent=2))
        actions.append(f"✓ Created Gemini CLI extension manifest at {ext_manifest}")
    else:
        actions.append(f"  Extension manifest already exists at {ext_manifest} (skipped)")

    # 3. Hook scripts
    post_hook_dest, hook_actions = _copy_hook_script(
        "gemini-cli-post-tool.py", home / ".llm-router" / "hooks"
    )
    actions += hook_actions

    auto_hook_dest, hook_actions = _copy_hook_script(
        "gemini-cli-auto-route.py", home / ".llm-router" / "hooks"
    )
    actions += hook_actions

    status_hook_dest, hook_actions = _copy_hook_script(
        "status-bar.py", home / ".llm-router" / "hooks"
    )
    actions += hook_actions

    end_hook_dest, hook_actions = _copy_hook_script(
        "gemini-cli-session-end.py", home / ".llm-router" / "hooks"
    )
    actions += hook_actions

    start_hook_dest, hook_actions = _copy_hook_script(
        "session-start.py", home / ".llm-router" / "hooks"
    )
    actions += hook_actions

    # 4. Extension hooks.json
    hooks_dir = ext_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hooks_json = hooks_dir / "hooks.json"
    hook_entry = {
        "hooks": {
            "SessionStart": [
                {"matcher": "*", "command": start_hook_dest}
            ],
            "PostToolUse": [
                {"matcher": "*", "command": post_hook_dest}
            ],
            "UserPromptSubmit": [
                {"matcher": "*", "command": auto_hook_dest},
                {"matcher": "*", "command": status_hook_dest}
            ],
            "SessionEnd": [
                {"matcher": "*", "command": end_hook_dest}
            ]
        }
    }
    if not hooks_json.exists():
        hooks_json.write_text(_json.dumps(hook_entry, indent=2))
        actions.append(f"✓ Created Gemini CLI hooks.json at {hooks_json}")
    else:
        # Merge if missing
        try:
            current = _json.loads(hooks_json.read_text())
            hooks = current.setdefault("hooks", {})
            
            # SessionStart
            start_hooks = hooks.setdefault("SessionStart", [])
            if not any(start_hook_dest in str(h) for h in start_hooks):
                start_hooks.append({"matcher": "*", "command": start_hook_dest})
                actions.append("✓ Added SessionStart hook to Gemini CLI")
            
            # PostToolUse
            post_hooks = hooks.setdefault("PostToolUse", [])
            if not any(post_hook_dest in str(h) for h in post_hooks):
                post_hooks.append({"matcher": "*", "command": post_hook_dest})
                actions.append("✓ Added PostToolUse hook to Gemini CLI")
            
            # UserPromptSubmit
            auto_hooks = hooks.setdefault("UserPromptSubmit", [])
            if not any(auto_hook_dest in str(h) for h in auto_hooks):
                auto_hooks.append({"matcher": "*", "command": auto_hook_dest})
                actions.append("✓ Added UserPromptSubmit hook to Gemini CLI")
            
            if not any(status_hook_dest in str(h) for h in auto_hooks):
                auto_hooks.append({"matcher": "*", "command": status_hook_dest})
                actions.append("✓ Added status-bar hook to Gemini CLI")

            # SessionEnd
            end_hooks = hooks.setdefault("SessionEnd", [])
            if not any(end_hook_dest in str(h) for h in end_hooks):
                end_hooks.append({"matcher": "*", "command": end_hook_dest})
                actions.append("✓ Added SessionEnd hook to Gemini CLI")
            
            hooks_json.write_text(_json.dumps(current, indent=2))
        except Exception as e:
            actions.append(f"  Could not update {hooks_json}: {e}")

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

    server_entry = {"command": "llm_router", "args": []}
    actions += _merge_json_mcp_block(copilot_dir / "mcp.json", "llm_router", server_entry)

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

    server_entry = {"command": "llm_router", "args": []}
    actions += _merge_json_mcp_block(openclaw_dir / "mcp.json", "llm_router", server_entry)
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

    server_entry = {"command": "llm_router", "args": []}
    actions += _merge_json_mcp_block(trae_dir / "mcp.json", "llm_router", server_entry)

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
        actions.append("  Install via: factory plugin install ypollak2/llm_router")
    else:
        actions.append("  .factory-plugin/ not found in repo root — run from the llm_router repo dir")
    actions.append("  Or install .claude-plugin/ directly: factory plugin install ypollak2/llm_router")
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

    server_entry = {"command": "llm_router", "args": []}
    actions += _merge_json_mcp_block(mcp_json, "llm_router", server_entry, root_key="servers")

    # Append routing guidance to .github/copilot-instructions.md in cwd (if it exists)
    copilot_instructions = pathlib.Path.cwd() / ".github" / "copilot-instructions.md"
    actions += _append_routing_rules(copilot_instructions, "vscode-rules.md")

    return actions


def _install_windsurf_files() -> list[str]:
    """Write Windsurf MCP config (project-scoped .windsurf/mcp.json). RED2-10-03:
    windsurf is documented in README/--help but had no installer, so
    `llm_router install --host windsurf` was rejected as an unknown host."""
    import pathlib

    actions: list[str] = []
    mcp_json = pathlib.Path.cwd() / ".windsurf" / "mcp.json"
    server_entry = {"command": "llm_router", "args": []}
    actions += _merge_json_mcp_block(mcp_json, "llm_router", server_entry, root_key="mcpServers")
    return actions


def _install_cursor_files() -> list[str]:
    """Write Cursor IDE MCP config and routing rules. Returns list of actions taken."""
    import pathlib

    actions: list[str] = []
    home = pathlib.Path.home()

    # Global Cursor MCP config (applies across all projects)
    mcp_json = home / ".cursor" / "mcp.json"
    server_entry = {"command": "llm_router", "args": []}
    actions += _merge_json_mcp_block(mcp_json, "llm_router", server_entry, root_key="mcpServers")

    # Append routing rules to ~/.cursor/rules/llm_router.md
    cursor_rules = home / ".cursor" / "rules" / "llm_router.md"
    actions += _append_routing_rules(cursor_rules, "cursor-rules.md")

    return actions


def _install_host(host: str, mode: str = "auto") -> None:
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
    print(f"\n{bold}llm_router install --host {host}{reset}\n")
    print("─" * min(w, 70))

    # Hosts that write files; all others print snippets
    _FILE_WRITERS = {
        "codex":      (lambda: _install_codex_files(mode="gateway" if mode == "auto" else mode),
                       "Restart Codex and run `llm_router doctor --host codex` to verify automatic routing."),
        "opencode":   (_install_opencode_files,     f"Restart OpenCode and run {route_tool('llm_savings')} to verify."),
        "gemini-cli": (_install_gemini_cli_files,   f"Restart Gemini CLI and run {route_tool('llm_savings')} to verify."),
        "copilot-cli":(_install_copilot_cli_files,  f"Restart Copilot CLI and run {route_tool('llm_savings')} to verify."),
        "openclaw":   (_install_openclaw_files,     f"Restart OpenClaw and run {route_tool('llm_savings')} to verify."),
        "trae":       (_install_trae_files,         f"Restart Trae IDE and run {route_tool('llm_savings')} to verify."),
        "factory":    (_install_factory_files,      "Run: factory plugin install ypollak2/llm_router"),
        "vscode":     (_install_vscode_files,       "Restart VS Code and enable MCP in Copilot settings."),
        "cursor":     (_install_cursor_files,       f"Restart Cursor and run {route_tool('llm_savings')} to verify."),
        "windsurf":   (_install_windsurf_files,     f"Restart Windsurf and run {route_tool('llm_savings')} to verify."),
    }

    for h in hosts_to_show:
        if h in _FILE_WRITERS:
            install_fn, verify_hint = _FILE_WRITERS[h]
            label = _HOST_SNIPPETS[h].format(bold=bold, reset=reset, **_SNIPPET_TOOLS).strip()
            print(f"{label}\n")
            actions = install_fn()
            for action in actions:
                print(f"  {action}")
            print()
            print(f"  {verify_hint}")
        else:
            snippet = _HOST_SNIPPETS[h].format(bold=bold, reset=reset, **_SNIPPET_TOOLS)
            print(snippet)
        print("─" * min(w, 70))

    print(
        f"\nFor Claude Code (hooks + full cost-routing): {bold}llm_router install{reset}\n"
        f"See docs/hosts/ for setup guides and trade-off explanations.\n"
    )
