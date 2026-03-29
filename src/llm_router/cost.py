"""Cost tracking with SQLite persistence.

Stores two categories of usage data in a local SQLite database:
1. **External LLM usage** (``usage`` table): Every call routed through LiteLLM,
   with model, tokens, cost, latency, and routing profile.
2. **Claude Code usage** (``claude_usage`` table): Token consumption by Claude
   Code models, with savings calculated against an Opus baseline.

The database uses WAL journal mode for concurrent read performance and applies
schema migrations idempotently on every connection.
"""

from __future__ import annotations

import aiosqlite

from llm_router.config import get_config
from llm_router.types import (
    LLMResponse, MODEL_COST_PER_1K, MODEL_SPEED_TPS,
    RoutingProfile, TaskType, colorize_model,
)

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
"""Schema for the ``usage`` table tracking external LLM calls. Each row captures
a single LiteLLM API call with its routing context (task_type, profile) and
outcome (tokens, cost, latency, success flag)."""

CREATE_CLAUDE_USAGE_TABLE = """
CREATE TABLE IF NOT EXISTS claude_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT DEFAULT (datetime('now')),
    model TEXT NOT NULL,
    tokens_used INTEGER NOT NULL,
    complexity TEXT NOT NULL,
    cost_saved_usd REAL NOT NULL DEFAULT 0,
    time_saved_sec REAL NOT NULL DEFAULT 0
)
"""
"""Schema for the ``claude_usage`` table tracking Claude Code model token
consumption. Includes computed savings columns comparing actual model cost/speed
against an Opus baseline."""


MIGRATE_CLAUDE_USAGE_ADD_SAVINGS = [
    "ALTER TABLE claude_usage ADD COLUMN cost_saved_usd REAL NOT NULL DEFAULT 0",
    "ALTER TABLE claude_usage ADD COLUMN time_saved_sec REAL NOT NULL DEFAULT 0",
]
"""Idempotent migration statements that add savings columns to older databases.
Each statement is wrapped in a try/except so it silently succeeds if the column
already exists. This avoids needing a formal migration framework."""


async def _get_db() -> aiosqlite.Connection:
    """Open (or create) the SQLite database and apply all migrations.

    Creates the parent directory if needed, enables WAL journal mode for
    better concurrent read performance, creates both tables if they don't
    exist, and runs all idempotent ALTER TABLE migrations.

    Returns:
        An open aiosqlite connection. Caller is responsible for closing it.
    """
    config = get_config()
    config.llm_router_db_path.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(str(config.llm_router_db_path))
    # WAL mode allows concurrent readers while a writer is active
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute(CREATE_TABLE)
    await db.execute(CREATE_CLAUDE_USAGE_TABLE)
    # Migrate: add savings columns if missing
    for stmt in MIGRATE_CLAUDE_USAGE_ADD_SAVINGS:
        try:
            await db.execute(stmt)
        except Exception:
            pass  # column already exists
    await db.commit()
    return db


async def log_usage(
    response: LLMResponse,
    task_type: TaskType,
    profile: RoutingProfile,
    success: bool = True,
) -> None:
    """Persist a completed external LLM call to the usage database.

    Called after every LiteLLM API call (successful or failed) to maintain
    a complete audit trail for cost reporting and provider analytics.

    Args:
        response: The LLMResponse from the completed call, containing model,
            provider, token counts, cost, and latency.
        task_type: The classified task type (e.g. code, research, analysis).
        profile: The active routing profile (e.g. balanced, speed, quality).
        success: Whether the call completed successfully. Failed calls are
            still logged for observability but flagged with success=0.
    """
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
    """Get total USD spent on external LLMs in the current calendar month.

    Returns:
        Total spend as a float. Returns 0.0 if no usage data exists.
    """
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
    """Build a human-readable usage summary with per-model and per-profile breakdowns.

    Args:
        period: Time window to summarize. One of ``"today"``, ``"week"``
            (last 7 days), ``"month"`` (last 30 days), or ``"all"`` (lifetime).

    Returns:
        A multi-line markdown-formatted string with total calls, tokens, cost,
        average latency, and breakdowns by model and routing profile.
        Returns a "no data" message if no usage exists for the period.
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


def calc_savings(model: str, tokens_used: int) -> tuple[float, float]:
    """Calculate cost and time savings compared to always using Opus.

    The Opus model serves as the quality ceiling baseline. Any cheaper/faster
    model that was used instead yields savings. If the model IS Opus, savings
    are zero by definition.

    Args:
        model: The model that was actually used (e.g. "haiku", "sonnet").
        tokens_used: Total tokens consumed by this call.

    Returns:
        A tuple of (cost_saved_usd, time_saved_sec), both clamped to >= 0.
        Cost savings use per-1K-token rates from ``MODEL_COST_PER_1K``.
        Time savings use tokens-per-second rates from ``MODEL_SPEED_TPS``.
    """
    baseline = "opus"
    if model == baseline:
        return 0.0, 0.0

    tokens_k = tokens_used / 1000
    actual_cost = tokens_k * MODEL_COST_PER_1K.get(model, 0)
    opus_cost = tokens_k * MODEL_COST_PER_1K[baseline]
    cost_saved = opus_cost - actual_cost

    actual_time = tokens_used / MODEL_SPEED_TPS.get(model, 120)
    opus_time = tokens_used / MODEL_SPEED_TPS[baseline]
    time_saved = opus_time - actual_time

    return max(0.0, cost_saved), max(0.0, time_saved)


async def log_claude_usage(model: str, tokens_used: int, complexity: str) -> dict:
    """Log a Claude Code model invocation and its savings vs. Opus.

    Calculates cost and time savings, persists the record to the
    ``claude_usage`` table, and returns the savings for immediate display.

    Args:
        model: The Claude model used (e.g. "haiku", "sonnet", "opus").
        tokens_used: Total tokens consumed.
        complexity: Classified complexity level (e.g. "simple", "moderate",
            "complex") for analytics grouping.

    Returns:
        Dict with ``cost_saved_usd`` and ``time_saved_sec`` for this call.
    """
    cost_saved, time_saved = calc_savings(model, tokens_used)

    db = await _get_db()
    try:
        await db.execute(
            "INSERT INTO claude_usage (model, tokens_used, complexity, cost_saved_usd, time_saved_sec) "
            "VALUES (?, ?, ?, ?, ?)",
            (model, tokens_used, complexity, cost_saved, time_saved),
        )
        await db.commit()
    finally:
        await db.close()

    return {"cost_saved_usd": cost_saved, "time_saved_sec": time_saved}


async def get_daily_claude_tokens() -> int:
    """Get the total number of Claude Code tokens consumed today (UTC).

    Returns:
        Token count as an integer. Returns 0 if no usage today.
    """
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
    """Get today's Claude Code token usage broken down by model.

    Returns:
        Dict mapping model name (e.g. "haiku") to total tokens used today.
        Empty dict if no usage exists.
    """
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


async def get_savings_summary(period: str = "today") -> dict:
    """Get cumulative savings for a given time period.

    Queries the ``claude_usage`` table for aggregate savings and a per-model
    breakdown. Handles backward compatibility with older databases that may
    lack the savings columns by catching query errors gracefully.

    Args:
        period: Time window. One of ``"today"``, ``"week"`` (last 7 days),
            ``"month"`` (last 30 days), or ``"all"`` (lifetime).

    Returns:
        Dict with keys: ``total_calls``, ``total_tokens``, ``cost_saved_usd``,
        ``time_saved_sec``, and ``by_model`` (a nested dict with per-model
        calls, tokens, cost_saved, and time_saved). Returns zeroed-out values
        if no data exists or the query fails.
    """
    where = {
        "today": "WHERE date(timestamp) = date('now')",
        "week": "WHERE timestamp >= datetime('now', '-7 days')",
        "month": "WHERE timestamp >= datetime('now', '-30 days')",
        "all": "",
    }.get(period, "")

    db = await _get_db()
    try:
        # Check if columns exist (backward compat for old DB schemas)
        try:
            cursor = await db.execute(
                f"SELECT COUNT(*), COALESCE(SUM(tokens_used), 0), "
                f"COALESCE(SUM(cost_saved_usd), 0), COALESCE(SUM(time_saved_sec), 0) "
                f"FROM claude_usage {where}"
            )
        except Exception:
            return {"total_calls": 0, "total_tokens": 0, "cost_saved_usd": 0.0,
                    "time_saved_sec": 0.0, "by_model": {}}

        row = await cursor.fetchone()
        if not row or row[0] == 0:
            return {"total_calls": 0, "total_tokens": 0, "cost_saved_usd": 0.0,
                    "time_saved_sec": 0.0, "by_model": {}}

        total_calls, total_tokens, cost_saved, time_saved = row

        # Per-model breakdown
        cursor = await db.execute(
            f"SELECT model, COUNT(*), SUM(tokens_used), "
            f"COALESCE(SUM(cost_saved_usd), 0), COALESCE(SUM(time_saved_sec), 0) "
            f"FROM claude_usage {where} GROUP BY model ORDER BY SUM(tokens_used) DESC"
        )
        rows = await cursor.fetchall()
        by_model = {
            model: {
                "calls": calls, "tokens": int(tokens),
                "cost_saved": float(saved), "time_saved": float(tsaved),
            }
            for model, calls, tokens, saved, tsaved in rows
        }

        return {
            "total_calls": int(total_calls),
            "total_tokens": int(total_tokens),
            "cost_saved_usd": float(cost_saved),
            "time_saved_sec": float(time_saved),
            "by_model": by_model,
        }
    finally:
        await db.close()
