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

import hashlib
import json
from pathlib import Path

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


CREATE_ROUTING_DECISIONS_TABLE = """
CREATE TABLE IF NOT EXISTS routing_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT DEFAULT (datetime('now')),
    prompt_hash TEXT,
    task_type TEXT,
    profile TEXT,
    classifier_type TEXT,
    classifier_model TEXT,
    classifier_confidence REAL,
    classifier_latency_ms REAL,
    complexity TEXT,
    recommended_model TEXT,
    base_model TEXT,
    was_downshifted INTEGER,
    budget_pct_used REAL,
    quality_mode TEXT,
    final_model TEXT,
    final_provider TEXT,
    success INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cost_usd REAL,
    latency_ms REAL
)
"""
"""Schema for the ``routing_decisions`` table tracking every routing decision
with full classification, recommendation, and outcome data for quality analysis."""

CREATE_SAVINGS_STATS_TABLE = """
CREATE TABLE IF NOT EXISTS savings_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    session_id TEXT NOT NULL,
    task_type TEXT NOT NULL,
    estimated_claude_cost_saved REAL NOT NULL,
    external_cost REAL NOT NULL,
    model_used TEXT NOT NULL,
    host TEXT NOT NULL DEFAULT 'claude_code'
)
"""
"""Schema for the ``savings_stats`` table tracking per-call routing savings.
Each row represents one routed call logged by the PostToolUse hook via JSONL,
then imported into SQLite by the MCP server for lifetime analytics."""

SAVINGS_LOG_PATH = Path.home() / ".llm-router" / "savings_log.jsonl"
"""Path to the JSONL file written by the PostToolUse hook for async import."""


CREATE_SEMANTIC_CACHE_TABLE = """
CREATE TABLE IF NOT EXISTS semantic_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type TEXT NOT NULL,
    embedding TEXT NOT NULL,
    response_content TEXT NOT NULL,
    response_model TEXT NOT NULL,
    response_cost_usd REAL NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
)
"""
"""Schema for the ``semantic_cache`` table. Each row stores a prompt embedding
alongside the cached response, enabling cosine-similarity dedup lookups."""

CREATE_SEMANTIC_CACHE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_semantic_cache_type_time
ON semantic_cache(task_type, created_at DESC)
"""

MIGRATE_CLAUDE_USAGE_ADD_SAVINGS = [
    "ALTER TABLE claude_usage ADD COLUMN cost_saved_usd REAL NOT NULL DEFAULT 0",
    "ALTER TABLE claude_usage ADD COLUMN time_saved_sec REAL NOT NULL DEFAULT 0",
]
"""Idempotent migration statements that add savings columns to older databases.
Each statement is wrapped in a try/except so it silently succeeds if the column
already exists. This avoids needing a formal migration framework."""

MIGRATE_ROUTING_DECISIONS_ADD_FEEDBACK = [
    "ALTER TABLE routing_decisions ADD COLUMN was_good INTEGER",
]
"""Idempotent migration to add user feedback column to routing_decisions."""

MIGRATE_ROUTING_DECISIONS_ADD_REASON = [
    "ALTER TABLE routing_decisions ADD COLUMN reason_code TEXT",
]
"""Idempotent migration to add classifier reasoning text to routing_decisions (v2.2)."""

MIGRATE_USAGE_ADD_SAVINGS = [
    "ALTER TABLE usage ADD COLUMN baseline_model TEXT",
    "ALTER TABLE usage ADD COLUMN potential_cost_usd REAL DEFAULT 0.0",
    "ALTER TABLE usage ADD COLUMN saved_usd REAL DEFAULT 0.0",
    "ALTER TABLE usage ADD COLUMN is_simulated INTEGER DEFAULT 0",
]

MIGRATE_USAGE_ADD_TEAM = [
    "ALTER TABLE usage ADD COLUMN user_id TEXT",
    "ALTER TABLE usage ADD COLUMN project_id TEXT",
]
"""Idempotent migration to add team identity columns (v3.0)."""

MIGRATE_SAVINGS_STATS_ADD_HOST = [
    "ALTER TABLE savings_stats ADD COLUMN host TEXT NOT NULL DEFAULT 'claude_code'",
]
"""Idempotent migration to add host attribution column to savings_stats (v3.1)."""

MIGRATE_ROUTING_DECISIONS_ADD_POLICY = [
    "ALTER TABLE routing_decisions ADD COLUMN policy_applied TEXT",
]

MIGRATE_ADD_CORRELATION_ID = [
    "ALTER TABLE usage ADD COLUMN correlation_id TEXT",
    "ALTER TABLE routing_decisions ADD COLUMN correlation_id TEXT",
]

MIGRATE_ADD_CACHE_METRICS = [
    "ALTER TABLE usage ADD COLUMN cache_hit INTEGER DEFAULT 0",
    "ALTER TABLE usage ADD COLUMN cache_savings_usd REAL DEFAULT 0.0",
]
"""Idempotent migration to add prompt caching metrics (v5.7)."""

MIGRATE_ROUTING_DECISIONS_ADD_JUDGE_SCORE = [
    "ALTER TABLE routing_decisions ADD COLUMN judge_score REAL DEFAULT NULL",
]
"""Idempotent migration to add judge_score for LLM-as-Judge quality evaluation (v5.8)."""

MIGRATE_ROUTING_DECISIONS_ADD_COMPLEXITY_TRACKING = [
    "ALTER TABLE routing_decisions ADD COLUMN requested_complexity TEXT",
    "ALTER TABLE routing_decisions ADD COLUMN complexity_downgraded INTEGER DEFAULT 0",
]
"""Idempotent migration to track pressure-based complexity downgrades (v5.9)."""
"""Idempotent migration to add policy audit column to routing_decisions (v3.2).

policy_applied: JSON string of policy actions, e.g.
  '{"blocked": ["openai/gpt-4o"], "source": "org-policy.yaml"}'
"""

CREATE_CORRECTIONS_TABLE = """
CREATE TABLE IF NOT EXISTS corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT DEFAULT (datetime('now')),
    original_tool TEXT NOT NULL,
    original_model TEXT NOT NULL,
    corrected_tool TEXT NOT NULL,
    corrected_model TEXT,
    reason TEXT,
    session_id TEXT
)
"""
"""Schema for the ``corrections`` table storing user-initiated reroute decisions.

Each row is written by ``llm_reroute``. The classifier reads this table to
lower confidence scores for repeatedly corrected tools, providing a basic
feedback loop between user corrections and routing quality.
"""

CREATE_COMPRESSION_STATS_TABLE = """
CREATE TABLE IF NOT EXISTS compression_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT DEFAULT (datetime('now')),
    session_id TEXT,
    command TEXT NOT NULL,
    layer TEXT NOT NULL,
    original_tokens INTEGER NOT NULL,
    compressed_tokens INTEGER NOT NULL,
    compression_ratio REAL NOT NULL,
    tokens_saved INTEGER NOT NULL,
    strategy TEXT
)
"""
"""Schema for the ``compression_stats`` table tracking token compression metrics.

Each row represents one compression operation (command output or response).
- layer: 'rtk' for command output, 'token-savior' for response compression
- command: The shell command (e.g., 'git log --oneline') or 'response'
- compression_ratio: compressed_tokens / original_tokens (0.0-1.0)
- strategy: Which filter was applied (e.g., 'git:log', 'docker:ps', 'generic')
"""

MIGRATE_ADD_COMPRESSION_STATS = [
    "CREATE TABLE IF NOT EXISTS compression_stats (id INTEGER PRIMARY KEY, timestamp TEXT DEFAULT (datetime('now')), session_id TEXT, command TEXT NOT NULL, layer TEXT NOT NULL, original_tokens INTEGER NOT NULL, compressed_tokens INTEGER NOT NULL, compression_ratio REAL NOT NULL, tokens_saved INTEGER NOT NULL, strategy TEXT)",
]
"""Idempotent migration to add compression tracking table (v6.2)."""

"""Idempotent migration to add per-call savings columns to usage table.

baseline_model:     Model that would have been used without routing (e.g. claude-sonnet)
potential_cost_usd: Estimated cost if baseline_model had handled the call
saved_usd:          potential_cost_usd - actual cost_usd (negative = routing cost money)
is_simulated:       1 for dry-run test calls (llm-router test), 0 for real calls
"""


async def _column_exists(db: aiosqlite.Connection, table: str, column: str) -> bool:
    """Return True if *column* exists in *table* (uses SQLite PRAGMA, no exceptions)."""
    cursor = await db.execute(
        f"SELECT name FROM pragma_table_info('{table}') WHERE name = ?", (column,)
    )
    return await cursor.fetchone() is not None


async def _safe_migrate(db: aiosqlite.Connection, stmt: str) -> None:
    """Run an ALTER TABLE statement only if the target column does not yet exist.

    Parses the column name from the statement so the check is explicit rather
    than relying on SQLite raising OperationalError for duplicate columns.
    Falls back to try/except for any statement that doesn't match the expected
    'ALTER TABLE <t> ADD COLUMN <col>' form.
    """
    import re
    m = re.match(
        r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+(\w+)", stmt, re.IGNORECASE
    )
    if m:
        table, column = m.group(1), m.group(2)
        if await _column_exists(db, table, column):
            return  # already migrated — skip
    try:
        await db.execute(stmt)
    except Exception:
        pass  # last-resort fallback for non-standard ALTER forms


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
    await db.execute(CREATE_ROUTING_DECISIONS_TABLE)
    await db.execute(CREATE_SAVINGS_STATS_TABLE)
    await db.execute(CREATE_SEMANTIC_CACHE_TABLE)
    await db.execute(CREATE_SEMANTIC_CACHE_INDEX)
    await db.execute(CREATE_CORRECTIONS_TABLE)
    # Performance indices — `IF NOT EXISTS` makes these idempotent.
    # These prevent full-table scans on the monthly-spend queries that fire
    # on every routing decision once the tables grow beyond ~10k rows.
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_usage_provider_ts ON usage(provider, timestamp)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_usage_model_ts ON usage(model, timestamp)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_routing_ts ON routing_decisions(timestamp)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_routing_model ON routing_decisions(final_model)"
    )
    # Run all migrations idempotently — _safe_migrate checks column existence
    # before executing, so re-running on an existing DB is always safe.
    all_migrations = (
        MIGRATE_CLAUDE_USAGE_ADD_SAVINGS
        + MIGRATE_ROUTING_DECISIONS_ADD_FEEDBACK
        + MIGRATE_ROUTING_DECISIONS_ADD_REASON
        + MIGRATE_USAGE_ADD_SAVINGS
        + MIGRATE_USAGE_ADD_TEAM
        + MIGRATE_SAVINGS_STATS_ADD_HOST
        + MIGRATE_ROUTING_DECISIONS_ADD_POLICY
        + MIGRATE_ADD_CORRELATION_ID
        + MIGRATE_ADD_CACHE_METRICS
        + MIGRATE_ROUTING_DECISIONS_ADD_JUDGE_SCORE
        + MIGRATE_ROUTING_DECISIONS_ADD_COMPLEXITY_TRACKING
    )
    for stmt in all_migrations:
        await _safe_migrate(db, stmt)
    await db.commit()
    return db


def _get_team_identity() -> tuple[str, str]:
    """Return (user_id, project_id) for the current process context.

    Cached per-process to avoid repeated git subprocess calls.
    Returns ("", "") when team identity is not configured.
    """
    try:
        from llm_router.team import get_project_id, get_user_id
        from llm_router.config import get_config
        cfg = get_config()
        uid = get_user_id(override=cfg.llm_router_user_id)
        pid = get_project_id()
        return uid, pid
    except Exception:
        return "", ""


async def log_usage(
    response: LLMResponse,
    task_type: TaskType,
    profile: RoutingProfile,
    success: bool = True,
    correlation_id: str | None = None,
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
        correlation_id: Optional hex ID linking this DB row to the structlog
            trace for the same routing call (first 8 chars of UUID4).
    """
    user_id, project_id = _get_team_identity()
    db = await _get_db()
    try:
        await db.execute(
            """INSERT INTO usage (model, provider, task_type, profile,
               input_tokens, output_tokens, cost_usd, latency_ms, success,
               user_id, project_id, correlation_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                user_id or None,
                project_id or None,
                correlation_id,
            ),
        )
        await db.commit()
    finally:
        await db.close()


async def log_cc_hint(task_type: str, model: str) -> None:
    """Log a Claude Code subscription model hint to the usage table.

    Called when _subscription_hint() returns a CC-MODE directive instead of
    making an external API call. Records the model tier that was suggested so
    the session-end summary can report per-model CC call counts.

    provider='subscription' distinguishes these rows from external API calls.
    """
    db = await _get_db()
    try:
        await db.execute(
            """INSERT INTO usage (model, provider, task_type, profile,
               input_tokens, output_tokens, cost_usd, latency_ms, success)
               VALUES (?, 'subscription', ?, 'subscription', 0, 0, 0.0, 0.0, 1)""",
            (model, task_type),
        )
        await db.commit()
    except Exception:
        pass
    finally:
        await db.close()


async def log_correction(
    original_tool: str,
    original_model: str,
    corrected_tool: str,
    corrected_model: str = "",
    reason: str = "",
    session_id: str = "",
) -> None:
    """Record a user-initiated reroute correction for feedback-loop learning.

    Called by ``llm_reroute`` whenever the user overrides a routing decision.
    The ``get_correction_count`` function reads these records to lower routing
    confidence for repeatedly corrected tools.

    Args:
        original_tool: The tool the router chose (e.g. "llm_query").
        original_model: The model selected for that tool.
        corrected_tool: The tool the user wants to use instead.
        corrected_model: Optional override model for the corrected tool.
        reason: Optional user-provided explanation.
        session_id: Session identifier for grouping corrections.
    """
    db = await _get_db()
    try:
        await db.execute(
            """
            INSERT INTO corrections
                (original_tool, original_model, corrected_tool, corrected_model, reason, session_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (original_tool, original_model, corrected_tool, corrected_model, reason, session_id),
        )
        await db.commit()
    finally:
        await db.close()


async def get_correction_count(tool: str) -> int:
    """Return how many times the given tool has been overridden by the user.

    Used by ``llm_route`` explain mode to compute routing confidence:
    each correction lowers confidence by 15 percentage points.

    Args:
        tool: MCP tool name (e.g. "llm_query", "llm_code").

    Returns:
        Number of user corrections targeting this tool as the original.
    """
    db = await _get_db()
    try:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM corrections WHERE original_tool = ?",
            (tool,),
        )
        row = await cursor.fetchone()
        return int(row[0]) if row else 0
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


async def get_daily_spend() -> float:
    """Get total USD spent on external LLMs today (UTC calendar day).

    Returns:
        Total spend as a float. Returns 0.0 if no usage data exists.
    """
    db = await _get_db()
    try:
        cursor = await db.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM usage "
            "WHERE timestamp >= datetime('now', 'start of day')"
        )
        row = await cursor.fetchone()
        return float(row[0]) if row else 0.0
    finally:
        await db.close()


async def get_daily_spend_by_task_type(task_type: str) -> float:
    """Get total USD spent on external LLMs today for a specific task type.

    Args:
        task_type: Task type string (e.g., 'query', 'code', 'research', 'generate', 'analyze').

    Returns:
        Total spend for that task type as a float. Returns 0.0 if no usage data exists.
    """
    db = await _get_db()
    try:
        cursor = await db.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM usage "
            "WHERE timestamp >= datetime('now', 'start of day') AND task_type = ?",
            (task_type,),
        )
        row = await cursor.fetchone()
        return float(row[0]) if row else 0.0
    finally:
        await db.close()


def fire_budget_alert(title: str, message: str) -> None:
    """Send a desktop notification for budget threshold events.

    Platform support:
    - **macOS**: ``osascript`` (built-in, no extra deps).
    - **Linux**: ``notify-send`` (libnotify, typically pre-installed on GNOME/KDE).
    - **Windows**: ``win10toast`` if installed, falls back to a log warning.

    Silently swallowed when no notification mechanism is available.

    Args:
        title: Notification title shown in bold.
        message: Notification body text.
    """
    import subprocess
    import sys

    try:
        if sys.platform == "darwin":
            script = (
                f'display notification "{message}" '
                f'with title "{title}" '
                f'sound name "Glass"'
            )
            subprocess.run(["osascript", "-e", script], timeout=3, capture_output=True)
        elif sys.platform.startswith("linux"):
            subprocess.run(
                ["notify-send", "--urgency=normal", title, message],
                timeout=3, capture_output=True,
            )
        elif sys.platform == "win32":
            try:
                from win10toast import ToastNotifier  # type: ignore[import]
                ToastNotifier().show_toast(title, message, duration=5, threaded=True)
            except ImportError:
                import logging
                logging.getLogger("llm_router").warning(
                    "Budget alert: %s — %s (install win10toast for desktop notifications)",
                    title, message,
                )
    except Exception:
        pass  # notification is best-effort — never block routing


async def rate_routing_decision(decision_id: int | None, good: bool) -> int | None:
    """Record user feedback (thumbs up/down) on a routing decision.

    Updates the ``was_good`` column on the specified row. If ``decision_id``
    is None, rates the most recent routing decision.

    Args:
        decision_id: Row ID in ``routing_decisions``, or None for the latest.
        good: True = good routing choice; False = bad routing choice.

    Returns:
        The row ID that was updated, or None if no matching row was found.
    """
    db = await _get_db()
    try:
        if decision_id is None:
            cursor = await db.execute(
                "SELECT id FROM routing_decisions ORDER BY id DESC LIMIT 1"
            )
            row = await cursor.fetchone()
            if not row:
                return None
            decision_id = row[0]

        await db.execute(
            "UPDATE routing_decisions SET was_good = ? WHERE id = ?",
            (1 if good else 0, decision_id),
        )
        await db.commit()
        # Confirm the row existed
        cursor = await db.execute(
            "SELECT id FROM routing_decisions WHERE id = ?", (decision_id,)
        )
        return decision_id if await cursor.fetchone() else None
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


# ── Routing decision logging ─────────────────────────────────────────────────


def _prompt_hash(prompt: str) -> str:
    """SHA-256 hash of the first 500 characters of a prompt.

    Provides a stable, privacy-preserving identifier for correlating
    repeated prompts without storing raw text.

    Args:
        prompt: The raw prompt text.

    Returns:
        Hex-encoded SHA-256 digest of prompt[:500].
    """
    return hashlib.sha256(prompt[:500].encode("utf-8")).hexdigest()


async def log_routing_decision(
    *,
    prompt: str,
    task_type: str,
    profile: str,
    classifier_type: str,
    classifier_model: str | None,
    classifier_confidence: float,
    classifier_latency_ms: float,
    complexity: str,
    recommended_model: str,
    base_model: str,
    was_downshifted: bool,
    budget_pct_used: float,
    quality_mode: str,
    final_model: str,
    final_provider: str,
    success: bool,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
    latency_ms: float,
    reason_code: str | None = None,
    correlation_id: str | None = None,
    response: str | None = None,
    requested_complexity: str | None = None,
) -> None:
    """Persist a complete routing decision to the routing_decisions table.

    Captures the full lifecycle of a routing decision: classification input,
    model selection reasoning, and execution outcome. Used by
    ``get_quality_report`` for analytics.

    Args:
        prompt: Raw prompt text (hashed before storage).
        task_type: Classified task type (e.g. "query", "code").
        profile: Active routing profile (e.g. "balanced", "budget").
        classifier_type: How classification was done (heuristic/llm/cached/hook).
        classifier_model: Which model classified, or None for non-LLM classifiers.
        classifier_confidence: Classifier confidence (0.0-1.0).
        classifier_latency_ms: Classification latency in milliseconds.
        complexity: Classified complexity (simple/moderate/complex) — final value used.
        recommended_model: Model recommended by the selector.
        base_model: What complexity alone would pick (before budget adjustment).
        was_downshifted: Whether budget pressure caused a cheaper model.
        budget_pct_used: Fraction of budget consumed at decision time.
        quality_mode: Active quality mode (best/balanced/conserve).
        final_model: The model that actually executed the request.
        final_provider: Provider of the final model.
        success: Whether the call completed successfully.
        input_tokens: Input tokens consumed.
        requested_complexity: Original complexity before pressure downgrade (for mismatch tracking).
            If omitted, defaults to complexity (no downgrade detected).
        output_tokens: Output tokens generated.
        cost_usd: Total cost of the LLM call.
        latency_ms: Total latency of the LLM call.
    """
    db = await _get_db()
    try:
        # Track complexity mismatch: if requested_complexity differs from final complexity,
        # a pressure downgrade occurred (e.g., complex→moderate when budget high)
        complexity_downgraded = 1 if requested_complexity and requested_complexity != complexity else 0
        await db.execute(
            """INSERT INTO routing_decisions
               (prompt_hash, task_type, profile, classifier_type, classifier_model,
                classifier_confidence, classifier_latency_ms, complexity,
                recommended_model, base_model, was_downshifted, budget_pct_used,
                quality_mode, final_model, final_provider, success,
                input_tokens, output_tokens, cost_usd, latency_ms, reason_code,
                correlation_id, requested_complexity, complexity_downgraded)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                _prompt_hash(prompt),
                task_type,
                profile,
                classifier_type,
                classifier_model,
                classifier_confidence,
                classifier_latency_ms,
                complexity,
                recommended_model,
                base_model,
                1 if was_downshifted else 0,
                budget_pct_used,
                quality_mode,
                final_model,
                final_provider,
                1 if success else 0,
                input_tokens,
                output_tokens,
                cost_usd,
                latency_ms,
                reason_code,
                correlation_id,
                requested_complexity,
                complexity_downgraded,
            ),
        )
        await db.commit()

        # Fire-and-forget judge evaluation for successful calls with response
        if success and response:
            try:
                from llm_router.judge import evaluate_response_async
                # Get the ID of the row we just inserted
                cursor = await db.execute("SELECT last_insert_rowid()")
                row_id_result = await cursor.fetchone()
                routing_decision_id = row_id_result[0] if row_id_result else None

                # Trigger background judge evaluation (non-blocking)
                await evaluate_response_async(
                    prompt=prompt,
                    response=response,
                    task_type=task_type,
                    routing_decision_id=routing_decision_id,
                )
            except Exception:
                pass  # Silent failure — judge is optional enhancement
    finally:
        await db.close()


async def get_quality_report(days: int = 7) -> dict:
    """Build a quality analytics report from routing decision history.

    Aggregates routing decisions over the given time window into a summary
    dict with breakdowns by classifier type, task type, and model.

    Args:
        days: Number of days to include in the report (default 7).

    Returns:
        Dict with keys: ``total_decisions``, ``by_classifier``, ``by_task_type``,
        ``avg_confidence``, ``downshift_rate``, ``avg_latency_ms``,
        ``total_cost_usd``, ``total_tokens``, ``success_rate``, ``by_model``.
        Returns zeroed values if no data exists.
    """
    where = f"WHERE timestamp >= datetime('now', '-{days} days')"

    db = await _get_db()
    try:
        # Totals
        cursor = await db.execute(
            f"""SELECT COUNT(*), AVG(classifier_confidence),
                AVG(CAST(was_downshifted AS REAL)), AVG(latency_ms),
                COALESCE(SUM(cost_usd), 0),
                COALESCE(SUM(input_tokens + output_tokens), 0),
                AVG(CAST(success AS REAL))
                FROM routing_decisions {where}"""
        )
        row = await cursor.fetchone()
        if not row or row[0] == 0:
            return {
                "total_decisions": 0,
                "by_classifier": {},
                "by_task_type": {},
                "avg_confidence": 0.0,
                "downshift_rate": 0.0,
                "avg_latency_ms": 0.0,
                "total_cost_usd": 0.0,
                "total_tokens": 0,
                "success_rate": 0.0,
                "by_model": {},
            }

        total, avg_conf, downshift_rate, avg_lat, total_cost, total_tok, success_rate = row

        # By classifier type
        cursor = await db.execute(
            f"SELECT classifier_type, COUNT(*) FROM routing_decisions {where} "
            "GROUP BY classifier_type ORDER BY COUNT(*) DESC"
        )
        by_classifier = {r[0]: r[1] for r in await cursor.fetchall()}

        # By task type
        cursor = await db.execute(
            f"SELECT task_type, COUNT(*) FROM routing_decisions {where} "
            "GROUP BY task_type ORDER BY COUNT(*) DESC"
        )
        by_task_type = {r[0]: r[1] for r in await cursor.fetchall()}

        # By model
        cursor = await db.execute(
            f"""SELECT final_model, COUNT(*), AVG(latency_ms), COALESCE(SUM(cost_usd), 0)
                FROM routing_decisions {where}
                GROUP BY final_model ORDER BY COUNT(*) DESC"""
        )
        by_model = {
            r[0]: {"count": r[1], "avg_latency": float(r[2]), "total_cost": float(r[3])}
            for r in await cursor.fetchall()
        }

        return {
            "total_decisions": int(total),
            "by_classifier": by_classifier,
            "by_task_type": by_task_type,
            "avg_confidence": float(avg_conf or 0),
            "downshift_rate": float(downshift_rate or 0),
            "avg_latency_ms": float(avg_lat or 0),
            "total_cost_usd": float(total_cost),
            "total_tokens": int(total_tok),
            "success_rate": float(success_rate or 0),
            "by_model": by_model,
        }
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


# ── Routing savings persistence ──────────────────────────────────────────────


async def log_savings(
    task_type: str,
    estimated_saved: float,
    external_cost: float,
    model: str,
    session_id: str,
) -> None:
    """Persist a single routing-savings record to the ``savings_stats`` table.

    Called by ``import_savings_log`` after reading lines from the JSONL file
    written by the PostToolUse hook.

    Args:
        task_type: The classified task type (e.g. "code", "research").
        estimated_saved: Estimated Claude API cost avoided by routing externally.
        external_cost: Actual cost incurred on the external provider.
        model: The external model that handled the request.
        session_id: Opaque identifier grouping calls within one Claude Code session.
    """
    from datetime import datetime, timezone

    db = await _get_db()
    try:
        await db.execute(
            "INSERT INTO savings_stats "
            "(timestamp, session_id, task_type, estimated_claude_cost_saved, external_cost, model_used) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(),
                session_id,
                task_type,
                estimated_saved,
                external_cost,
                model,
            ),
        )
        await db.commit()
    finally:
        await db.close()


async def get_lifetime_savings_summary(days: int = 30) -> dict:
    """Return aggregate routing savings over the last *days* days.

    Queries the ``savings_stats`` table for totals and a per-session breakdown.

    Args:
        days: Look-back window in days.  Use 0 for all-time.

    Returns:
        Dict with ``total_saved``, ``total_external_cost``, ``net_savings``,
        ``tasks_routed``, and ``by_session`` (list of per-session dicts).
    """
    where = (
        f"WHERE timestamp >= datetime('now', '-{days} days')"
        if days > 0
        else ""
    )
    empty: dict = {
        "total_saved": 0.0,
        "total_external_cost": 0.0,
        "net_savings": 0.0,
        "tasks_routed": 0,
        "by_session": [],
    }

    db = await _get_db()
    try:
        cursor = await db.execute(
            f"SELECT COUNT(*), COALESCE(SUM(estimated_claude_cost_saved), 0), "
            f"COALESCE(SUM(external_cost), 0) FROM savings_stats {where}"
        )
        row = await cursor.fetchone()
        if not row or row[0] == 0:
            return empty

        tasks_routed, total_saved, total_external = row

        cursor = await db.execute(
            f"SELECT session_id, COUNT(*), "
            f"COALESCE(SUM(estimated_claude_cost_saved), 0), "
            f"COALESCE(SUM(external_cost), 0), "
            f"MIN(timestamp), MAX(timestamp) "
            f"FROM savings_stats {where} "
            f"GROUP BY session_id ORDER BY MAX(timestamp) DESC"
        )
        sessions = await cursor.fetchall()
        by_session = [
            {
                "session_id": sid,
                "tasks": int(cnt),
                "saved": float(saved),
                "external_cost": float(ext),
                "first_seen": first,
                "last_seen": last,
            }
            for sid, cnt, saved, ext, first, last in sessions
        ]

        return {
            "total_saved": float(total_saved),
            "total_external_cost": float(total_external),
            "net_savings": float(total_saved) - float(total_external),
            "tasks_routed": int(tasks_routed),
            "by_session": by_session,
        }
    finally:
        await db.close()


async def import_savings_log() -> int:
    """Import savings records from the JSONL file into SQLite, then truncate.

    The PostToolUse hook appends one JSON line per routed call to
    ``~/.llm-router/savings_log.jsonl``.  This function reads all lines,
    inserts them into the ``savings_stats`` table, and truncates the file.

    Returns:
        Number of records imported.
    """
    import asyncio

    # Offload synchronous Path.exists() to thread pool to avoid blocking event loop
    exists = await asyncio.to_thread(SAVINGS_LOG_PATH.exists)
    if not exists:
        return 0

    try:
        raw = SAVINGS_LOG_PATH.read_text()
    except OSError:
        return 0

    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if not lines:
        return 0

    from datetime import datetime, timezone

    db = await _get_db()
    imported = 0
    try:
        for line in lines:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            await db.execute(
                "INSERT INTO savings_stats "
                "(timestamp, session_id, task_type, estimated_claude_cost_saved, "
                "external_cost, model_used, host) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    entry.get("timestamp", datetime.now(timezone.utc).isoformat()),
                    entry.get("session_id", "unknown"),
                    entry.get("task_type", "unknown"),
                    float(entry.get("estimated_saved", 0.0)),
                    float(entry.get("external_cost", 0.0)),
                    entry.get("model", "unknown"),
                    entry.get("host", "claude_code"),
                ),
            )
            imported += 1
        await db.commit()
        # Truncate only after successful commit — prevents data loss
        try:
            SAVINGS_LOG_PATH.write_text("")
        except OSError:
            pass
    finally:
        await db.close()

    return imported


async def get_model_latency_stats(window_days: int = 7) -> dict[str, dict]:
    """Return P50/P95 latency statistics per model from recent routing decisions.

    Used by ``benchmarks.get_model_latency_penalty()`` to penalise models that
    are consistently slow in *this* user's environment (e.g. Codex cold-starts).

    Args:
        window_days: Look-back window in days (default 7).

    Returns:
        Dict mapping ``final_model`` -> ``{"p50": float, "p95": float, "count": int}``.
        Only models with at least 5 successful calls are included.
        Returns an empty dict on any error.
    """
    db = await _get_db()
    try:
        cursor = await db.execute(
            """
            SELECT final_model, latency_ms
            FROM routing_decisions
            WHERE timestamp >= datetime('now', ?)
              AND final_model IS NOT NULL
              AND success = 1
              AND latency_ms IS NOT NULL
            ORDER BY final_model, latency_ms
            """,
            (f"-{window_days} days",),
        )
        rows = await cursor.fetchall()
    except Exception:
        return {}
    finally:
        await db.close()

    # Group latency samples by model
    from collections import defaultdict
    samples: dict[str, list[float]] = defaultdict(list)
    for model, latency in rows:
        samples[model].append(float(latency))

    result: dict[str, dict] = {}
    for model, latencies in samples.items():
        if len(latencies) < 5:
            continue
        latencies.sort()
        n = len(latencies)
        p50 = latencies[int(n * 0.50)]
        p95 = latencies[min(int(n * 0.95), n - 1)]
        result[model] = {"p50": p50, "p95": p95, "count": n}
    return result


# Sonnet baseline costs used when per-call saved_usd is not populated (pre-v2.1 rows)
_SONNET_INPUT_PER_M = 3.0
_SONNET_OUTPUT_PER_M = 15.0
_FREE_PROVIDERS = {"ollama", "codex"}


async def get_savings_by_period() -> dict[str, dict]:
    """Return time-bucketed savings aggregates for the savings dashboard.

    Queries the usage table for four periods: today, this week (Mon–Sun),
    this calendar month, and all-time. For each period returns:
        saved_usd:    total dollars saved vs Sonnet baseline
        actual_usd:   total dollars actually spent on paid API calls
        baseline_usd: what Sonnet would have cost for the same tokens
        calls:        total routed calls in the period
        efficiency:   baseline_usd / actual_usd multiplier (0 if no paid calls)

    Rows with saved_usd populated (v2.1+) use that directly. Pre-v2.1 rows
    fall back to estimating the Sonnet baseline from token counts.
    """
    db = await _get_db()
    try:
        # Build period boundaries as SQLite datetime expressions
        periods = {
            "today": "date('now')",
            "week": "date('now', 'weekday 0', '-6 days')",
            "month": "date('now', 'start of month')",
            "all_time": "'1970-01-01'",
        }
        result: dict[str, dict] = {}
        for name, since_expr in periods.items():
            rows = await db.execute_fetchall(
                f"""SELECT provider, input_tokens, output_tokens, cost_usd, saved_usd
                    FROM usage
                    WHERE date(timestamp) >= {since_expr}
                      AND success = 1
                      AND is_simulated IS NOT 1""",
            )
            actual = baseline = saved_total = 0.0
            calls = 0
            for provider, in_tok, out_tok, cost, saved_col in rows:
                in_tok = in_tok or 0
                out_tok = out_tok or 0
                cost = cost or 0.0
                calls += 1
                if provider == "subscription":
                    continue  # CC subscription rows have no token cost data
                sonnet_est = (in_tok * _SONNET_INPUT_PER_M + out_tok * _SONNET_OUTPUT_PER_M) / 1_000_000
                baseline += sonnet_est
                if provider in _FREE_PROVIDERS:
                    saved_total += saved_col if saved_col else sonnet_est
                else:
                    actual += cost
                    saved_total += saved_col if saved_col else max(0.0, sonnet_est - cost)

            efficiency = baseline / actual if actual > 0.001 else 0.0
            result[name] = {
                "saved_usd": round(saved_total, 4),
                "actual_usd": round(actual, 4),
                "baseline_usd": round(baseline, 4),
                "calls": calls,
                "efficiency": round(efficiency, 1),
            }
        return result
    finally:
        await db.close()


async def get_model_failure_rates(window_days: int = 30) -> dict[str, float]:
    """Return the failure rate per model over the given time window.

    Used by ``benchmarks.get_model_failure_penalty()`` to apply a local
    feedback loop on top of the benchmark scores: models that consistently fail
    in production get pushed down the chain regardless of their benchmark rank.

    Args:
        window_days: Number of past days to include (default 30).

    Returns:
        Dict mapping ``final_model`` -> failure rate (0.0–1.0).
        Only models with at least 5 routing decisions are included,
        to avoid penalizing models on insufficient data.
        Returns an empty dict if the table is empty or on any error.
    """
    db = await _get_db()
    try:
        cursor = await db.execute(
            """
            SELECT final_model,
                   COUNT(*) AS total,
                   SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) AS failures
            FROM routing_decisions
            WHERE timestamp >= datetime('now', ?)
              AND final_model IS NOT NULL
            GROUP BY final_model
            HAVING total >= 5
            """,
            (f"-{window_days} days",),
        )
        rows = await cursor.fetchall()
        return {
            row[0]: row[2] / row[1]
            for row in rows
            if row[1] > 0
        }
    except Exception:
        return {}
    finally:
        await db.close()


async def get_model_acceptance_scores(window_days: int = 30) -> dict[str, float]:
    """Return the user-acceptance rate per model based on ``llm_rate`` feedback.

    Acceptance rate = (was_good=1 count) / (total rated count). Only models
    with at least 3 explicitly rated calls are included to avoid penalising
    models on insufficient data.

    Args:
        window_days: Look-back window in days (default 30).

    Returns:
        Dict mapping ``final_model`` -> acceptance rate (0.0–1.0).
        Returns an empty dict if no feedback exists or on any error.
    """
    db = await _get_db()
    try:
        cursor = await db.execute(
            """
            SELECT final_model,
                   COUNT(*) AS rated,
                   SUM(CASE WHEN was_good = 1 THEN 1 ELSE 0 END) AS good
            FROM routing_decisions
            WHERE timestamp >= datetime('now', ?)
              AND final_model IS NOT NULL
              AND was_good IS NOT NULL
            GROUP BY final_model
            HAVING rated >= 3
            """,
            (f"-{window_days} days",),
        )
        rows = await cursor.fetchall()
        return {row[0]: row[2] / row[1] for row in rows if row[1] > 0}
    except Exception:
        return {}
    finally:
        await db.close()


async def get_team_savings(
    user_id: str = "",
    project_id: str = "",
    period: str = "week",
) -> dict:
    """Return aggregated savings for the team dashboard.

    Queries the ``usage`` table filtered by optional user/project and period.
    Free providers (ollama, codex, subscription) are counted toward free_pct.

    Args:
        user_id: Filter to a specific user. Empty = all users.
        project_id: Filter to a specific project. Empty = all projects.
        period: ``"today"``, ``"week"``, ``"month"``, or ``"all"``.

    Returns:
        Dict with total_calls, saved_usd, actual_usd, free_pct, top_models.
    """
    period_map = {
        "today": "date('now')",
        "week": "date('now', 'weekday 0', '-6 days')",
        "month": "date('now', 'start of month')",
        "all": "'1970-01-01'",
    }
    since = period_map.get(period, period_map["week"])

    where_parts = [f"timestamp >= {since}"]
    params: list = []
    if user_id:
        where_parts.append("user_id = ?")
        params.append(user_id)
    if project_id:
        where_parts.append("project_id = ?")
        params.append(project_id)
    where = " AND ".join(where_parts)

    _free = {"ollama", "codex", "subscription"}

    db = await _get_db()
    try:
        cursor = await db.execute(
            f"""
            SELECT model, provider,
                   COUNT(*) as calls,
                   COALESCE(SUM(cost_usd), 0) as actual_cost,
                   COALESCE(SUM(input_tokens + output_tokens), 0) as tokens
            FROM usage
            WHERE {where}
            GROUP BY model, provider
            ORDER BY calls DESC
            """,
            params,
        )
        rows = await cursor.fetchall()
    except Exception:
        return {"total_calls": 0, "saved_usd": 0.0, "actual_usd": 0.0, "free_pct": 0.0, "top_models": []}
    finally:
        await db.close()

    total_calls = sum(r[2] for r in rows)
    actual_usd = sum(r[3] for r in rows)
    free_calls = sum(r[2] for r in rows if r[1] in _free)
    free_pct = free_calls / total_calls if total_calls else 0.0

    # Estimate savings vs Sonnet baseline using token counts
    total_tokens = sum(r[4] for r in rows)
    sonnet_baseline = total_tokens / 1000 * ((_SONNET_INPUT_PER_M + _SONNET_OUTPUT_PER_M) / 2 / 1000)
    saved_usd = max(0.0, sonnet_baseline - actual_usd)

    top_models = [
        {"model": r[0], "provider": r[1], "calls": r[2], "cost": r[3]}
        for r in rows[:10]
    ]

    return {
        "total_calls": total_calls,
        "saved_usd": saved_usd,
        "actual_usd": actual_usd,
        "free_pct": free_pct,
        "top_models": top_models,
    }


# Sonnet 4.6 pricing used as baseline for savings calculations
_SONNET_INPUT_PER_M = 3.0    # $3 per million input tokens
_SONNET_OUTPUT_PER_M = 15.0  # $15 per million output tokens


async def get_routing_savings_vs_sonnet(days: int = 0) -> dict:
    """Compute savings by comparing actual cost vs Sonnet 4.6 baseline.

    Uses the routing_decisions table (populated by the router on every call).
    Savings = what Sonnet would have cost − what we actually paid.

    Args:
        days: Look-back window. 0 = all time.

    Returns:
        Dict with ``total_calls``, ``actual_cost``, ``baseline_cost``,
        ``saved``, ``input_tokens``, ``output_tokens``, and ``by_model``.
    """
    where = (
        f"WHERE timestamp >= datetime('now', '-{days} days') AND success = 1"
        if days > 0
        else "WHERE success = 1"
    )
    empty: dict = {
        "total_calls": 0,
        "actual_cost": 0.0,
        "baseline_cost": 0.0,
        "saved": 0.0,
        "input_tokens": 0,
        "output_tokens": 0,
        "by_model": {},
    }
    db = await _get_db()
    try:
        cursor = await db.execute(
            f"""SELECT COUNT(*),
                       COALESCE(SUM(cost_usd), 0),
                       COALESCE(SUM(input_tokens), 0),
                       COALESCE(SUM(output_tokens), 0)
                FROM routing_decisions {where}"""
        )
        row = await cursor.fetchone()
        if not row or row[0] == 0:
            return empty

        total, actual_cost, in_tok, out_tok = row
        baseline = (in_tok * _SONNET_INPUT_PER_M + out_tok * _SONNET_OUTPUT_PER_M) / 1_000_000
        saved = max(0.0, baseline - actual_cost)

        cursor = await db.execute(
            f"""SELECT final_model, COUNT(*),
                       COALESCE(SUM(cost_usd), 0),
                       COALESCE(SUM(input_tokens), 0),
                       COALESCE(SUM(output_tokens), 0)
                FROM routing_decisions {where}
                GROUP BY final_model ORDER BY COUNT(*) DESC"""
        )
        by_model = {}
        for m_row in await cursor.fetchall():
            m, cnt, m_cost, m_in, m_out = m_row
            m_baseline = (m_in * _SONNET_INPUT_PER_M + m_out * _SONNET_OUTPUT_PER_M) / 1_000_000
            by_model[m or "unknown"] = {
                "calls": int(cnt),
                "actual_cost": float(m_cost),
                "baseline_cost": float(m_baseline),
                "saved": max(0.0, m_baseline - float(m_cost)),
            }

        return {
            "total_calls": int(total),
            "actual_cost": float(actual_cost),
            "baseline_cost": float(baseline),
            "saved": float(saved),
            "input_tokens": int(in_tok),
            "output_tokens": int(out_tok),
            "by_model": by_model,
        }
    except Exception:
        return empty
    finally:
        await db.close()


async def get_cache_savings(period: str = "today") -> dict[str, float]:
    """Get prompt caching savings for the period.

    Queries the usage table for rows where cache_hit=1 and sums cache_savings_usd.

    Args:
        period: Time period — "today", "week", "month", or "all".

    Returns:
        Dict with ``total_calls_cached``, ``total_savings_usd``, ``cache_hit_rate``.
    """
    db = await _get_db()
    try:
        # Determine time filter
        if period == "today":
            time_filter = "timestamp >= datetime('now', 'start of day')"
        elif period == "week":
            time_filter = "timestamp >= datetime('now', '-7 days')"
        elif period == "month":
            time_filter = "timestamp >= datetime('now', 'start of month')"
        else:  # all
            time_filter = "1"

        # Get cache hit stats
        cursor = await db.execute(
            f"""SELECT COUNT(*), COALESCE(SUM(cache_savings_usd), 0)
                FROM usage WHERE {time_filter} AND cache_hit = 1"""
        )
        cached_row = await cursor.fetchone()
        cached_calls, cached_savings = cached_row if cached_row else (0, 0.0)

        # Get total calls for hit rate
        cursor = await db.execute(f"SELECT COUNT(*) FROM usage WHERE {time_filter}")
        total_row = await cursor.fetchone()
        total_calls = total_row[0] if total_row else 0

        cache_hit_rate = (cached_calls / total_calls * 100) if total_calls > 0 else 0.0

        return {
            "total_calls_cached": int(cached_calls),
            "total_savings_usd": float(cached_savings),
            "cache_hit_rate": float(cache_hit_rate),
        }
    except Exception:
        return {
            "total_calls_cached": 0,
            "total_savings_usd": 0.0,
            "cache_hit_rate": 0.0,
        }
    finally:
        await db.close()



async def log_compression_stat(
    *,
    session_id: str | None = None,
    command: str,
    layer: str,
    original_tokens: int,
    compressed_tokens: int,
    compression_ratio: float,
    strategy: str | None = None,
) -> None:
    """Log a compression operation (RTK command output or Token-Savior response).
    
    Args:
        session_id: Session ID for correlation with routing decisions
        command: The shell command (e.g., 'git log') or 'response'
        layer: 'rtk' for command output, 'token-savior' for response
        original_tokens: Token count before compression
        compressed_tokens: Token count after compression
        compression_ratio: compressed_tokens / original_tokens
        strategy: Which filter applied (e.g., 'git:log', 'docker:ps')
    """
    db = await _get_db()
    try:
        tokens_saved = original_tokens - compressed_tokens
        await db.execute(
            """INSERT INTO compression_stats
               (session_id, command, layer, original_tokens, compressed_tokens,
                compression_ratio, tokens_saved, strategy)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                command,
                layer,
                original_tokens,
                compressed_tokens,
                compression_ratio,
                tokens_saved,
                strategy,
            ),
        )
        await db.commit()
    finally:
        await db.close()


async def get_compression_stats(days: int = 7) -> dict:
    """Get compression statistics for the last N days.
    
    Returns:
        Dict with compression metrics by layer and strategy.
    """
    from datetime import datetime, timedelta, timezone
    
    db = await _get_db()
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        
        # Total operations
        cursor = await db.execute(
            "SELECT COUNT(*) as count FROM compression_stats WHERE timestamp >= ?",
            (cutoff,)
        )
        result = await cursor.fetchone()
        total_ops = result[0] if result else 0
        
        # RTK stats (Layer 1: Command output compression)
        cursor = await db.execute(
            """SELECT 
                COUNT(*) as operations,
                SUM(original_tokens) as original_tokens,
                SUM(compressed_tokens) as compressed_tokens,
                SUM(tokens_saved) as tokens_saved,
                AVG(compression_ratio) as avg_ratio
               FROM compression_stats 
               WHERE layer = 'rtk' AND timestamp >= ?""",
            (cutoff,)
        )
        rtk_row = await cursor.fetchone()
        rtk_stats = {
            "operations": rtk_row[0] or 0,
            "original_tokens": rtk_row[1] or 0,
            "compressed_tokens": rtk_row[2] or 0,
            "tokens_saved": rtk_row[3] or 0,
            "avg_compression_ratio": float(rtk_row[4]) if rtk_row[4] else 0.0,
        }
        
        # Token-Savior stats (Layer 3: Response compression)
        cursor = await db.execute(
            """SELECT 
                COUNT(*) as operations,
                SUM(original_tokens) as original_tokens,
                SUM(compressed_tokens) as compressed_tokens,
                SUM(tokens_saved) as tokens_saved,
                AVG(compression_ratio) as avg_ratio
               FROM compression_stats 
               WHERE layer = 'token-savior' AND timestamp >= ?""",
            (cutoff,)
        )
        token_savior_row = await cursor.fetchone()
        token_savior_stats = {
            "operations": token_savior_row[0] or 0,
            "original_tokens": token_savior_row[1] or 0,
            "compressed_tokens": token_savior_row[2] or 0,
            "tokens_saved": token_savior_row[3] or 0,
            "avg_compression_ratio": float(token_savior_row[4]) if token_savior_row[4] else 0.0,
        }
        
        # By strategy breakdown
        cursor = await db.execute(
            """SELECT 
                strategy,
                COUNT(*) as operations,
                SUM(tokens_saved) as tokens_saved,
                AVG(compression_ratio) as avg_ratio
               FROM compression_stats 
               WHERE timestamp >= ?
               GROUP BY strategy
               ORDER BY tokens_saved DESC""",
            (cutoff,)
        )
        strategies = {}
        async for row in cursor:
            strategies[row[0]] = {
                "operations": row[1],
                "tokens_saved": row[2] or 0,
                "avg_compression_ratio": float(row[3]) if row[3] else 0.0,
            }
        
        # Total tokens saved
        cursor = await db.execute(
            "SELECT SUM(tokens_saved) FROM compression_stats WHERE timestamp >= ?",
            (cutoff,)
        )
        result = await cursor.fetchone()
        total_saved = result[0] if result and result[0] else 0
        
        return {
            "period_days": days,
            "total_operations": total_ops,
            "rtk_stats": rtk_stats,
            "token_savior_stats": token_savior_stats,
            "by_strategy": strategies,
            "total_tokens_saved": total_saved,
        }
    except Exception:
        return {
            "period_days": days,
            "total_operations": 0,
            "rtk_stats": {},
            "token_savior_stats": {},
            "by_strategy": {},
            "total_tokens_saved": 0,
        }
    finally:
        await db.close()
