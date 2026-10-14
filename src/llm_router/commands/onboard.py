"""Onboard command — zero-friction setup wizard."""

from __future__ import annotations

import os
import shutil
import sys
from typing import Callable

from llm_router import paths


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

def _run_onboard(
    input_fn: Callable[[str], str] = input,
    runner: Callable[[str], int] | None = None,
    detect: Callable[[], dict[str, str]] | None = None,
    interactive: Callable[[], bool] | None = None,
) -> None:
    """Zero-friction onboarding: detect capabilities, pick enforcement mode, write config, install.

    ``input_fn``, ``runner``, ``detect`` (``runtime_offer.runtime_status``) and
    ``interactive`` (stdin is a terminal) are injectable so tests never touch the host.
    """
    from llm_router import runtime_offer as _ro
    from llm_router.onboard_needs import ask_needs

    STATE_DIR = str(paths.llm_router_home())

    print(f"\n{_bold('╔══════════════════════════════════════════╗')}")
    print(f"{_bold('║   LLM Router — Onboarding Wizard          ║')}")
    print(f"{_bold('╚══════════════════════════════════════════╝')}\n")
    print("  Detecting your setup...\n")

    # ── 1. Detect local runtimes ──────────────────────────────────────────────
    runtimes = dict((detect or _ro.runtime_status)())
    ollama = runtimes.get("ollama")
    if ollama == "running":
        print(_ok("Ollama running  (free local tier, ~1–3s)"))
    elif ollama == "installed":
        print(_warn("Ollama installed but not running  — start it with: ollama serve"))
    for name, state in runtimes.items():
        if name == "ollama":
            continue
        if state == "running":
            print(_ok(f"Local runtime detected: {name}  (via openai_compat)"))
        else:
            print(_warn(f"Local runtime installed but not running: {name}  — start its server"))
    if not runtimes:
        print(_warn("No local model runtime detected"))
        print(_ro.runtime_offer())
    # Installed Ollama counts: the session hook starts it. Other runtimes count
    # only while their server answers.
    local_ok = ollama is not None or "running" in runtimes.values()

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
    if local_ok or codex_ok:
        profile = "budget"
        print(f"  {_bold('Recommended profile:')} {_green('budget')}  (free-first via Ollama/Codex)")
    elif found_keys:
        profile = "balanced"
        print(f"  {_bold('Recommended profile:')} {_yellow('balanced')}  (paid API routing)")
    else:
        print(f"\n  {_red('✗  No routing backends found!')}")
        if runtimes:
            print(_ro.runtime_offer())
        print("     Start a local runtime or set an API key, then re-run `llm-router onboard`.\n")
        # Only a person at a terminal can approve a system install: piped input
        # (`yes | llm-router onboard`) never runs it.
        if not (interactive or sys.stdin.isatty)():
            return
        try:
            answer = input_fn("  Run the install command above now? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "n"
        if answer in ("y", "yes"):
            print("  " + _ro.install_runtime(confirm=True, runner=runner))
        return

    # ── 4b. Needs questions (feed the task-4 recommender; not wired yet) ──────
    print(f"\n  {_bold('A few questions (Enter keeps the default, s skips):')}")
    needs = ask_needs(input_fn, print)
    print(f"  Needs: {needs.main_use}, languages={','.join(needs.languages)}, priority={needs.priority}")

    # ── 5. Choose enforcement mode ────────────────────────────────────────────
    print(f"\n  {_bold('Enforcement mode:')}")
    print(f"    {_dim('[1] shadow')}   — observe routing decisions, no enforcement  {_dim('(safe start)')}")
    print(f"    {_dim('[2] suggest')}  — show routing hints, allow overrides")
    print(f"    {_dim('[3] enforce')}  — block Claude when routing is violated  {_dim('(maximum savings)')}")
    print()
    try:
        choice = input_fn("  Choose [1/2/3, default=1]: ").strip() or "1"
    except (EOFError, KeyboardInterrupt):
        choice = "1"

    enforce = {"1": "shadow", "2": "suggest", "3": "hard"}.get(choice, "shadow")
    mode_label = {"shadow": _dim("shadow  (observation only)"),
                  "suggest": _yellow("suggest  (hints, no blocking)"),
                  "hard": _green("enforce  (maximum savings)")}.get(enforce, enforce)

    # ── 5b. Ground Truth consent (R8) ─────────────────────────────────────────
    # The audit found llm-router claiming it preserves task success while the
    # only mechanism that could measure that was off by default. The decision
    # was to turn it on — but it reads the operator's prompts, so it is asked
    # for rather than assumed. `ask()` records a REFUSAL when the session is
    # not interactive: a CI run or a Dockerfile cannot agree to anything.
    gt_enabled = False
    try:
        from llm_router import ground_truth_consent

        consent = ground_truth_consent.ask(interactive=sys.stdin.isatty())
        gt_enabled = consent.granted
    except Exception as exc:  # noqa: BLE001
        from llm_router import failopen
        failopen.record("CHZ-FO-ONBOARD-GT-CONSENT", exc)

    # ── 6. Write config to ~/.llm-router/.env ────────────────────────────────
    env_path = os.path.join(STATE_DIR, ".env")
    os.makedirs(STATE_DIR, exist_ok=True)
    env_lines = [
        f"LLM_ROUTER_ENFORCE={enforce}",
        f"LLM_ROUTER_PROFILE={profile}",
        # Written either way, so an existing `=1` from a previous install is
        # turned OFF by a later refusal rather than silently surviving it.
        f"LLM_ROUTER_GROUND_TRUTH={'1' if gt_enabled else '0'}",
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
