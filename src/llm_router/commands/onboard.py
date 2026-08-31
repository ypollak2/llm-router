"""Onboard command — zero-friction setup wizard."""

from __future__ import annotations

import os
import shutil
import subprocess as sp
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

def cmd_onboard(args: list[str]) -> int:
    """Entry point for onboard command."""
    _run_onboard()
    return 0


# ── Implementation ─────────────────────────────────────────────────────────────

def _run_onboard() -> None:
    """Zero-friction onboarding: detect capabilities, pick enforcement mode, write config, install."""
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
