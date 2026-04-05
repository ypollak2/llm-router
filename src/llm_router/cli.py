"""CLI entry point for llm-router.

Usage:
    llm-router                  — start the MCP server (stdio transport)
    llm-router install          — install hooks, rules, and MCP server config globally
    llm-router install --check  — show what would be installed without doing it
    llm-router install --force  — reinstall even if already present
    llm-router uninstall        — remove hooks and MCP registration
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
        _run_uninstall()
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
    print("    llm-router doctor          — verify everything is wired up")
    print("    llm-router status          — today's cost & savings")
    print("    llm-router dashboard       — web dashboard (localhost:7337)")
    print("    llm-router install --check — preview install state")
    print("    llm-router install --force — reinstall / update paths")
    print("    llm-router uninstall       — remove\n")


# ── uninstall ──────────────────────────────────────────────────────────────────

def _run_uninstall() -> None:
    from llm_router.install_hooks import uninstall

    print(f"\n{_bold('Uninstalling LLM Router...')}\n")
    actions = uninstall()
    for a in actions:
        print(f"  {a}")
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

    # ── 8. Version ────────────────────────────────────────────────────────────
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

def _run_demo() -> None:
    """Show routing decisions for a set of sample prompts."""

    # Detect active config
    cc_mode = os.getenv("LLM_ROUTER_CLAUDE_SUBSCRIPTION", "").lower() in ("true", "1", "yes")
    has_perplexity = bool(os.getenv("PERPLEXITY_API_KEY"))
    has_openai = bool(os.getenv("OPENAI_API_KEY"))
    has_gemini = bool(os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"))

    # (prompt, task_type, complexity, model_if_cc, model_if_api, cost_estimate)
    DEMO_CASES = [
        ('"what does os.path.join do?"',         "query",    "simple",     "Claude Haiku",    "Gemini Flash",  "$0.00001"),
        ('"why is my async code slow?"',          "analyze",  "moderate",   "Claude Sonnet",   "GPT-4o",        "$0.003"),
        ('"implement a Redis-backed rate limiter"',"code",    "complex",    "Claude Opus",     "o3",            "$0.015"),
        ('"prove the halting problem is undecidable"',"analyze","deep_reason","Opus + thinking","o3",           "$0.03"),
        ('"research latest Gemini 2.5 benchmarks"',"research","moderate",   "Perplexity" if has_perplexity else "Claude Sonnet","Perplexity","$0.002"),
        ('"write a hero section for a SaaS landing"',"generate","moderate", "Claude Sonnet",   "Claude Haiku",  "$0.001"),
        ('"generate a dashboard screenshot mockup"',"image",  "—",          "Flux Pro",        "Flux Pro",      "$0.04"),
    ]

    col_w = [44, 8, 12, 18, 9]
    sep = "─" * (sum(col_w) + len(col_w) * 2 + 2)

    print(f"\n{_bold('llm-router demo')}  — how smart routing handles your prompts\n")

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
        config_parts.append("no external APIs — subscription mode assumed")
    print(f"  Active config: {', '.join(config_parts)}\n")

    # Header
    print(f"  {'Prompt':<{col_w[0]}}  {'Task':<{col_w[1]}}  {'Complexity':<{col_w[2]}}  {'Model':<{col_w[3]}}  {'~Cost'}")
    print("  " + sep)

    total_routed = 0.0
    total_opus   = 0.0
    for prompt, task, complexity, model_cc, model_api, cost_str in DEMO_CASES:
        model = model_cc if cc_mode else model_api

        # Colorize complexity
        if complexity == "simple":
            compl_label = _green(complexity)
        elif complexity == "moderate":
            compl_label = _yellow(complexity)
        elif complexity in ("complex", "deep_reason"):
            compl_label = _red(complexity)
        else:
            compl_label = complexity

        # Colorize cost
        try:
            cost_val = float(cost_str.lstrip("$"))
            cost_label = _green(cost_str) if cost_val < 0.002 else (_yellow(cost_str) if cost_val < 0.01 else _red(cost_str))
        except ValueError:
            cost_label = cost_str

        prompt_disp = prompt if len(prompt) <= col_w[0] else prompt[:col_w[0] - 1] + "…"
        row = (
            f"  {_pad(prompt_disp, col_w[0])}"
            f"  {_pad(task, col_w[1])}"
            f"  {_pad(compl_label, col_w[2])}"
            f"  {_pad(model, col_w[3])}"
            f"  {cost_label}"
        )
        print(row)

        # Track savings estimate (vs always-Opus at $0.015)
        try:
            total_routed += float(cost_str.lstrip("$"))
            total_opus   += 0.015
        except ValueError:
            pass

    print("  " + sep)

    savings_pct = 100 * (1 - total_routed / total_opus) if total_opus > 0 else 0
    savings_str = f"${total_opus:.3f} → ${total_routed:.3f}  ({savings_pct:.0f}% cheaper)"
    print(f"\n  {_bold('Savings vs always-Opus:')}  {_green(savings_str)}")
    print(f"\n  {_yellow('→')} Run {_bold('llm-router setup')} to configure API keys for more providers.")
    print(f"  {_yellow('→')} Run {_bold('llm-router dashboard')} to see live routing decisions.\n")


# ── status ─────────────────────────────────────────────────────────────────────

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
    print("    llm-router doctor      — full health check")
    print("    llm-router install     — install hooks globally")
    print("    llm-router dashboard   — web dashboard (localhost:7337)")
    print("─" * WIDTH + "\n")


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
