"""Team identity and multi-channel notification for v3.0 Team Dashboard.

Provides:
- User/project identity detection (git email, hostname, git remote)
- Savings report formatting
- Multi-channel push: Slack, Discord, Telegram, generic HTTP

Channel detection is automatic based on the endpoint URL:
  hooks.slack.com          → Slack Block Kit
  discord.com/api/webhooks → Discord Embed
  api.telegram.org/bot*    → Telegram Bot API (requires chat_id)
  anything else            → Generic JSON POST
"""

from __future__ import annotations

import logging
import re
import socket
import subprocess
from typing import Any

log = logging.getLogger("llm_router.team")


# ── Identity ──────────────────────────────────────────────────────────────────

def get_user_id(override: str = "") -> str:
    """Return a stable user identifier.

    Priority:
      1. ``override`` arg (from ``LLM_ROUTER_USER_ID`` env var)
      2. ``git config user.email`` in the current directory
      3. ``username@hostname`` as a final fallback
    """
    if override:
        return override
    try:
        email = subprocess.check_output(
            ["git", "config", "user.email"],
            stderr=subprocess.DEVNULL, text=True,
        ).strip()
        if email:
            return email
    except Exception:
        pass
    import getpass
    try:
        user = getpass.getuser()
    except Exception:
        user = "unknown"
    host = socket.gethostname().split(".")[0]
    return f"{user}@{host}"


def get_project_id() -> str:
    """Return a stable project identifier.

    Priority:
      1. Git remote origin URL, normalised to ``owner/repo``
      2. Basename of the current working directory
    """
    try:
        url = subprocess.check_output(
            ["git", "remote", "get-url", "origin"],
            stderr=subprocess.DEVNULL, text=True,
        ).strip()
        m = re.search(r"[:/]([^/]+/[^/]+?)(?:\.git)?$", url)
        if m:
            return m.group(1)
    except Exception:
        pass
    import os
    return os.path.basename(os.getcwd())


# ── Channel detection ─────────────────────────────────────────────────────────

def detect_channel(url: str) -> str:
    """Infer notification channel from URL.

    Returns one of: ``"slack"``, ``"discord"``, ``"telegram"``, ``"generic"``.
    """
    if not url:
        return "generic"
    if "hooks.slack.com" in url:
        return "slack"
    if "discord.com/api/webhooks" in url:
        return "discord"
    if "api.telegram.org/bot" in url:
        return "telegram"
    return "generic"


# ── Formatters ────────────────────────────────────────────────────────────────

def _bar(pct: float, width: int = 8) -> str:
    filled = round(pct * width)
    return "█" * filled + "░" * (width - filled)


def _slack_payload(report: dict[str, Any]) -> dict:
    user = report.get("user_id", "unknown")
    project = report.get("project_id", "unknown")
    period = report.get("period", "all-time")
    calls = report.get("total_calls", 0)
    saved = report.get("saved_usd", 0.0)
    free_pct = report.get("free_pct", 0.0)
    top_models = report.get("top_models", [])

    model_text = "\n".join(
        f"• `{m['model']}`  {m['calls']}x  ${m['cost']:.4f}"
        for m in top_models[:5]
    ) or "_no data_"

    context_text = (
        f"Fleet-wide realized savings ({period}): {_fmt_usd_or_na(report.get('realized_savings_usd'))}"
        f"  |  Inferred misroute rate (baseline): {_fmt_pct_or_na(report.get('mis_route_rate_inferred'))}"
    )

    return {
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": "🤖 LLM Router Savings Report"}},
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*User*\n{user}"},
                    {"type": "mrkdwn", "text": f"*Project*\n{project}"},
                    {"type": "mrkdwn", "text": f"*Period*\n{period}"},
                    {"type": "mrkdwn", "text": f"*Calls*\n{calls:,}"},
                    {"type": "mrkdwn", "text": f"*Saved*\n${saved:.4f}"},
                    {"type": "mrkdwn", "text": f"*Free tier*\n{free_pct:.0%}  {_bar(free_pct)}"},
                ],
            },
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*Top models*\n{model_text}"}},
            {"type": "context", "elements": [{"type": "mrkdwn", "text": context_text}]},
            {"type": "divider"},
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": "Powered by <https://github.com/ypollak2/llm-router|llm-router>"}],
            },
        ]
    }


def _discord_payload(report: dict[str, Any]) -> dict:
    user = report.get("user_id", "unknown")
    project = report.get("project_id", "unknown")
    period = report.get("period", "all-time")
    calls = report.get("total_calls", 0)
    saved = report.get("saved_usd", 0.0)
    free_pct = report.get("free_pct", 0.0)
    top_models = report.get("top_models", [])

    model_text = "\n".join(
        f"• `{m['model']}` — {m['calls']}x · ${m['cost']:.4f}"
        for m in top_models[:5]
    ) or "_(no data)_"

    return {
        "embeds": [{
            "title": "🤖 LLM Router Savings Report",
            "color": 0x00C851,
            "fields": [
                {"name": "User", "value": user, "inline": True},
                {"name": "Project", "value": project, "inline": True},
                {"name": "Period", "value": period, "inline": True},
                {"name": "Calls", "value": str(calls), "inline": True},
                {"name": "Saved", "value": f"${saved:.4f}", "inline": True},
                {"name": "Free tier", "value": f"{free_pct:.0%}  {_bar(free_pct)}", "inline": True},
                {"name": "Top models", "value": model_text, "inline": False},
                {
                    "name": "Fleet-wide context",
                    "value": (
                        f"Realized savings ({period}): "
                        f"{_fmt_usd_or_na(report.get('realized_savings_usd'))}\n"
                        f"Inferred misroute rate (baseline): "
                        f"{_fmt_pct_or_na(report.get('mis_route_rate_inferred'))}"
                    ),
                    "inline": False,
                },
            ],
            "footer": {"text": "llm-router · github.com/ypollak2/llm-router"},
        }]
    }


def _telegram_message(report: dict[str, Any], chat_id: str) -> dict:
    def _esc(s: str) -> str:
        for ch in r"_*[]()~`>#+-=|{}.!":
            s = s.replace(ch, f"\\{ch}")
        return s

    user = _esc(report.get("user_id", "unknown"))
    project = _esc(report.get("project_id", "unknown"))
    period = _esc(report.get("period", "all-time"))
    calls = report.get("total_calls", 0)
    saved = report.get("saved_usd", 0.0)
    actual = report.get("actual_usd", 0.0)
    free_pct = report.get("free_pct", 0.0)
    top_models = report.get("top_models", [])

    model_lines = "\n".join(
        f"  • `{_esc(m['model'])}` — {m['calls']}x · ${m['cost']:.4f}"
        for m in top_models[:5]
    ) or "  _\\(no data\\)_"

    realized_savings = _esc(_fmt_usd_or_na(report.get("realized_savings_usd")))
    misroute_rate = _esc(_fmt_pct_or_na(report.get("mis_route_rate_inferred")))

    text = (
        f"*🤖 LLM Router Savings Report*\n\n"
        f"👤 *User:* {user}\n"
        f"📁 *Project:* {project}\n"
        f"📅 *Period:* {period}\n\n"
        f"📊 *Calls:* {calls:,}\n"
        f"💰 *Saved:* \\${saved:.4f}  \\(paid \\${actual:.4f}\\)\n"
        f"🆓 *Free tier:* {free_pct:.0%}  {_esc(_bar(free_pct))}\n\n"
        f"🏆 *Top models:*\n{model_lines}\n\n"
        f"🌐 *Fleet\\-wide context:* savings {realized_savings} · "
        f"misroute rate {misroute_rate}\n\n"
        f"_Powered by [llm\\-router](https://github\\.com/ypollak2/llm\\-router)_"
    )
    return {"chat_id": chat_id, "text": text, "parse_mode": "MarkdownV2"}


def _fmt_usd_or_na(value: float | None) -> str:
    """Format a nullable USD figure, defensively — the WS3 context field may be None."""
    return f"${value:.4f}" if value is not None else "N/A"


def _fmt_pct_or_na(value: float | None) -> str:
    """Format a nullable 0.0-1.0 fraction as a percent, defensively — the WS2
    context field may be None."""
    return f"{value * 100:.1f}%" if value is not None else "N/A"


def _generic_payload(report: dict[str, Any]) -> dict:
    return {"source": "llm-router", "version": "3.0", "report": report}


# ── Push ──────────────────────────────────────────────────────────────────────

async def push_report(
    report: dict[str, Any],
    url: str,
    telegram_chat_id: str = "",
) -> tuple[bool, str]:
    """Push a savings report to the configured notification channel.

    Args:
        report: Report dict from ``build_team_report()``.
        url: Webhook or API URL — channel auto-detected from URL pattern.
        telegram_chat_id: Required for Telegram; ignored for other channels.

    Returns:
        ``(success, message)`` tuple.
    """
    import aiohttp

    channel = detect_channel(url)

    if channel == "slack":
        payload, post_url = _slack_payload(report), url
    elif channel == "discord":
        payload, post_url = _discord_payload(report), url
    elif channel == "telegram":
        if not telegram_chat_id:
            return False, "Telegram requires LLM_ROUTER_TEAM_CHAT_ID to be set."
        post_url = url.rstrip("/")
        if not post_url.endswith("/sendMessage"):
            post_url += "/sendMessage"
        payload = _telegram_message(report, telegram_chat_id)
    else:
        payload, post_url = _generic_payload(report), url

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                post_url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status in (200, 204):
                    return True, f"✓ Report pushed to {channel} ({resp.status})"
                body = await resp.text()
                return False, f"HTTP {resp.status}: {body[:200]}"
    except Exception as e:
        return False, f"Push failed: {e}"


# ── Report builder ─────────────────────────────────────────────────────────────

# team.py's own period vocabulary ("all") doesn't match dashboard_data's
# WindowLiteral ("lifetime") — map the one divergent value, pass the rest through.
_PERIOD_TO_WINDOW: dict[str, str] = {
    "today": "today",
    "week": "week",
    "month": "month",
    "all": "lifetime",
}


def _add_ws23_context(report: dict[str, Any], period: str) -> dict[str, Any]:
    """Additively enrich `report` with WS2/WS3 population-level context.

    Adds ``realized_savings_usd`` (WS3, ``dashboard_data.query_realized_savings``)
    and ``mis_route_rate_inferred`` (WS2, ``routing_quality.summarize``).

    Both fields are best-effort and fail-open, mirroring
    ``audit_routing.run_audit()`` / ``retrospective._add_ws267_context()``'s
    precedent: a missing or errored data source leaves the field as ``None``
    and never raises or blocks the team report.

    Important caveat: unlike ``total_calls``/``saved_usd``/``actual_usd``
    (which are filtered to this report's ``user_id``/``project_id`` via
    ``cost.get_team_savings``'s SQL WHERE clause), neither
    ``query_realized_savings`` nor ``routing_quality.summarize`` support
    per-user/per-project filtering — both are strictly global/fleet-wide
    figures. They provide population-level context alongside this report's
    scoped figures, not a scoped measurement of this user/project.
    """
    window = _PERIOD_TO_WINDOW.get(period, "week")

    try:
        from llm_router.dashboard_data import query_realized_savings

        report["realized_savings_usd"] = query_realized_savings(
            window
        ).realized_savings_usd
    except Exception as exc:  # noqa: BLE001 - fail-open, context-only field
        log.warning("team_report_realized_savings_failed error=%s", exc)
        report["realized_savings_usd"] = None

    try:
        from llm_router.routing_quality import summarize as _summarize_quality

        report["mis_route_rate_inferred"] = _summarize_quality().get(
            "mis_route_rate_inferred"
        )
    except Exception as exc:  # noqa: BLE001 - fail-open, context-only field
        log.warning("team_report_quality_baseline_failed error=%s", exc)
        report["mis_route_rate_inferred"] = None

    return report


async def build_team_report(
    user_id: str,
    project_id: str,
    period: str = "week",
) -> dict[str, Any]:
    """Build a savings report dict for the given user/project/period.

    Args:
        user_id: User identifier string.
        project_id: Project identifier string.
        period: ``"today"``, ``"week"``, ``"month"``, or ``"all"``.

    Returns:
        Report dict with keys: user_id, project_id, period, total_calls,
        saved_usd, actual_usd, free_pct, top_models, realized_savings_usd
        (WS3 fleet-wide context, see ``_add_ws23_context``),
        mis_route_rate_inferred (WS2 fleet-wide context, see
        ``_add_ws23_context``).
    """
    from llm_router.cost import get_team_savings

    data = await get_team_savings(user_id=user_id, project_id=project_id, period=period)
    report = {
        "user_id": user_id,
        "project_id": project_id,
        "period": period,
        "total_calls": data.get("total_calls", 0),
        "saved_usd": data.get("saved_usd", 0.0),
        "actual_usd": data.get("actual_usd", 0.0),
        "free_pct": data.get("free_pct", 0.0),
        "top_models": data.get("top_models", []),
    }
    return _add_ws23_context(report, period)
