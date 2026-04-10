"""Interactive quickstart wizard for llm-router.

Target: < 5 minutes from zero to first routed call.

Steps:
  1. Detect installed AI coding hosts
  2. Offer API key configuration (or Ollama-only mode)
  3. Install for detected host(s)
  4. Fire a test call to confirm routing works
  5. Show projected monthly savings
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def detect_hosts() -> list[str]:
    """Detect which AI coding hosts are installed on this machine.

    Returns:
        List of host names from: "claude", "cursor", "vscode".
        Empty list if nothing is detected.
    """
    hosts: list[str] = []

    if shutil.which("claude"):
        hosts.append("claude")

    cursor_dir = Path.home() / ".cursor"
    if cursor_dir.exists():
        hosts.append("cursor")

    # VS Code settings paths
    if sys.platform == "darwin":
        vscode_dir = Path.home() / "Library" / "Application Support" / "Code"
    elif sys.platform == "win32":
        vscode_dir = Path(os.environ.get("APPDATA", "")) / "Code"
    else:
        vscode_dir = Path.home() / ".config" / "Code"

    if vscode_dir.exists():
        hosts.append("vscode")

    return hosts


def _has_any_api_key() -> bool:
    """Return True if at least one LLM API key is set in the environment."""
    key_vars = [
        "GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
        "PERPLEXITY_API_KEY", "GROQ_API_KEY", "MISTRAL_API_KEY",
        "DEEPSEEK_API_KEY",
    ]
    return any(os.getenv(k) for k in key_vars)


def _has_ollama() -> bool:
    """Return True if Ollama is reachable at localhost:11434."""
    import urllib.request
    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=2):
            return True
    except Exception:
        return False


def _print_step(n: int, total: int, title: str) -> None:
    print(f"\n  [{n}/{total}] {title}")
    print("  " + "─" * 50)


def _ask(prompt: str, default: str = "") -> str:
    """Prompt the user for input with an optional default."""
    if default:
        full_prompt = f"  {prompt} [{default}]: "
    else:
        full_prompt = f"  {prompt}: "
    try:
        response = input(full_prompt).strip()
        return response or default
    except (EOFError, KeyboardInterrupt):
        return default


def _ask_yes(prompt: str, default: bool = True) -> bool:
    """Ask a yes/no question."""
    hint = "[Y/n]" if default else "[y/N]"
    answer = _ask(f"{prompt} {hint}", "y" if default else "n")
    return answer.lower() in ("y", "yes", "")


def main() -> None:
    """Interactive quickstart wizard."""
    print("\n" + "═" * 54)
    print("  llm-router quickstart — first routed call in < 5min")
    print("═" * 54)

    total_steps = 5

    # ── Step 1: Detect hosts ───────────────────────────────────────────────────
    _print_step(1, total_steps, "Detecting AI coding hosts")
    hosts = detect_hosts()
    if hosts:
        print(f"  Found: {', '.join(hosts)}")
        target_hosts = hosts
    else:
        print("  No hosts auto-detected.")
        print("  Supported: claude (Claude Code), cursor, vscode")
        host_input = _ask("Which host do you use?", "claude")
        target_hosts = [h.strip() for h in host_input.split(",") if h.strip()]
        if not target_hosts:
            target_hosts = ["claude"]

    # ── Step 2: API keys ───────────────────────────────────────────────────────
    _print_step(2, total_steps, "Configure LLM providers")
    has_keys = _has_any_api_key()
    has_ollama = _has_ollama()

    if has_keys:
        print("  ✓ At least one API key found in environment.")
        print("  Tip: run `llm-router setup` for full provider configuration.")
    elif has_ollama:
        print("  ✓ Ollama detected — you can route for free locally.")
        print("  For cloud providers, add keys to your shell profile:")
        print("    export GEMINI_API_KEY=...  (free tier: 1M tokens/day)")
        print("    export OPENAI_API_KEY=...")
    else:
        print("  No API keys found and Ollama not running.")
        print("")
        print("  Fastest free option:")
        print("    1. Get a free Gemini key: aistudio.google.com/apikey")
        print("    2. export GEMINI_API_KEY=<your-key>")
        print("    3. Re-run this wizard")
        print("")
        print("  Free local option (no API key needed):")
        print("    1. Install Ollama: ollama.com")
        print("    2. ollama pull qwen2.5:0.5b")
        print("    3. Re-run this wizard")
        print("")
        if not _ask_yes("Continue without API keys?", default=False):
            print("\n  Exiting. Add keys and re-run: llm-router quickstart\n")
            return

    # ── Step 3: Install ────────────────────────────────────────────────────────
    _print_step(3, total_steps, "Installing for your hosts")

    from llm_router.cli import _install_host, _run_install

    for host in target_hosts:
        print(f"\n  Installing for: {host}")
        try:
            if host == "claude":
                _run_install(flags=[])
            else:
                _install_host(host)
            print(f"  ✓ {host} configured")
        except Exception as e:
            print(f"  ⚠ Could not auto-install for {host}: {e}")
            print(f"    Manual: llm-router install --host {host}")

    # ── Step 4: Test call ──────────────────────────────────────────────────────
    _print_step(4, total_steps, "Test routing (live call)")
    print("  Sending a test prompt: 'Say hello in exactly one word'")

    if _ask_yes("Run test call now?", default=True):
        try:
            import asyncio
            from llm_router.router import route_and_call
            from llm_router.types import TaskType, RoutingProfile

            async def _test() -> str:
                resp = await route_and_call(
                    TaskType.QUERY,
                    "Say hello in exactly one word.",
                    profile=RoutingProfile.BUDGET,
                )
                return resp.content

            result = asyncio.run(_test())
            print(f"\n  ✓ Routing works! Response: {result[:80]!r}")
        except Exception as e:
            print(f"\n  ⚠ Test call failed: {e}")
            print("    Check your API keys with: llm-router doctor")
    else:
        print("  Skipped.")

    # ── Step 5: Savings projection ─────────────────────────────────────────────
    _print_step(5, total_steps, "What you'll save")
    print("")
    print("  Typical savings with llm-router:")
    print("")
    print("  ┌─────────────────┬────────────┬────────────┬──────────┐")
    print("  │ Usage           │ Without    │ With       │ Savings  │")
    print("  ├─────────────────┼────────────┼────────────┼──────────┤")
    print("  │ 100 calls/day   │ ~$4.50/mo  │ ~$0.90/mo  │ 80%      │")
    print("  │ 500 calls/day   │ ~$22/mo    │ ~$4.50/mo  │ 80%      │")
    print("  │ 2000 calls/day  │ ~$90/mo    │ ~$18/mo    │ 80%      │")
    print("  └─────────────────┴────────────┴────────────┴──────────┘")
    print("")
    print("  Track your actual savings: llm_usage (in Claude Code)")

    # ── Done ──────────────────────────────────────────────────────────────────
    print("\n" + "═" * 54)
    print("  Setup complete!")
    print("")
    print("  Next steps:")
    for host in target_hosts:
        if host == "claude":
            print("    • Restart Claude Code — routing starts automatically")
        elif host == "cursor":
            print("    • Restart Cursor — routing starts automatically")
        elif host == "vscode":
            print("    • Restart VS Code and enable MCP in Copilot settings")
    print("")
    print("  Commands:")
    print("    llm-router status    — view routing status and savings")
    print("    llm-router doctor    — diagnose any installation issues")
    print("    llm-router setup     — configure additional providers")
    print("═" * 54 + "\n")
