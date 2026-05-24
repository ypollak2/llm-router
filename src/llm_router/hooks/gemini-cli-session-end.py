#!/usr/bin/env python3
"""Gemini CLI session-end hook — display quota and savings summary.

Fires at the end of a Gemini CLI session (when the user exits or session ends).
Shows:
- Gemini quota usage (requests / daily limit)
- Savings from routing to free providers (Ollama, Codex, Gemini CLI itself)
- Recommendations for future usage

Usage: Installed at ~/.llm-router/hooks/gemini-cli-session-end.py by `llm-router install`.
Registered in Gemini CLI's hook config to fire on SessionEnd.
"""

import json
import sys
import asyncio


async def get_session_summary() -> dict:
    """Gather session summary data.

    Returns:
        Dict with keys: quota_status, savings, recommendations
    """
    summary = {}
    
    # Get session start time to calculate session savings
    session_start = 0
    try:
        import os
        start_file = os.path.expanduser("~/.llm-router/session_start.txt")
        if os.path.exists(start_file):
            with open(start_file) as f:
                session_start = float(f.read().strip())
    except Exception:
        pass

    # Get Gemini quota status
    try:
        from llm_router.gemini_cli_quota import get_gemini_quota_status

        quota = await get_gemini_quota_status()
        count = quota.get("count", 0)
        limit = quota.get("daily_limit", 1500)
        tier = quota.get("tier", "unknown")
        pressure = quota.get("pressure", 0.0)

        summary["quota"] = {
            "count": count,
            "limit": limit,
            "tier": tier,
            "pressure": pressure,
            "percentage": int(pressure * 100),
        }
    except Exception:
        summary["quota"] = None

    # Get usage and savings data
    try:
        from llm_router import cost

        daily_spend = await cost.get_daily_spend()
        
        # Session spend
        session_spend = 0.0
        if session_start > 0:
            from llm_router.cost import get_total_spend_since
            from datetime import datetime, timezone
            since_iso = datetime.fromtimestamp(session_start, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            session_spend = await get_total_spend_since(since_iso)

        # Try to get provider breakdown
        from llm_router.cost import get_provider_spend_breakdown

        breakdown = await get_provider_spend_breakdown(days=1)
        free_providers_daily = sum(
            cost_usd
            for provider, cost_usd in (breakdown or {}).items()
            if provider in {"gemini_cli", "codex", "ollama"}
        )

        summary["spend"] = {
            "daily_usd": daily_spend,
            "session_usd": session_spend,
            "free_providers_daily_usd": free_providers_daily,
            "estimated_savings_pct": (
                int((free_providers_daily / (daily_spend + free_providers_daily) * 100))
                if (daily_spend + free_providers_daily) > 0
                else 0
            ),
        }
    except Exception:
        summary["spend"] = None

    # Dashboard URL
    try:
        from llm_router.dashboard.server import _get_or_create_token, DEFAULT_PORT
        token = _get_or_create_token()
        summary["dashboard_url"] = f"http://localhost:{DEFAULT_PORT}/?token={token}"
    except Exception:
        summary["dashboard_url"] = None

    return summary


def format_quota_bar(quota: dict) -> str:
    """Format quota status as a readable bar."""
    if not quota:
        return "  Gemini quota:  unavailable"
    
    width = 20
    filled = max(0, min(width, round(quota['pressure'] * width)))
    bar = "█" * filled + "░" * (width - filled)
    
    color = "\033[32m" # Green
    if quota['percentage'] > 90:
        color = "\033[31m" # Red
    elif quota['percentage'] > 70:
        color = "\033[33m" # Yellow
        
    return (
        f"  Gemini Quota   {color}{bar}\033[0m {quota['percentage']}% "
        f"({quota['count']}/{quota['limit']})"
    )


def format_savings_panel(spend: dict) -> str:
    """Format savings as a readable panel."""
    if not spend:
        return ""
    
    lines = []
    lines.append(f"  Session Spend: ${spend['session_usd']:.4f}")
    lines.append(f"  Daily Savings: \033[32m${spend['free_providers_daily_usd']:.3f}\033[0m ({spend['estimated_savings_pct']}% free)")
    
    return "\n".join(lines)


def hook_handler(event_data: dict) -> dict:
    """Handle SessionEnd event from Gemini CLI.

    Displays session summary to the user.

    Returns:
        Modified event_data with summary message.
    """
    try:
        # Run async summary gathering
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            summary = loop.run_until_complete(get_session_summary())
        finally:
            loop.close()

        # Format output
        width = 54
        div = "─" * width
        lines = ["\n  " + "\033[2m" + div + "\033[0m"]
        lines.append("  \033[1m📊 LLM Router Session Summary Dashboard\033[0m")
        lines.append("  " + "\033[2m" + div + "\033[0m")

        if summary.get("quota"):
            lines.append(format_quota_bar(summary["quota"]))

        if summary.get("spend"):
            lines.append("")
            lines.append(format_savings_panel(summary["spend"]))

        if summary.get("dashboard_url"):
            lines.append("")
            lines.append(f"  \033[1mDashboard:\033[0m \033[4;34m{summary['dashboard_url']}\033[0m")

        lines.append("  " + "\033[2m" + div + "\033[0m\n")

        # Add message to event (display to user)
        message = "\n".join(lines)
        event_data["summary_message"] = message
        if "outputs" not in event_data:
            event_data["outputs"] = []
        event_data["outputs"].append({"type": "text", "content": message})

        return event_data
    except Exception as e:
        # Never let hook errors break Gemini
        print(f"Session-end hook error (ignored): {e}", file=sys.stderr)
        return event_data


if __name__ == "__main__":
    # When called directly, expect event JSON on stdin
    try:
        event_data = json.loads(sys.stdin.read())
        result = hook_handler(event_data)
        print(json.dumps(result))
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
