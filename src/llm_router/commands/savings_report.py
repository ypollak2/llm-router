"""Detailed savings report command.

Reads the authoritative per-call ledger ``savings_stats`` (written by the
savings_logger hook) as the SINGLE source of truth, so the report can never
disagree with the stored stats. Paid vs free is split by ``external_cost`` (no
double-counting), and the saved amount is the stored ``estimated_claude_cost_saved``
(not a separately-recomputed baseline).

Usage:
    llm_router savings-report              — full report (all time)
    llm_router savings-report --period week — weekly report
    llm_router savings-report --period day  — today only
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

_FREE_PROVIDERS = {"ollama", "codex", "gemini_cli", "openai_compat"}


def _get_db_path() -> Path:
    return Path.home() / ".llm-router" / "usage.db"


def _get_time_filter(period: str = "all") -> tuple[str, tuple]:
    """Return (sql_fragment, params) for the given period."""
    if period == "day":
        cutoff = datetime.now(timezone.utc) - timedelta(days=1)
    elif period == "week":
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    elif period == "month":
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    else:
        return "", ()
    return "AND timestamp > ?", (cutoff.isoformat(),)


def _provider_of(model: str) -> str:
    if "/" in model:
        return model.split("/", 1)[0]
    if model.startswith("claude") or model == "cc":
        return "anthropic"
    return model or "unknown"


def _query(db_path: Path, period: str, *, paid: bool) -> dict:
    """Aggregate savings_stats for paid (external_cost>0) or free (==0) routes."""
    tf_sql, tf_params = _get_time_filter(period)
    cond = "external_cost > 0" if paid else "(external_cost = 0 OR external_cost IS NULL)"
    stats = {"calls": 0, "saved": 0.0, "cost": 0.0, "by_model": {}}
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(  # nosec B608 — cond is a hardcoded literal, not user input
            f"""SELECT COUNT(*) AS calls,
                       COALESCE(SUM(estimated_claude_cost_saved), 0) AS saved,
                       COALESCE(SUM(external_cost), 0) AS cost,
                       model_used AS model
                FROM savings_stats
                WHERE {cond} {tf_sql}
                GROUP BY model_used
                ORDER BY calls DESC""",
            tf_params,
        ).fetchall()
        conn.close()
    except Exception:
        return stats
    for r in rows:
        stats["calls"] += r["calls"]
        stats["saved"] += r["saved"] or 0.0
        stats["cost"] += r["cost"] or 0.0
        stats["by_model"][r["model"] or "unknown"] = {
            "calls": r["calls"], "saved": r["saved"] or 0.0, "cost": r["cost"] or 0.0,
            "provider": _provider_of(r["model"] or "unknown"),
        }
    return stats


def render_savings_report(period: str = "all") -> str:
    db_path = _get_db_path()
    if not db_path.exists():
        return "No usage data found. Start routing prompts to generate data."

    free = _query(db_path, period, paid=False)
    paid = _query(db_path, period, paid=True)
    if not free["calls"] and not paid["calls"]:
        return "No routing data available for this period."

    label = {"day": "Last 24 Hours", "week": "Last 7 Days",
             "month": "Last 30 Days", "all": "All Time"}.get(period, "All Time")
    total_saved = free["saved"] + paid["saved"]
    total_calls = free["calls"] + paid["calls"]

    out = [f"\n╭─ SAVINGS REPORT ─ {label} " + "─" * 34 + "╮", "│"]
    out.append(f"│  Claude quota saved:  ${total_saved:.4f}   across {total_calls} routed call(s)")
    out.append("│")

    def section(title: str, s: dict, free_section: bool) -> None:
        if not s["calls"]:
            return
        spent = "$0.0000" if free_section else f"${s['cost']:.4f}"
        out.append(f"│  {title}")
        out.append(f"│    {s['calls']:>4} calls · saved ${s['saved']:.4f} vs Claude · {spent} spent")
        for model, d in sorted(s["by_model"].items(), key=lambda x: -x[1]["saved"])[:10]:
            out.append(f"│      {model:<26} {d['calls']:>3}×   saved ${d['saved']:.4f}")
        out.append("│")

    section("FREE / LOCAL  (ollama · codex · gemini-cli)", free, True)
    section("PAID EXTERNAL  (gemini · openai · …)", paid, False)

    out.append("│  Note: counts only prompts LLM Router ROUTED (conversation turns). Tokens")
    out.append("│        consumed by downstream agents/tools are not metered here.")
    out.append("╰" + "─" * 60 + "╯")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    argv = argv or []
    period = "all"
    if "--period" in argv:
        i = argv.index("--period")
        if i + 1 < len(argv):
            period = argv[i + 1]
    print(render_savings_report(period))
    return 0
