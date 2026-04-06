"""CLI entry point for llm-router.

Usage:
    llm-router                  — start the MCP server (stdio transport)
    llm-router install              — install hooks, rules, and MCP server config globally
    llm-router install --check      — show what would be installed without doing it
    llm-router install --force      — reinstall even if already present
    llm-router install --claw-code  — also install into claw-code (auto-detects ~/.claw-code/)
    llm-router install --headless   — install for Docker/agent/CI environments (API-key mode, no OAuth)
    llm-router uninstall        — remove hooks and MCP registration
    llm-router uninstall --purge — also delete ~/.llm-router/ (usage DB, .env, logs)
    llm-router setup            — interactive wizard: configure providers and API keys
    llm-router status           — show routing status, today's savings, subscription pressure
    llm-router doctor           — check that everything is wired up correctly
    llm-router demo             — show routing decisions for sample prompts
    llm-router dashboard        — start the web dashboard at localhost:7337
    llm-router dashboard --port 7338  — use a custom port
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
    else:
        # Default: start the MCP server (original behavior)
        from llm_router.server import main as _mcp_main
        _mcp_main()


# ── install ────────────────────────────────────────────────────────────────────

def _run_install(flags: list[str]) -> None:
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


if __name__ == "__main__":
    main()
