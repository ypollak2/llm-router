"""Savings dashboard — token-centric time-series with ANSI colors.

Provides `llm_dashboard` MCP tool showing:
- Time-series chart of daily tokens saved (ASCII bars with color)
- Per-model breakdown with gross/classifier/net columns
- Routing distribution with colored bars
- Billing-mode-aware messaging (subscription = quota freed)

All data from existing SQLite `usage` table — no new collection needed.
"""

from __future__ import annotations

import os

from mcp.server.mcpserver import Context

from llm_router.cost import _get_db
from llm_router.savings import net_saved

# ── ANSI color codes ────────────────────────────────────────────────────────

_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_BLUE = "\033[34m"
_CYAN = "\033[36m"
_MAGENTA = "\033[35m"
_RED = "\033[31m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RESET = "\033[0m"
_BG_GREEN = "\033[42m"
_BG_BLUE = "\033[44m"
_BG_YELLOW = "\033[43m"
_BG_CYAN = "\033[46m"
_BG_MAGENTA = "\033[45m"

# Block characters for bars
_FULL_BLOCK = "█"
_HALF_BLOCK = "▌"

# Fail-open fallback only — the authoritative host (frontier Claude) rates live
# in cost.py's single canonical source and are read at call time in
# _host_baseline (AC-3/AC-4: no stale, independent, drifting price copies; the
# hardcoded 15/75 here was ~3x the current list price).
HOST_INPUT_PER_M = 5.0
HOST_OUTPUT_PER_M = 25.0

# Provider colors for distribution bars
_PROVIDER_COLORS = {
    "ollama": _GREEN,
    "gemini": _BLUE,
    "deepseek": _CYAN,
    "openai": _MAGENTA,
    "groq": _YELLOW,
    "cache": _GREEN,
    "codex": _GREEN,
    "anthropic": _MAGENTA,
    "mistral": _RED,
    "cohere": _YELLOW,
    "xai": _DIM,
}


def _window_to_sql(window: str) -> str:
    """Convert window string to SQLite datetime filter."""
    w = window.lower().strip()
    if w in ("14d", "2w"):
        return "datetime('now', '-14 days')"
    if w in ("30d", "1m"):
        return "datetime('now', '-30 days')"
    if w in ("3m", "90d"):
        return "datetime('now', '-90 days')"
    if w in ("1y", "12m", "365d"):
        return "datetime('now', '-365 days')"
    if w == "all":
        return "datetime('2020-01-01')"
    # Default 14 days
    return "datetime('now', '-14 days')"


def _window_label(window: str) -> str:
    w = window.lower().strip()
    labels = {
        "14d": "LAST 14 DAYS", "2w": "LAST 14 DAYS",
        "30d": "LAST 30 DAYS", "1m": "LAST MONTH",
        "3m": "LAST 3 MONTHS", "90d": "LAST 3 MONTHS",
        "1y": "LAST YEAR", "12m": "LAST YEAR", "365d": "LAST YEAR",
        "all": "ALL TIME",
    }
    return labels.get(w, "LAST 14 DAYS")


def _host_baseline(input_tokens: int, output_tokens: int) -> float:
    """What the host (frontier Claude) model would charge for the same tokens.

    Reads cost.py's canonical ``_HOST_INPUT_PER_M`` / ``_HOST_OUTPUT_PER_M`` at
    call time so the dashboard can never drift from the rest of the accounting
    (AC-3/AC-4 / INV-COST-004). Fails open to the module fallback list price.
    """
    try:
        from llm_router import cost as _cost
        in_pm = float(_cost._HOST_INPUT_PER_M)
        out_pm = float(_cost._HOST_OUTPUT_PER_M)
    except Exception:  # noqa: BLE001 — a price read must never break the dashboard
        in_pm, out_pm = HOST_INPUT_PER_M, HOST_OUTPUT_PER_M
    return (input_tokens * in_pm + output_tokens * out_pm) / 1_000_000


def _format_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _render_bar(value: float, max_value: float, width: int = 30, color: str = _GREEN) -> str:
    """Render a colored horizontal bar."""
    if max_value <= 0:
        return ""
    filled = int((value / max_value) * width)
    filled = min(filled, width)
    return f"{color}{_FULL_BLOCK * filled}{_RESET}"


def _render_sparkline(values: list[float], width: int = 40) -> str:
    """Render a colored sparkline bar chart for daily values."""
    if not values:
        return ""
    max_val = max(values) if max(values) > 0 else 1
    blocks = " ▁▂▃▄▅▆▇█"
    line = ""
    for v in values:
        idx = int((v / max_val) * 8)
        idx = min(idx, 8)
        line += f"{_GREEN}{blocks[idx]}{_RESET}"
    return line


async def _query_daily_savings(since_sql: str, baseline: str = "opus") -> list[dict]:
    """Query daily token savings from usage table."""
    db = await _get_db()
    try:
        cursor = await db.execute(
            f"""
            SELECT
                date(timestamp,'localtime') as day,
                SUM(input_tokens + output_tokens) as total_tokens,
                SUM(input_tokens) as input_tokens,
                SUM(output_tokens) as output_tokens,
                SUM(cost_usd) as actual_cost,
                COUNT(*) as call_count
            FROM usage
            WHERE timestamp >= {since_sql}
              AND success = 1
            GROUP BY date(timestamp,'localtime')
            ORDER BY day
            """,
        )
        rows = await cursor.fetchall()
    finally:
        await db.close()

    baseline_fn = _host_baseline
    results = []
    for row in rows:
        day, total_tok, in_tok, out_tok, actual, calls = row
        base_cost = baseline_fn(in_tok, out_tok)
        saved = net_saved(base_cost, actual)
        results.append({
            "day": day,
            "tokens": total_tok,
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "actual_cost": actual,
            "baseline_cost": base_cost,
            "saved": saved,
            "calls": calls,
        })
    return results


async def _query_provider_breakdown(since_sql: str, baseline: str = "opus") -> list[dict]:
    """Query per-provider savings breakdown."""
    db = await _get_db()
    try:
        cursor = await db.execute(
            f"""
            SELECT
                provider,
                SUM(input_tokens + output_tokens) as total_tokens,
                SUM(input_tokens) as input_tokens,
                SUM(output_tokens) as output_tokens,
                SUM(cost_usd) as actual_cost,
                COUNT(*) as call_count
            FROM usage
            WHERE timestamp >= {since_sql}
              AND success = 1
            GROUP BY provider
            ORDER BY total_tokens DESC
            """,
        )
        rows = await cursor.fetchall()
    finally:
        await db.close()

    baseline_fn = _host_baseline
    results = []
    for row in rows:
        prov, total_tok, in_tok, out_tok, actual, calls = row
        base_cost = baseline_fn(in_tok, out_tok)
        saved = net_saved(base_cost, actual)
        results.append({
            "provider": prov,
            "tokens": total_tok,
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "actual_cost": actual,
            "baseline_cost": base_cost,
            "saved": saved,
            "calls": calls,
        })
    return results


async def _query_classifier_overhead(since_sql: str) -> float:
    """Sum classifier costs from routing_decisions table."""
    db = await _get_db()
    try:
        cursor = await db.execute(
            f"""
            SELECT COALESCE(SUM(classifier_latency_ms), 0)
            FROM routing_decisions
            WHERE timestamp >= {since_sql}
              AND is_real = 1
            """,
        )
        row = await cursor.fetchone()
        # Estimate cost from latency: ~$0.0001 per 1000ms of classifier time
        latency_ms = float(row[0]) if row else 0.0
        return latency_ms * 0.0000001  # rough estimate
    except Exception:
        return 0.0
    finally:
        await db.close()


def _render_dashboard(
    daily: list[dict],
    breakdown: list[dict],
    classifier_overhead: float,
    window_label: str,
    baseline: str,
    is_subscription: bool,
) -> str:
    """Render the full dashboard as a colored string."""
    lines: list[str] = []

    # ── Header ──────────────────────────────────────────────────────────
    lines.append(f"{_BOLD}╔══════════════════════════════════════════════════════════════╗{_RESET}")
    lines.append(f"{_BOLD}║  {_CYAN}LLM Router — Savings Dashboard{_RESET}{_BOLD}                              ║{_RESET}")
    # Show the ACTUAL canonical host price used by _host_baseline, not a stale
    # hardcoded "$15/$75" (AC-3/AC-4). Padded to the 60-col inner box width.
    _bl_in, _bl_out = HOST_INPUT_PER_M, HOST_OUTPUT_PER_M
    try:
        from llm_router import cost as _cost
        _bl_in, _bl_out = float(_cost._HOST_INPUT_PER_M), float(_cost._HOST_OUTPUT_PER_M)
    except Exception:  # noqa: BLE001
        pass
    _bl_text = f"Baseline: host Claude (${_bl_in:g}/${_bl_out:g} per 1M tokens)"
    lines.append(f"{_BOLD}║  {_DIM}{_bl_text}{_RESET}{_BOLD}{' ' * max(0, 60 - len(_bl_text))}║{_RESET}")
    lines.append(f"{_BOLD}╠══════════════════════════════════════════════════════════════╣{_RESET}")

    # ── Totals ──────────────────────────────────────────────────────────
    total_tokens = sum(d["tokens"] for d in daily)
    total_saved = sum(d["saved"] for d in daily) - classifier_overhead
    total_calls = sum(d["calls"] for d in daily)
    net_total_saved = total_saved
    # AUD-06: net_total_saved is SIGNED. Colour by sign -- a negative
    # rendered in green reads as a win, which is the display-layer half
    # of the same defect the clamp caused.
    _net_col = _GREEN if net_total_saved >= 0 else _RED

    lines.append("")
    lines.append(f"  {_BOLD}{window_label}{_RESET}")
    lines.append("")
    lines.append(f"  {_GREEN}{_BOLD}{_format_tokens(total_tokens)}{_RESET} tokens routed  ·  "
                 f"{_GREEN}{_BOLD}{total_calls}{_RESET} calls  ·  "
                 f"{_net_col}{_BOLD}${net_total_saved:.2f}{_RESET} net saved")
    lines.append("")

    # ── Time-series sparkline ───────────────────────────────────────────
    if daily:
        daily_tokens = [d["tokens"] for d in daily]

        lines.append(f"  {_DIM}Tokens saved per day:{_RESET}")
        sparkline = _render_sparkline(daily_tokens, width=min(len(daily), 60))
        lines.append(f"  {sparkline}")

        # Date labels (first, middle, last)
        if len(daily) >= 3:
            first = daily[0]["day"][5:]  # MM-DD
            mid = daily[len(daily) // 2]["day"][5:]
            last = daily[-1]["day"][5:]
            padding = " " * max(1, (len(daily) // 2) - len(first))
            padding2 = " " * max(1, (len(daily) // 2) - len(mid))
            lines.append(f"  {_DIM}{first}{padding}{mid}{padding2}{last}{_RESET}")
        lines.append("")

        # Daily max/avg
        max_day = max(daily, key=lambda d: d["tokens"])
        avg_tokens = total_tokens // max(len(daily), 1)
        lines.append(f"  {_DIM}Peak:{_RESET} {_format_tokens(max_day['tokens'])} ({max_day['day'][5:]})  "
                     f"{_DIM}Avg:{_RESET} {_format_tokens(avg_tokens)}/day")

    lines.append("")
    lines.append(f"  {_BOLD}{'─' * 60}{_RESET}")

    # ── Provider breakdown ──────────────────────────────────────────────
    lines.append("")
    lines.append(f"  {_BOLD}BREAKDOWN{_RESET}")
    lines.append(f"  {_DIM}{'Provider':<20} {'Calls':>6} {'Tokens':>10} {'Gross $':>10} {'Net $':>10}{_RESET}")
    lines.append(f"  {_DIM}{'─' * 58}{_RESET}")

    max_tokens = max((b["tokens"] for b in breakdown), default=1)

    for b in breakdown:
        prov = b["provider"]
        color = _PROVIDER_COLORS.get(prov, _DIM)
        bar = _render_bar(b["tokens"], max_tokens, width=10, color=color)
        is_free = prov in ("ollama", "codex", "gemini_cli", "cache")
        free_tag = f" {_GREEN}FREE{_RESET}" if is_free else ""

        lines.append(
            f"  {color}{prov:<18}{_RESET} "
            f"{b['calls']:>6} "
            f"{_format_tokens(b['tokens']):>10} "
            f"{_GREEN}${b['saved']:.4f}{_RESET}{'':<6} "
            f"${b['saved'] - (classifier_overhead * b['calls'] / max(total_calls, 1)):+.4f}{_RESET}"
            f"{free_tag}"
        )
        lines.append(f"  {bar}")

    lines.append(f"  {_DIM}{'─' * 58}{_RESET}")

    # Classifier overhead
    lines.append(f"  {_YELLOW}Classifier overhead:{_RESET} {_YELLOW}-${classifier_overhead:.4f}{_RESET}")
    lines.append(f"  {_BOLD}{_net_col}NET SAVED:{_RESET} {_BOLD}{_net_col}${net_total_saved:.4f}{_RESET} "
                 f"({_format_tokens(total_tokens)} tokens via cheaper models)")

    # ── Subscription notice ─────────────────────────────────────────────
    if is_subscription:
        lines.append("")
        lines.append(f"  {_YELLOW}┌─ SUBSCRIPTION MODE ────────────────────────────────┐{_RESET}")
        lines.append(f"  {_YELLOW}│{_RESET} You're on a flat-rate Claude plan.                 {_YELLOW}│{_RESET}")
        lines.append(f"  {_YELLOW}│{_RESET} Dollar savings = vs frontier baseline (reference).  {_YELLOW}│{_RESET}")
        lines.append(f"  {_YELLOW}│{_RESET} Real value = {_BOLD}quota freed{_RESET} for complex tasks.          {_YELLOW}│{_RESET}")
        lines.append(f"  {_YELLOW}│{_RESET} {_format_tokens(total_tokens)} tokens handled by cheaper models      {_YELLOW}│{_RESET}")
        lines.append(f"  {_YELLOW}│{_RESET} = quota preserved for Opus-class work.              {_YELLOW}│{_RESET}")
        lines.append(f"  {_YELLOW}└────────────────────────────────────────────────────┘{_RESET}")

    # ── Routing distribution ────────────────────────────────────────────
    lines.append("")
    lines.append(f"  {_BOLD}ROUTING DISTRIBUTION{_RESET}")
    lines.append("")

    for b in breakdown:
        prov = b["provider"]
        color = _PROVIDER_COLORS.get(prov, _DIM)
        pct = (b["calls"] / max(total_calls, 1)) * 100
        bar_width = int(pct / 100 * 30)
        bar = f"{color}{_FULL_BLOCK * bar_width}{_RESET}"
        lines.append(f"  {bar} {prov:<14} {pct:>4.0f}%  {_DIM}({b['calls']} calls){_RESET}")

    lines.append("")
    lines.append(f"{_BOLD}╚══════════════════════════════════════════════════════════════╝{_RESET}")

    return "\n".join(lines)


async def llm_savings_dashboard(
    ctx: Context,
    window: str = "14d",
    baseline: str = "opus",
) -> str:
    """Savings dashboard with time-series, token metrics, and colored bars.

    Shows routing savings over time with per-provider breakdown,
    classifier overhead subtracted, and billing-mode-aware messaging.

    Args:
        window: Time window — "14d" (default), "30d", "3m", "1y", "all".
        baseline: Cost baseline — "opus" (default, matches host model).
    """
    since_sql = _window_to_sql(window)
    label = _window_label(window)

    daily = await _query_daily_savings(since_sql, baseline)
    breakdown = await _query_provider_breakdown(since_sql, baseline)
    overhead = await _query_classifier_overhead(since_sql)

    # Detect subscription mode
    is_subscription = os.getenv("LLM_ROUTER_CLAUDE_SUBSCRIPTION", "").lower() in ("true", "1", "yes")

    if not daily and not breakdown:
        return (
            f"{_YELLOW}No routing data found for {label}.{_RESET}\n"
            f"Route some prompts first: ask a question and let the router handle it.\n"
            f"Data is stored in ~/.llm-router/usage.db"
        )

    return _render_dashboard(daily, breakdown, overhead, label, baseline, is_subscription)


def register(mcp, should_register=None) -> None:
    """Register dashboard tools with the MCPServer instance."""
    gate = should_register or (lambda _: True)
    if gate("llm_savings_dashboard"):
        mcp.tool()(llm_savings_dashboard)
