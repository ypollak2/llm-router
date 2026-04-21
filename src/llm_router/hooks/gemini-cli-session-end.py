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

        # Try to get provider breakdown
        from llm_router.cost import get_provider_spend_breakdown

        breakdown = await get_provider_spend_breakdown(days=1)
        free_providers = sum(
            cost_usd
            for provider, cost_usd in (breakdown or {}).items()
            if provider in {"gemini_cli", "codex", "ollama"}
        )

        summary["spend"] = {
            "daily_usd": daily_spend,
            "free_providers_usd": free_providers,
            "estimated_savings_pct": (
                int((free_providers / daily_spend * 100))
                if daily_spend > 0
                else 0
            ),
        }
    except Exception:
        summary["spend"] = None

    return summary


def format_quota_line(quota: dict) -> str:
    """Format quota status as a readable line."""
    if not quota:
        return "Gemini quota: unavailable"
    return (
        f"Gemini quota: {quota['count']}/{quota['limit']} "
        f"({quota['percentage']}%) — "
        f"Tier: {quota['tier']}"
    )


def format_savings_line(spend: dict) -> str:
    """Format savings as a readable line."""
    if not spend:
        return ""
    if spend["daily_usd"] == 0:
        return "Spend: $0.00 (all free providers) ✅"
    return (
        f"Daily savings: ${spend['free_providers_usd']:.3f} "
        f"({spend['estimated_savings_pct']}% from free routing) ⚡"
    )


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
        lines = ["\n" + "=" * 60]
        lines.append("📊 LLM Router Session Summary")
        lines.append("=" * 60)

        if summary.get("quota"):
            lines.append(format_quota_line(summary["quota"]))

        if summary.get("spend"):
            savings_line = format_savings_line(summary["spend"])
            if savings_line:
                lines.append(savings_line)

        lines.append("=" * 60 + "\n")

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
