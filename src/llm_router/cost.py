"""Cost tracking with SQLite persistence."""

from __future__ import annotations

import aiosqlite

from llm_router.config import get_config
from llm_router.types import LLMResponse, RoutingProfile, TaskType, colorize_model

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT DEFAULT (datetime('now')),
    model TEXT NOT NULL,
    provider TEXT NOT NULL,
    task_type TEXT NOT NULL,
    profile TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    latency_ms REAL NOT NULL,
    success INTEGER NOT NULL DEFAULT 1
)
"""

CREATE_CLAUDE_USAGE_TABLE = """
CREATE TABLE IF NOT EXISTS claude_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT DEFAULT (datetime('now')),
    model TEXT NOT NULL,
    tokens_used INTEGER NOT NULL,
    complexity TEXT NOT NULL
)
"""


async def _get_db() -> aiosqlite.Connection:
    config = get_config()
    config.llm_router_db_path.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(str(config.llm_router_db_path))
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute(CREATE_TABLE)
    await db.execute(CREATE_CLAUDE_USAGE_TABLE)
    await db.commit()
    return db


async def log_usage(
    response: LLMResponse,
    task_type: TaskType,
    profile: RoutingProfile,
    success: bool = True,
) -> None:
    """Log a completed LLM call to the usage database."""
    db = await _get_db()
    try:
        await db.execute(
            """INSERT INTO usage (model, provider, task_type, profile,
               input_tokens, output_tokens, cost_usd, latency_ms, success)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                response.model,
                response.provider,
                task_type.value,
                profile.value,
                response.input_tokens,
                response.output_tokens,
                response.cost_usd,
                response.latency_ms,
                1 if success else 0,
            ),
        )
        await db.commit()
    finally:
        await db.close()


async def get_monthly_spend() -> float:
    """Get total USD spent in the current calendar month."""
    db = await _get_db()
    try:
        cursor = await db.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM usage "
            "WHERE timestamp >= datetime('now', 'start of month')"
        )
        row = await cursor.fetchone()
        return float(row[0]) if row else 0.0
    finally:
        await db.close()


async def get_usage_summary(period: str = "today") -> str:
    """Get a human-readable usage summary.

    Args:
        period: "today", "week", "month", or "all"
    """
    where = {
        "today": "WHERE date(timestamp) = date('now')",
        "week": "WHERE timestamp >= datetime('now', '-7 days')",
        "month": "WHERE timestamp >= datetime('now', '-30 days')",
        "all": "",
    }.get(period, "")

    db = await _get_db()
    try:
        # Total summary
        cursor = await db.execute(
            f"""SELECT COUNT(*), SUM(input_tokens), SUM(output_tokens),
                SUM(cost_usd), AVG(latency_ms)
                FROM usage {where}"""
        )
        row = await cursor.fetchone()
        if not row or row[0] == 0:
            return f"No usage data for period: {period}"

        total_calls, total_in, total_out, total_cost, avg_latency = row
        lines = [
            f"## Usage Summary ({period})",
            f"Calls: {total_calls}",
            f"Tokens: {total_in:,} in + {total_out:,} out = {total_in + total_out:,} total",
            f"Cost: ${total_cost:.4f}",
            f"Avg latency: {avg_latency:.0f}ms",
            "",
            "### By Model",
        ]

        # Per-model breakdown
        cursor = await db.execute(
            f"""SELECT model, COUNT(*), SUM(cost_usd), SUM(input_tokens + output_tokens)
                FROM usage {where} GROUP BY model ORDER BY SUM(cost_usd) DESC"""
        )
        rows = await cursor.fetchall()
        for model, calls, cost, tokens in rows:
            lines.append(f"- {colorize_model(model)}: {calls} calls, ${cost:.4f}, {tokens:,} tokens")

        lines.append("")
        lines.append("### By Profile")

        # Per-profile breakdown
        cursor = await db.execute(
            f"""SELECT profile, COUNT(*), SUM(cost_usd)
                FROM usage {where} GROUP BY profile ORDER BY SUM(cost_usd) DESC"""
        )
        rows = await cursor.fetchall()
        for profile, calls, cost in rows:
            lines.append(f"- {profile}: {calls} calls, ${cost:.4f}")

        return "\n".join(lines)
    finally:
        await db.close()


# ── Claude Code token tracking ───────────────────────────────────────────────


async def log_claude_usage(model: str, tokens_used: int, complexity: str) -> None:
    """Log Claude Code model usage (haiku/sonnet/opus) for budget tracking."""
    db = await _get_db()
    try:
        await db.execute(
            "INSERT INTO claude_usage (model, tokens_used, complexity) VALUES (?, ?, ?)",
            (model, tokens_used, complexity),
        )
        await db.commit()
    finally:
        await db.close()


async def get_daily_claude_tokens() -> int:
    """Get total Claude Code tokens used today."""
    db = await _get_db()
    try:
        cursor = await db.execute(
            "SELECT COALESCE(SUM(tokens_used), 0) FROM claude_usage "
            "WHERE date(timestamp) = date('now')"
        )
        row = await cursor.fetchone()
        return int(row[0]) if row else 0
    finally:
        await db.close()


async def get_daily_claude_breakdown() -> dict[str, int]:
    """Get today's Claude token usage broken down by model."""
    db = await _get_db()
    try:
        cursor = await db.execute(
            "SELECT model, SUM(tokens_used) FROM claude_usage "
            "WHERE date(timestamp) = date('now') GROUP BY model"
        )
        rows = await cursor.fetchall()
        return {model: int(tokens) for model, tokens in rows}
    finally:
        await db.close()
