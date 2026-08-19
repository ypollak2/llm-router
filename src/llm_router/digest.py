"""Savings digest + spend-spike detection (v3.3).

Provides three capabilities:
  1. format_digest(period)       — formatted savings summary for a time window
  2. detect_spend_spike()        — compare today vs 7-day average; flag anomalies
  3. simulate_without_routing()  — estimate cost if every call hit the host Claude model
  4. send_to_webhook(url, text)  — HTTP POST to Slack/Discord/generic webhook

The digest is compatible with Slack, Discord (block-formatted), and generic
webhooks. Channel format is auto-detected from the URL, same as llm_team_push.
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from llm_router.cost import _get_db
from llm_router.savings import net_saved

log = logging.getLogger("llm_router.digest")

# Fail-open fallback only. The authoritative host (frontier Claude) prices live
# in cost.py's single canonical source and are read at call time below (AC-4:
# no independent, drifting price copies per surface).
_HOST_IN_PER_M_FALLBACK  = 5.0
_HOST_OUT_PER_M_FALLBACK = 25.0


def _host_baseline(in_tok: int, out_tok: int) -> float:
    """What the host (frontier Claude) model would charge for the same tokens.

    Prices are read from cost.py's canonical ``_HOST_INPUT_PER_M`` /
    ``_HOST_OUTPUT_PER_M`` at call time — so a runtime price-sync is reflected and
    digest can never drift from the rest of the accounting (AC-4 / INV-COST-004).
    Fails open to the last-known list price.
    """
    try:
        from llm_router import cost as _cost
        in_pm = float(_cost._HOST_INPUT_PER_M)
        out_pm = float(_cost._HOST_OUTPUT_PER_M)
    except Exception:  # noqa: BLE001 — a price read must never break the digest
        in_pm, out_pm = _HOST_IN_PER_M_FALLBACK, _HOST_OUT_PER_M_FALLBACK
    return (in_tok * in_pm + out_tok * out_pm) / 1_000_000


_PERIOD_SQL: dict[str, str] = {
    # timestamp is stored UTC (DEFAULT datetime('now')); convert to local before
    # comparing to the local day, else "today" drops the last N hours of activity
    # for non-UTC users near midnight.
    "today":     "date(timestamp,'localtime') = date('now','localtime')",
    "week":      "timestamp >= datetime('now', '-7 days')",
    "month":     "timestamp >= datetime('now', 'start of month')",
    "all time":  "1=1",
}

_FREE_PROVIDERS = {"ollama", "codex", "gemini_cli"}


async def _fetch_period_data(period: str) -> dict[str, Any]:
    """Query usage table for a given period and return aggregated stats."""
    where = _PERIOD_SQL.get(period, _PERIOD_SQL["week"])
    db = await _get_db()
    try:
        rows = await (await db.execute(
            f"""
            SELECT provider,
                   COUNT(*) as calls,
                   COALESCE(SUM(input_tokens),  0) as in_tok,
                   COALESCE(SUM(output_tokens), 0) as out_tok,
                   COALESCE(SUM(cost_usd),      0) as cost
            FROM usage
            WHERE success = 1 AND {where}
            GROUP BY provider
            """
        )).fetchall()
    finally:
        await db.close()

    total_calls = total_cost = total_saved = 0
    total_in = total_out = 0
    by_provider: dict[str, dict] = {}

    for provider, calls, in_tok, out_tok, cost in rows:
        baseline = _host_baseline(in_tok, out_tok)
        if provider in _FREE_PROVIDERS:
            saved = baseline
        elif provider == "subscription":
            saved = 0.0
        else:
            saved = net_saved(baseline, cost)
        total_calls += calls
        total_cost  += cost
        total_saved += saved
        total_in    += in_tok
        total_out   += out_tok
        by_provider[provider] = {"calls": calls, "cost": cost, "saved": saved}

    return {
        "calls":       total_calls,
        "cost":        total_cost,
        "saved":       total_saved,
        "total_in":    total_in,
        "total_out":   total_out,
        "by_provider": by_provider,
    }


async def detect_spend_spike(
    threshold_multiplier: float = 2.0,
) -> tuple[bool, float, float]:
    """Compare today's spend against 7-day average.

    Args:
        threshold_multiplier: Alert when today ≥ this × 7-day average.

    Returns:
        (is_spike, today_usd, avg_7day_usd)
    """
    db = await _get_db()
    try:
        today_row = await (await db.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM usage "
            "WHERE success=1 AND date(timestamp,'localtime') = date('now','localtime')"
        )).fetchone()
        week_row = await (await db.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM usage "
            "WHERE success=1 AND timestamp >= datetime('now', '-7 days')"
        )).fetchone()
    finally:
        await db.close()

    today_usd    = float(today_row[0]) if today_row else 0.0
    week_total   = float(week_row[0])  if week_row  else 0.0
    avg_7day_usd = week_total / 7.0

    is_spike = avg_7day_usd > 0.001 and today_usd >= threshold_multiplier * avg_7day_usd
    return is_spike, today_usd, avg_7day_usd


async def simulate_without_routing(period: str = "week") -> tuple[float, float, float]:
    """Estimate what costs would have been without any routing.

    Every successful call is priced at the host (frontier Claude) rate regardless of the actual
    provider — the "what if router was off?" baseline.

    Returns:
        (actual_cost, hypothetical_cost, savings_pct)
    """
    data = await _fetch_period_data(period)
    actual   = data["cost"]
    baseline = _host_baseline(data["total_in"], data["total_out"])
    savings_pct = (baseline - actual) / baseline * 100 if baseline > 0 else 0.0
    return actual, baseline, savings_pct


def _fmt_tok(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


async def format_digest(period: str = "week") -> str:
    """Format a savings digest for the given period.

    Args:
        period: "today", "week", "month", or "all time".

    Returns:
        Plain-text digest suitable for Slack/Discord/terminal.
    """
    data = await _fetch_period_data(period)
    is_spike, today_usd, avg_7day = await detect_spend_spike()
    actual, baseline, savings_pct = await simulate_without_routing(period)

    W = 64
    lines: list[str] = [
        "─" * W,
        f"  LLM Router — Savings Digest  ({period})",
        "",
        f"  Calls:      {data['calls']:>6}",
        f"  Actual cost: ${data['cost']:.4f}",
        f"  Saved:       ${data['saved']:.4f} vs Claude baseline",
    ]

    if data["by_provider"]:
        lines.append("")
        lines.append("  By provider:")
        for provider, pd in sorted(data["by_provider"].items(), key=lambda x: -x[1]["calls"]):
            lines.append(
                f"    {provider:<12}  {pd['calls']:>4} calls  "
                f"${pd['cost']:.4f} cost  ${pd['saved']:.4f} saved"
            )

    # What if router was off?
    if baseline > 0:
        lines.append("")
        lines.append(f"  📊 Without routing: ~${baseline:.4f} (Claude baseline)")
        lines.append(f"     Routing saved {savings_pct:.0f}% of potential cost")

    # Spend spike alert
    if is_spike:
        lines.append("")
        lines.append(
            f"  ⚠️  Spend spike: today ${today_usd:.4f} vs "
            f"${avg_7day:.4f}/day avg (7d)"
        )

    lines.append("─" * W)
    return "\n".join(lines)


def _slack_payload(text: str) -> dict:
    return {
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": f"```{text}```"}},
        ]
    }


def _discord_payload(text: str) -> dict:
    return {"content": f"```\n{text}\n```"}


def _generic_payload(text: str) -> dict:
    return {"text": text}


def _build_payload(url: str, text: str) -> dict:
    if "hooks.slack.com" in url or "slack.com/services" in url:
        return _slack_payload(text)
    if "discord.com/api/webhooks" in url:
        return _discord_payload(text)
    return _generic_payload(text)


async def send_to_webhook(url: str, text: str) -> tuple[bool, str]:
    """POST digest text to a webhook URL.

    Returns:
        (success, message)
    """
    if not url:
        return False, "No webhook URL configured (LLM_ROUTER_WEBHOOK_URL)."
    payload = _build_payload(url, text)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status in (200, 204):
                    return True, f"Digest sent (HTTP {resp.status})."
                body = await resp.text()
                return False, f"Webhook returned HTTP {resp.status}: {body[:200]}"
    except Exception as exc:
        return False, f"Webhook request failed: {exc}"
