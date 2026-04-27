"""Config command — display and manage routing configuration."""

from __future__ import annotations

import os
import sys
from pathlib import Path


# ── Formatting utilities ────────────────────────────────────────────────────

def _color_enabled() -> bool:
    """Check if color output is enabled."""
    return sys.stdout.isatty() and not os.getenv("NO_COLOR")


def _bold(s: str) -> str:
    """Bold text."""
    return f"\033[1m{s}\033[0m" if _color_enabled() else s


def _yellow(s: str) -> str:
    """Yellow text."""
    return f"\033[33m{s}\033[0m" if _color_enabled() else s


def _green(s: str) -> str:
    """Green text."""
    return f"\033[32m{s}\033[0m" if _color_enabled() else s


def _dim(s: str) -> str:
    """Dim text."""
    return f"\033[2m{s}\033[0m" if _color_enabled() else s


def _ok(label: str) -> str:
    """Formatted success message."""
    return f"  {_green('✓')}  {label}"


def _warn(label: str) -> str:
    """Formatted warning message."""
    return f"  {_yellow('⚠')}  {label}"


def _fail(label: str, fix: str | None = None) -> str:
    """Formatted failure message."""
    msg = f"  {_bold('✗')}  {label}"
    if fix:
        msg += f"\n       {_yellow('→')} {fix}"
    return msg


# ── Config init subcommand ────────────────────────────────────────────────

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


# ── Config show/lint subcommands ───────────────────────────────────────────

def _run_config(flags: list[str]) -> None:
    """Display or validate routing configuration.
    
    Subcommands:
    - show: Display current effective configuration
    - lint: Validate configuration files for errors
    - init: Create a new .llm-router.yml template
    """
    sub = flags[0] if flags else "show"

    from llm_router.repo_config import (
        effective_config,
        find_repo_config_path,
        fingerprint_repo,
    )

    if sub == "init":
        _run_config_init()
        return

    # ── show / lint ───────────────────────────────────────────────────────────
    merged = effective_config()
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
    enforce = merged.effective_enforce()
    profile = merged.effective_profile() or "balanced  (default)"
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
        warnings.append(
            "No config files found — using defaults. Run `llm-router config init` to create one."
        )
    if merged.effective_enforce() == "shadow":
        warnings.append(
            "Enforce mode is 'shadow' — routing is observed only, not enforced. Switch to 'enforce' for maximum savings."
        )

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
                    warnings.append(
                        f"{label} config YAML is invalid — fix before routing is affected"
                    )

    if warnings:
        print()
        for w in warnings:
            print(_warn(w))
    elif sub == "lint":
        print(_ok("Config looks good"))

    print()


# ── Main command entry point ───────────────────────────────────────────────

def cmd_config(args: list[str]) -> int:
    """Execute: llm-router config [show|lint|init]
    
    Manage and display routing configuration.
    """
    _run_config(args)
    return 0
