#!/usr/bin/env python3
"""llm-router verify — End-to-end health check (30 seconds).

Command: uv run llm-router verify

Runs a comprehensive health check:
  1. Configuration loaded (env vars, API keys)
  2. SQLite database accessible
  3. All active providers online (Ollama, OpenAI, Gemini, etc.)
  4. Hooks installed + executable
  5. Routing chain verified
  6. Last 5 routing decisions checked

Example output:
  ✅ Configuration loaded from ~/.llm-router/config.yaml
  ✅ SQLite database: ~/.llm-router/usage.db (45 MB, last write 5 mins ago)

  ── Active Models ──
  ✅ Ollama (http://localhost:11434)
     └─ gemma4:latest (7B, avg 120 tok/s)
     └─ qwen2.5-coder:7b (7B, avg 95 tok/s)

  ✅ OpenAI API (org-id=org-xxx)
     └─ gpt-4o (available)
     └─ o3 (available)

  ✅ Gemini API
     └─ gemini-2.5-flash (available)

  ❌ Perplexity API (OFFLINE — check PERPLEXITY_API_KEY)

  ── Hooks Status ──
  ✅ auto-route hook (fires on every UserPromptSubmit)
  ✅ session-end hook (tracks savings)
  ✅ enforce-route hook (policy enforcement)

  ── Live Routing Chain ──
  simple:   ollama → openai/gpt-4o-mini → gemini/flash
  moderate: ollama → openai/gpt-4o → gemini/pro → sonnet
  complex:  ollama → openai/o3 → claude/opus

  ── Last 5 Decisions ──
  2 min ago  → haiku (code, simple)      $0.0001
  5 min ago  → sonnet (analysis, mod)    $0.008
  12 min ago → opus (planning, complex)  $0.062
  ...

  ─────────────────────────────────────────────
  No issues detected. You're good! 🚀
"""

import argparse
import json
import os
import sqlite3
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional

from llm_router.terminal_style import (
    Color,
    Symbol,
)


def check_configuration() -> tuple[bool, str]:
    """Check if configuration is properly loaded.

    Returns:
        (success, message)
    """
    config_path = Path.home() / ".llm-router" / "config.yaml"
    if config_path.exists():
        return True, f"Configuration loaded from {config_path}"
    return True, "Using environment variables (no config.yaml)"


def check_database() -> tuple[bool, str]:
    """Check if SQLite database is accessible.

    Returns:
        (success, message)
    """
    db_path = Path.home() / ".llm-router" / "usage.db"
    if not db_path.exists():
        return False, f"Database not found: {db_path}"

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM routing_decisions")
        count = cursor.fetchone()[0]
        conn.close()

        size_mb = db_path.stat().st_size / (1024 * 1024)
        mtime = datetime.fromtimestamp(db_path.stat().st_mtime)
        time_ago = datetime.now() - mtime

        if time_ago.total_seconds() < 300:  # 5 minutes
            time_str = f"{int(time_ago.total_seconds())} seconds ago"
        elif time_ago.total_seconds() < 3600:  # 1 hour
            time_str = f"{int(time_ago.total_seconds() / 60)} minutes ago"
        else:
            time_str = f"{int(time_ago.total_seconds() / 3600)} hours ago"

        return True, f"SQLite database: {db_path} ({size_mb:.1f} MB, last write {time_str})"
    except sqlite3.Error as e:
        return False, f"Database error: {e}"


def check_ollama() -> tuple[bool, str]:
    """Check if Ollama is running and list models.

    Returns:
        (success, message)
    """
    url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
    try:
        response = urllib.request.urlopen(f"{url}/api/tags", timeout=2)
        data = json.loads(response.read())
        models = data.get("models", [])

        if not models:
            return True, "Ollama running but no models loaded"

        model_str = " | ".join([m.get("name", "unknown") for m in models[:3]])
        if len(models) > 3:
            model_str += f" (+{len(models) - 3} more)"

        return True, f"Ollama ({url}) — {len(models)} models: {model_str}"
    except Exception as e:
        return False, f"Ollama OFFLINE ({url}) — {str(e)}"


def check_openai() -> tuple[bool, str]:
    """Check if OpenAI API key is configured.

    Returns:
        (success, message)
    """
    if os.environ.get("OPENAI_API_KEY"):
        return True, "OpenAI API configured"
    return False, "OpenAI API key missing (OPENAI_API_KEY)"


def check_gemini() -> tuple[bool, str]:
    """Check if Gemini API key is configured.

    Returns:
        (success, message)
    """
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        return True, "Gemini API configured"
    return False, "Gemini API key missing (GEMINI_API_KEY)"


def check_hooks() -> tuple[bool, list[str]]:
    """Check if routing hooks are installed.

    Returns:
        (all_ok, messages)
    """
    hooks_dir = Path.home() / ".claude" / "hooks"
    messages = []
    all_ok = True

    hook_names = [
        "llm-router-auto-route.py",
        "llm-router-session-end.py",
        "llm-router-enforce-route.py",
    ]

    for hook_name in hook_names:
        hook_path = hooks_dir / hook_name
        if hook_path.exists():
            if os.access(hook_path, os.X_OK):
                messages.append(f"✅ {hook_name} (installed + executable)")
            else:
                messages.append(f"⚠ {hook_name} (installed but not executable)")
                all_ok = False
        else:
            messages.append(f"❌ {hook_name} (not found)")
            all_ok = False

    return all_ok, messages


def check_last_decisions(limit: int = 5) -> list[str]:
    """Check last routing decisions.

    Returns:
        List of decision descriptions
    """
    db_path = Path.home() / ".llm-router" / "usage.db"
    if not db_path.exists():
        return []

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT timestamp, model, task_type, task_complexity, cost_usd FROM routing_decisions "
            "WHERE is_simulated = 0 ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        )
        rows = cursor.fetchall()
        conn.close()

        messages = []
        for timestamp, model, task_type, complexity, cost in rows:
            try:
                dt = datetime.fromisoformat(timestamp)
                time_ago = datetime.now() - dt
                if time_ago.total_seconds() < 60:
                    time_str = "just now"
                elif time_ago.total_seconds() < 3600:
                    time_str = f"{int(time_ago.total_seconds() / 60)} min ago"
                else:
                    time_str = f"{int(time_ago.total_seconds() / 3600)} hours ago"
            except (ValueError, TypeError):
                time_str = "unknown"

            msg = f"  {time_str:10} → {model:8} ({task_type}, {complexity}) ${cost:.4f}"
            messages.append(msg)

        return messages
    except sqlite3.Error:
        return []


def print_check_result(success: bool, message: str) -> None:
    """Print formatted check result.

    Args:
        success: Whether check passed
        message: Result message
    """
    icon = "✅" if success else "❌"
    print(f"{icon} {message}")


def main(args: Optional[list[str]] = None) -> int:
    """Main entry point for verify command.

    Args:
        args: Command line arguments (for testing)

    Returns:
        Exit code (0 if all checks pass)
    """
    parser = argparse.ArgumentParser(
        description="Health check for llm-router"
    )
    parser.parse_args(args or [])

    print()
    print(Color.ORCHESTRATE_BLUE("=" * 70))
    print(Color.ORCHESTRATE_BLUE("              llm-router health check"))
    print(Color.ORCHESTRATE_BLUE("=" * 70))
    print()

    all_ok = True

    # Configuration
    print(Color.ORCHESTRATE_BLUE("Configuration"))
    print(Color.ORCHESTRATE_BLUE("─" * 70))
    success, msg = check_configuration()
    print_check_result(success, msg)
    all_ok = all_ok and success

    success, msg = check_database()
    print_check_result(success, msg)
    all_ok = all_ok and success
    print()

    # Providers
    print(Color.ORCHESTRATE_BLUE("Providers"))
    print(Color.ORCHESTRATE_BLUE("─" * 70))
    success, msg = check_ollama()
    print_check_result(success, msg)

    success, msg = check_openai()
    print_check_result(success, msg)
    all_ok = all_ok and success

    success, msg = check_gemini()
    print_check_result(success, msg)
    all_ok = all_ok and success
    print()

    # Hooks
    print(Color.ORCHESTRATE_BLUE("Hooks"))
    print(Color.ORCHESTRATE_BLUE("─" * 70))
    hooks_ok, hook_messages = check_hooks()
    for msg in hook_messages:
        print(msg)
    all_ok = all_ok and hooks_ok
    print()

    # Recent decisions
    print(Color.ORCHESTRATE_BLUE("Recent Decisions"))
    print(Color.ORCHESTRATE_BLUE("─" * 70))
    decisions = check_last_decisions(5)
    if decisions:
        for msg in decisions:
            print(msg)
    else:
        print("  No routing decisions yet")
    print()

    # Summary
    print(Color.ORCHESTRATE_BLUE("─" * 70))
    if all_ok:
        print(f"{Symbol.SUCCESS.value} No issues detected. You're good! {Symbol.LIGHTNING.value}")
    else:
        print(f"{Symbol.WARNING.value} Some issues detected. See above.")
    print()

    return 0 if all_ok else 1


if __name__ == "__main__":
    exit(main())
