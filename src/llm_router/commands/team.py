"""Team command — manage team reports and notifications."""

from __future__ import annotations

import os
import re as _re
import sys
from pathlib import Path


# ── Formatting utilities ────────────────────────────────────────────────────

def _color_enabled() -> bool:
    """Check if color output is enabled."""
    return sys.stdout.isatty() and not os.getenv("NO_COLOR")


def _bold(s: str) -> str:
    """Bold text."""
    return f"\033[1m{s}\033[0m" if _color_enabled() else s


def _green(s: str) -> str:
    """Green text."""
    return f"\033[32m{s}\033[0m" if _color_enabled() else s


def _red(s: str) -> str:
    """Red text."""
    return f"\033[31m{s}\033[0m" if _color_enabled() else s


def _yellow(s: str) -> str:
    """Yellow text."""
    return f"\033[33m{s}\033[0m" if _color_enabled() else s


def _dim(s: str) -> str:
    """Dim text."""
    return f"\033[2m{s}\033[0m" if _color_enabled() else s


# ── Team report ────────────────────────────────────────────────────────────

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


# ── Entry point ─────────────────────────────────────────────────────────────

def cmd_team(args: list[str]) -> int:
    """Execute: llm-router team [report|push|setup] [period]

    Manage team reports and notifications.
    """
    subcmd = args[0] if args else "report"
    flags = args[1:] if args else []
    _run_team(subcmd, flags)
    return 0
