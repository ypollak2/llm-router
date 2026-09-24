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
import os
import re
from pathlib import Path

import aiosqlite

from llm_router import pricing as _pricing
from llm_router.provenance import Measured
# R11: `state_path` comes from `llm_router.paths`, the canonical resolver.
# The import from `config` here was unused and shadowed by the in-function
# import below — two names for one concept is the class R11 removed.
from llm_router.config import get_config
from llm_router.types import (
    LLMResponse, MODEL_COST_PER_1K, MODEL_SPEED_TPS,
    RoutingProfile, TaskType, colorize_model,
)
from llm_router.savings import net_saved

from llm_router import paths


def _detect_synthetic() -> bool:
    """Is this process a test or benchmark run?

    C-02. Delegates to `routing_quality.detect_synthetic` rather than
    reimplementing the check: four provenance mechanisms already existed in this
    codebase and three of them did not work, precisely because each surface
    decided for itself what counted as real.
    """
    try:
        from llm_router.routing_quality import detect_synthetic

        return detect_synthetic()
    except Exception:  # noqa: BLE001 -- provenance must never fail a spend write
        # Fail CLOSED: unknown provenance is marked synthetic rather than
        # admitted as production, matching `is_evaluable`.
        return True



def _refuse_unisolated_test_write(db_path: Path) -> bool:
    """True when a test is about to write into the user's real database.

    WHY THIS EXISTS
    ---------------
    A "stub-detection guard" used to be the only protection, matching an exact
    fingerprint of token/cost values. Every fixture added after it was written walked
    straight through. Measured against the rows that actually reached production it
    would have blocked **0 of 28,536** — while its own comment asserted that unisolated
    tests "can never pollute the real ~/.llm-router/usage.db".

    The damage was not hypothetical. Those 28,536 synthetic rows were 69.4% of
    `routing_decisions`, all naming `openai/gpt-4o-mini`, and the dashboard reported
    them as routing behaviour. They are exactly the rows with
    `classifier_type='unknown'` — the classifier never ran for one of them. Excluding
    them, the router's real preference is `hermes3:8b` (local) at 38.6%, `gpt-4o` at
    35.6%, and gpt-4o-mini at 0.0%. The product's primary surface understated local
    routing threefold and invented a majority share for a model it never chose.

    WHAT CHANGED
    ------------
    "Does this row look synthetic?" is a guess that ages badly — it enumerates the
    values its author happened to know. "Is a test writing to the production database?"
    is directly observable and cannot drift as fixtures change.

    This takes only a path, deliberately: a guard that inspects row values is the
    fingerprint defect returning under a new name, and `tests/test_prod_db_isolation.py`
    asserts the signature to keep it that way.

    The suite's own writes are unaffected — the `temp_db` fixture repoints
    `LLM_ROUTER_DB_PATH`, so `db_path` is a tmp file and this returns False. Only a test
    aimed at the real database is refused, and `LLM_ROUTER_ALLOW_STUBS=1` opts out
    deliberately for tests that mean it.
    """
    if os.environ.get("LLM_ROUTER_ALLOW_STUBS") == "1":
        return False
    if not os.environ.get("PYTEST_CURRENT_TEST"):
        return False  # not a test — this is a real user's routing decision
    if os.environ.get("LLM_ROUTER_HOME", "").strip():
        # R11. Explicitly isolated: the process has declared where its state
        # lives, so by construction it is not the operator's database.
        #
        # This arm is load-bearing. The comparison below used to be against
        # `state_path("usage.db")`, which FOLLOWS LLM_ROUTER_HOME — so once the
        # config stopped freezing that path, "the production database" and
        # "wherever this isolated run points" became the same value and the
        # guard refused every test write, silently. A guard that blocks
        # everything is as useless as one that blocks nothing, and it fails in
        # the direction that looks like passing tests.
        return False
    try:
        # PRODUCTION means the operator's real home, not whatever the current
        # environment resolves to.
        production = Path.home() / ".llm-router" / "usage.db"
        return Path(db_path).resolve() == production.resolve()
    except OSError:  # pragma: no cover — an unresolvable path is not the production one
        return False

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
    host TEXT NOT NULL DEFAULT 'claude_code',
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0
)
"""
"""Schema for the ``savings_stats`` table tracking per-call routing savings.
Each row represents one routed call logged by the PostToolUse hook via JSONL,
then imported into SQLite by the MCP server for lifetime analytics."""


def savings_log_path() -> Path:
    """Where the savings JSONL lives, resolved PER CALL.

    This was a module-level constant evaluated at import, which froze the real
    user's home the moment cost.py was first imported — so LLM_ROUTER_HOME could
    not move it. That matters more here than elsewhere: import_savings_log does
    not merely read this file, it CLAIMS it with os.replace and deletes it after
    importing. A test that believed it was isolated could consume a developer's
    real, not-yet-imported savings history.
    """
    from llm_router.paths import state_path

    return state_path("savings_log.jsonl")


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

MIGRATE_CLAUDE_USAGE_CACHE_TOKENS = [
    "ALTER TABLE claude_usage ADD COLUMN input_tokens INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE claude_usage ADD COLUMN output_tokens INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE claude_usage ADD COLUMN cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE claude_usage ADD COLUMN cache_read_input_tokens INTEGER NOT NULL DEFAULT 0",
]
"""v9.2.2 — separate token counts so calc_savings can use the 4-component
Anthropic billing formula instead of a single lumped tokens_used."""


MIGRATE_ADD_CODEX_USAGE_TABLE = [
    """CREATE TABLE IF NOT EXISTS codex_usage (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT DEFAULT (datetime('now')),
        model TEXT NOT NULL,
        tokens_used INTEGER NOT NULL,
        complexity TEXT NOT NULL,
        cost_saved_usd REAL NOT NULL DEFAULT 0,
        time_saved_sec REAL NOT NULL DEFAULT 0,
        input_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0,
        cache_read_input_tokens INTEGER NOT NULL DEFAULT 0,
        routing_overhead_usd REAL NOT NULL DEFAULT 0.0
    )""",
    "CREATE INDEX IF NOT EXISTS idx_codex_usage_ts ON codex_usage(timestamp)",
]
"""v9.3.0 — Codex CLI session token consumption + savings, parallel to claude_usage.
Schema kept symmetric with claude_usage so dashboard queries can UNION cleanly."""


MIGRATE_ADD_GEMINI_USAGE_TABLE = [
    """CREATE TABLE IF NOT EXISTS gemini_usage (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT DEFAULT (datetime('now')),
        model TEXT NOT NULL,
        tokens_used INTEGER NOT NULL,
        complexity TEXT NOT NULL,
        cost_saved_usd REAL NOT NULL DEFAULT 0,
        time_saved_sec REAL NOT NULL DEFAULT 0,
        input_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0,
        cache_read_input_tokens INTEGER NOT NULL DEFAULT 0,
        routing_overhead_usd REAL NOT NULL DEFAULT 0.0
    )""",
    "CREATE INDEX IF NOT EXISTS idx_gemini_usage_ts ON gemini_usage(timestamp)",
]
"""v9.3.1 — Gemini CLI session token consumption + savings, parallel to
claude_usage and codex_usage. Same symmetric schema."""


MIGRATE_USAGE_ROUTING_OVERHEAD = [
    "ALTER TABLE claude_usage ADD COLUMN routing_overhead_usd REAL NOT NULL DEFAULT 0.0",
    "ALTER TABLE usage ADD COLUMN routing_overhead_usd REAL NOT NULL DEFAULT 0.0",
]
"""v9.2.2 — capture the classifier + Ollama call cost so realized savings
(gross_saved - routing_overhead) can be reported alongside the gross number."""


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

# ── C-02 / Phase 2: the provenance cutover ─────────────────────────────────
#
# `is_simulated` was declared with `DEFAULT 0`, and never written. So every one
# of the ~23,000 historical rows reads as `is_simulated = 0`, which is not a
# recorded fact about those rows -- it is the column default standing in for a
# measurement nobody took. 1,813 of them are known fixtures, and they are not
# separable after the fact because they carry real model names.
#
# NULL is the honest value: UNKNOWN provenance. This does not delete anything
# (the column never held information) and it does not touch the append-only
# routing ledger, which is a different store. It replaces a default that lies
# with an absence that is true.
#
# Guarded by `provenance_meta` so it runs exactly once. Without that, a second
# run would blank the provenance of rows written correctly after the cutover --
# turning a fix into the bug it was fixing.
MIGRATE_USAGE_PROVENANCE_CUTOVER_TABLE = [
    """CREATE TABLE IF NOT EXISTS provenance_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        applied_at TEXT DEFAULT (datetime('now'))
    )""",
]

PROVENANCE_CUTOVER_KEY = "usage_is_simulated_cutover"


async def provenance_exclusion_summary() -> dict:
    """How many historical rows are excluded from savings, and why. T-21.

    The cutover writes its count into `provenance_meta` and NOTHING has ever
    read it back. The user-visible consequence is a support ticket shaped like
    "my lifetime savings dropped to $0 after upgrading" with no in-product
    answer — the figure is correct (those rows' provenance was never measured,
    so counting them as real money was the lie), but a correct number that
    appears without explanation is indistinguishable from a bug.

    F16 widened this: `claude_usage`, `codex_usage`, `gemini_usage` and
    `savings_stats` gained the same column, so the same drop now happens across
    four more ledgers.

    Returns keys: `cutover_rows`, `unknown_rows`, `production_rows`,
    `synthetic_rows`, `explanation`. Fail-open — returns zeros with an
    `explanation` saying so, never raises into a savings command.
    """
    out = {"cutover_rows": 0, "unknown_rows": 0, "production_rows": 0,
           "synthetic_rows": 0, "explanation": ""}
    try:
        db = await _get_db()
    except Exception as exc:  # noqa: BLE001
        # CHZ-FO-02: a function that returns live data must account for the
        # swallow, or the zeros it returns read as a measurement.
        from llm_router import failopen as _fo
        _fo.record("CHZ-FO-COST-PROVENANCE-SUMMARY-OPEN", exc)
        out["explanation"] = f"ledger unreadable: {type(exc).__name__}"
        return out
    try:
        try:
            cur = await db.execute(
                "SELECT value FROM provenance_meta WHERE key = ?",
                (PROVENANCE_CUTOVER_KEY,),
            )
            row = await cur.fetchone()
            out["cutover_rows"] = int(row[0]) if row and str(row[0]).isdigit() else 0
        except Exception as _meta_exc:  # noqa: BLE001 — table may predate the migration
            from llm_router import failopen as _fo
            _fo.record("CHZ-FO-COST-PROVENANCE-META", _meta_exc)
        cur = await db.execute(
            "SELECT COALESCE(SUM(is_simulated IS NULL), 0), "
            "       COALESCE(SUM(is_simulated = 0), 0), "
            "       COALESCE(SUM(is_simulated = 1), 0) FROM usage"
        )
        row = await cur.fetchone()
        if row:
            out["unknown_rows"] = int(row[0] or 0)
            out["production_rows"] = int(row[1] or 0)
            out["synthetic_rows"] = int(row[2] or 0)
    except Exception as exc:  # noqa: BLE001
        from llm_router import failopen as _fo
        _fo.record("CHZ-FO-COST-PROVENANCE-COUNT", exc)
        out["explanation"] = f"count failed: {type(exc).__name__}"
        return out
    finally:
        await db.close()

    if out["unknown_rows"]:
        out["explanation"] = (
            f"{out['unknown_rows']} usage row(s) predate provenance tracking and "
            f"are excluded from savings totals. Their origin — real traffic or a "
            f"test run — was never recorded, and counting an unmeasured row as "
            f"money is the defect this excludes them to avoid. Totals will rebuild "
            f"from new, stamped calls."
        )
    else:
        out["explanation"] = "no rows are excluded for missing provenance."
    return out


async def _apply_provenance_cutover(db) -> int:
    """Mark pre-provenance `usage` rows UNKNOWN. Runs once. Returns rows marked.

    Idempotent by construction: the sentinel is written in the same transaction
    as the update, so an interrupted run either did both or neither.
    """
    try:
        cur = await db.execute(
            "SELECT value FROM provenance_meta WHERE key = ?", (PROVENANCE_CUTOVER_KEY,)
        )
        if await cur.fetchone():
            return 0  # already applied

        cur = await db.execute(
            "UPDATE usage SET is_simulated = NULL WHERE is_simulated = 0"
        )
        marked = cur.rowcount or 0
        await db.execute(
            "INSERT INTO provenance_meta (key, value) VALUES (?, ?)",
            (PROVENANCE_CUTOVER_KEY, str(marked)),
        )
        await db.commit()
        if marked:
            import logging as _logging

            _logging.getLogger("llm_router").info(
                "provenance cutover: %d pre-provenance usage rows marked UNKNOWN. "
                "They are excluded from savings until superseded by a clean window.",
                marked,
            )
        return marked
    except Exception as exc:  # noqa: BLE001 — a migration must never break routing
        import logging as _logging

        _logging.getLogger("llm_router").debug("provenance cutover skipped: %s", exc)
        return 0


MIGRATE_USAGE_ADD_TEAM = [
    "ALTER TABLE usage ADD COLUMN user_id TEXT",
    "ALTER TABLE usage ADD COLUMN project_id TEXT",
]
"""Idempotent migration to add team identity columns (v3.0)."""

MIGRATE_USAGE_ADD_COMPLEXITY = [
    "ALTER TABLE usage ADD COLUMN complexity TEXT DEFAULT 'moderate'",
]
"""Idempotent migration to track task complexity in usage table (v7.3)."""

MIGRATE_SIBLING_TABLES_ADD_PROVENANCE = [
    "ALTER TABLE claude_usage ADD COLUMN is_simulated INTEGER",
    "ALTER TABLE codex_usage ADD COLUMN is_simulated INTEGER",
    "ALTER TABLE gemini_usage ADD COLUMN is_simulated INTEGER",
    "ALTER TABLE savings_stats ADD COLUMN is_simulated INTEGER",
]
"""T-05: give the four sibling ledgers the provenance column `usage` already has.

DELIBERATELY NO DEFAULT, and this is the entire lesson of C-02. `usage.is_simulated`
shipped as `INTEGER DEFAULT 0`, so all ~23,000 historical rows read as "production"
— a value nobody ever measured, asserted by the schema. The filter built on top of
it looked protective and excluded nothing.

Without a default, every pre-existing row is NULL = *unknown*, which is the truth:
these tables were written for months with no provenance recorded, and no amount of
backfill can recover it. `production_only()` is fail-closed (`= 0`), so unknown rows
drop out of money figures rather than being counted as real.

CONSEQUENCE, stated rather than discovered later: on an existing install every
historical row in these four tables leaves the savings totals the moment this
migration runs. The figures will read low until new, stamped rows accumulate. That
is a correction, not a regression — the previous totals included an unmeasured
population."""

MIGRATE_SAVINGS_STATS_ADD_HOST = [
    "ALTER TABLE savings_stats ADD COLUMN host TEXT NOT NULL DEFAULT 'claude_code'",
]
"""Idempotent migration to add host attribution column to savings_stats (v3.1)."""

MIGRATE_SAVINGS_STATS_ADD_TOKENS = [
    "ALTER TABLE savings_stats ADD COLUMN input_tokens INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE savings_stats ADD COLUMN output_tokens INTEGER NOT NULL DEFAULT 0",
]
"""Idempotent migration: record token counts for DIRECT-routed calls so the
dashboard's token totals include free-provider (Ollama/Codex) throughput (v7.4)."""

MIGRATE_SAVINGS_STATS_ADD_MODE = [
    "ALTER TABLE savings_stats ADD COLUMN mode TEXT",
]
"""Idempotent migration: record WHETHER a routed draft replaced Claude's turn.

Nullable on purpose. Every pre-existing row predates the distinction, and
backfilling them with a value would retroactively assert something that was
never measured — the one thing this column exists to stop. NULL means "not
recorded"; 'block' means the turn was replaced; 'echo' means it was not, and
such a row carries estimated_claude_cost_saved = 0."""

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

MIGRATE_ROUTING_DECISIONS_ADD_CAPABILITIES = [
    "ALTER TABLE routing_decisions ADD COLUMN capabilities_json TEXT DEFAULT NULL",
]
"""Shadow-mode capability vector (see capabilities.serialize_capability_decision).

Written only when LLM_ROUTER_CAPABILITY_ROUTING is on, never read by live routing.
NULL therefore means "shadow mode was off for this decision", which is the
common case and must stay distinguishable from "was on and found nothing".
"""

MIGRATE_ROUTING_DECISIONS_ADD_AUDIT = [
    "ALTER TABLE routing_decisions ADD COLUMN audit_verdict TEXT DEFAULT NULL",
    "ALTER TABLE routing_decisions ADD COLUMN audit_checked_at TEXT DEFAULT NULL",
]
"""Post-hoc misroute audit (see misroute_audit.py).

Both columns default NULL, and NULL is the "not yet audited" marker the
sampler selects on — so an existing database needs no backfill and the audit
picks up every pre-existing row on its first run.
"""

MIGRATE_ROUTING_DECISIONS_ADD_SUBJECT = [
    "ALTER TABLE routing_decisions ADD COLUMN subject TEXT",
]
"""Plan 07 Cat E — enables (policy, subject, model) outcome aggregation for bandit selection."""

MIGRATE_ROUTING_DECISIONS_ADD_PROVENANCE = [
    "ALTER TABLE routing_decisions ADD COLUMN provenance TEXT",
]
"""Where a row came from, written by `_write_provenance()` at insert time.

Deliberately has NO DEFAULT. `is_real` (v7.5) tried to answer the same question with
`INTEGER DEFAULT 1`, which means a column named "is real" reads 1 on rows that are
demonstrably synthetic — a default that asserts the very thing it should be recording.

A NULL here means "written before this column existed" and is reported as UNKNOWN by
`llm_router.attribution`, never promoted into attributed or unattributed. Absence of evidence
is its own answer; the alternative is a default that manufactures one."""

CREATE_BENCHMARK_RESULTS_TABLE = """
CREATE TABLE IF NOT EXISTS benchmark_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT DEFAULT (datetime('now')),
    version TEXT NOT NULL,
    policy TEXT NOT NULL,
    benchmark TEXT NOT NULL,
    split TEXT NOT NULL,
    score REAL NOT NULL,
    n_samples INTEGER NOT NULL,
    per_subject_json TEXT
)
"""
"""Plan 07 Cat G.3 — append-only record of benchmark runs keyed by version+policy.
The regression detector reads ordered rows from here to surface release-over-release
score drops."""
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

CREATE_MODEL_QUALITY_TRENDS_TABLE = """
CREATE TABLE IF NOT EXISTS model_quality_trends (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT DEFAULT (datetime('now')),
    model TEXT NOT NULL,
    task_type TEXT,
    window_start TEXT,
    window_end TEXT,
    avg_score REAL,
    sample_count INTEGER,
    trend_direction TEXT
)
"""
"""Schema for the ``model_quality_trends`` table tracking rolling quality scores per model.

Each row represents a quality window (e.g., 7-day rolling average) for a single model.
- avg_score: Average judge score (0–1) over the window
- sample_count: Number of evaluated responses in the window
- trend_direction: 'improving'|'stable'|'degrading' based on previous window
"""

MIGRATE_ADD_MODEL_QUALITY_TRENDS = [
    "CREATE TABLE IF NOT EXISTS model_quality_trends (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT DEFAULT (datetime('now')), model TEXT NOT NULL, task_type TEXT, window_start TEXT, window_end TEXT, avg_score REAL, sample_count INTEGER, trend_direction TEXT)",
]
"""Idempotent migration to add model quality trends table (v6.2)."""

"""Idempotent migration to add per-call savings columns to usage table.

baseline_model:     Model that would have been used without routing (e.g. claude-sonnet)
potential_cost_usd: Estimated cost if baseline_model had handled the call
saved_usd:          potential_cost_usd - actual cost_usd (negative = routing cost money)
is_simulated:       1 for dry-run test calls (llm_router test), 0 for real calls
"""


CREATE_QUOTA_SNAPSHOTS_TABLE = """
CREATE TABLE IF NOT EXISTS quota_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT DEFAULT (datetime('now')),
    session_id TEXT NOT NULL,
    prompt_sequence INTEGER NOT NULL,
    prompt_hash TEXT,
    -- Quota state at the moment this prompt arrived
    claude_session_pct REAL NOT NULL,
    claude_weekly_pct REAL NOT NULL,
    claude_sonnet_pct REAL NOT NULL,
    openai_spent_usd REAL NOT NULL DEFAULT 0,
    gemini_spent_usd REAL NOT NULL DEFAULT 0,
    ollama_available INTEGER NOT NULL DEFAULT 1,
    cache_age_seconds REAL NOT NULL,
    was_cache_fresh INTEGER NOT NULL,    -- 1=fresh, 0=stale fallback
    -- Routing outcome
    routing_decision_id INTEGER,         -- FK → routing_decisions.id (nullable: Ollama has no row)
    final_model TEXT,
    final_provider TEXT,
    complexity_requested TEXT,
    complexity_used TEXT,
    was_downgraded INTEGER DEFAULT 0     -- 1 if pressure forced complexity downgrade
)
"""
"""Schema for the ``quota_snapshots`` table tracking per-prompt quota state audit trail.

Each row captures the quota state (Claude session%, weekly%, provider spend) at the
moment a prompt arrived, enabling retrospective analysis of quota pressure patterns
and correlation with routing decisions. Rows are retained forever (no TTL) for
complete audit trail.
"""

MIGRATE_ROUTING_DECISIONS_ADD_REAL_FLAG = [
    "ALTER TABLE routing_decisions ADD COLUMN is_real INTEGER DEFAULT 1",
    "ALTER TABLE routing_decisions ADD COLUMN session_id TEXT",
    "ALTER TABLE routing_decisions ADD COLUMN prompt_sequence INTEGER",
]
"""Idempotent migration to add data quality and audit columns to routing_decisions (v7.5).

is_real: 1 for production calls, 0 for test/simulated data
session_id: Correlation ID for grouping prompts within a session
prompt_sequence: Sequential prompt number within session (0, 1, 2, ...)
"""

MIGRATE_ROUTING_DECISIONS_MARK_CONTAMINATED = [
    """UPDATE routing_decisions
    SET is_real = 0
    WHERE is_real IS NULL
       OR final_model LIKE 'test/%'
       OR (cost_usd = 0.01 AND final_provider = 'test')
       OR final_model IS NULL""",
]
"""One-time fixup to mark contaminated routing records with is_real=0 (v7.5).

Marks 1,974 test/demo records as contaminated but retains them for audit trail.
This migration runs idempotently — subsequent runs are no-ops after first execution.

DO NOT RELY ON `is_real` AS A PROVENANCE FILTER (T-05, audit 2026-09-22).

This docstring used to claim *"All downstream analytics queries use
`WHERE is_real = 1` to filter them out."* That was false, and it was load-bearing
false: it is the sentence that made five money surfaces look already-protected.
What `grep` actually shows, at the time of writing:

* **No query in this module** mentions `is_real` at all — the column is written
  by the migration above and read nowhere in `cost.py`.
* Four queries elsewhere use it: `tools/dashboard.py:236` and
  `commands/verify.py:251` with a real `= 1`, and `hooks/session-end.py:1124`
  and `:1137` as `(is_real = 1 OR is_real IS NULL)` — which admits every
  unmarked row and so filters nothing on a column that is NULL by default.

`is_real` also carries `DEFAULT 1`, the same defect `provenance` was introduced
to avoid: a column named "is real" that asserts 1 about rows nobody measured.

The supported filters are :func:`production_only` for the money tables and
:func:`routing_production_only` for `routing_decisions`. If you are about to add
`is_real` to a WHERE clause, use one of those instead."""

MIGRATE_ADD_QUOTA_SNAPSHOTS_TABLE = [
    """CREATE TABLE IF NOT EXISTS quota_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT DEFAULT (datetime('now')),
        session_id TEXT NOT NULL,
        prompt_sequence INTEGER NOT NULL,
        prompt_hash TEXT,
        claude_session_pct REAL NOT NULL,
        claude_weekly_pct REAL NOT NULL,
        claude_sonnet_pct REAL NOT NULL,
        openai_spent_usd REAL NOT NULL DEFAULT 0,
        gemini_spent_usd REAL NOT NULL DEFAULT 0,
        ollama_available INTEGER NOT NULL DEFAULT 1,
        cache_age_seconds REAL NOT NULL,
        was_cache_fresh INTEGER NOT NULL,
        routing_decision_id INTEGER,
        final_model TEXT,
        final_provider TEXT,
        complexity_requested TEXT,
        complexity_used TEXT,
        was_downgraded INTEGER DEFAULT 0
    )""",
]
"""Idempotent migration to create quota_snapshots table (v7.5)."""

async def _column_exists(db: aiosqlite.Connection, table: str, column: str) -> bool:
    """Return True if *column* exists in *table* (uses SQLite PRAGMA, no exceptions).
    
    SECURITY: the identifier is pattern-checked AND confirmed against
    `sqlite_master` with a bound parameter before any interpolation.

    S6 (remediation II). This used a HAND-MAINTAINED allowlist of nine table
    names, and it had drifted: `codex_usage`, `gemini_usage` and `migrations`
    are all migrated by this module and none was on the list.

    The consequence was not a security hole — it was the opposite direction.
    An unmatched table returned False, meaning "the column does not exist",
    when the truth was "I cannot tell". `_safe_migrate` then ran the ALTER, it
    failed with `duplicate column name`, and the failure was recorded as a
    swallowed exception. Two such statements fired on EVERY database open and
    became the loudest code in the fail-open counter.

    Returning "I don't know" as "no" is the same defect class this audit kept
    finding in the money surfaces, in the opposite polarity: there, unknown
    provenance had to not count as production; here, an unknown table must not
    count as a missing column.

    The list is gone. Injection safety now comes from two checks that cannot
    drift: the identifier must match a strict pattern, and the table must
    actually exist in `sqlite_master` — confirmed with a PARAMETERISED query
    before the name is ever interpolated.
    """
    # A SQLite identifier, and nothing else. This is stricter than the old
    # allowlist for anything that is not a plain table name.
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table or ""):
        return False

    # Confirm the table exists, with the name as a BOUND PARAMETER. After this
    # returns a row, `table` is a real table name from this database's own
    # schema rather than a string someone passed in.
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (table,)
    )
    if await cursor.fetchone() is None:
        # The table does not exist yet, so the column cannot. This is a real
        # "no", unlike the old unknown-table "no".
        return False

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
    except Exception as exc:
        # S2 (remediation II). "The column is already there" is the migration
        # SUCCEEDING at being idempotent. Recording it as a swallowed failure
        # made the counter's baseline 4-per-database-open instead of zero, and
        # a counter whose zero is unreachable cannot signal anything.
        #
        # Worse, it made the counter self-inflating: `doctor --audit` opens the
        # database, so READING the fail-open count raised it by 4, and figures
        # published from it were partly measuring the diagnostic.
        if "duplicate column name" in str(exc).lower():
            return
        # Anything else is a real migration failure: a spike means schema
        # migration is silently not happening, and every later query then fails
        # on a missing column somewhere far from here.
        from llm_router import failopen
        failopen.record("CHZ-FO-COST-MIGRATE-ALTER", exc)


def _mark_worker_daemon(conn: "aiosqlite.Connection") -> None:
    """Best-effort mark the aiosqlite worker thread as a daemon.

    aiosqlite's ``_connection_worker_thread`` is non-daemon by default. If a
    task holding a connection is dropped at event-loop shutdown (its
    ``finally: await db.close()`` never runs), a non-daemon worker keeps the
    interpreter alive forever — the CHZ-AUD-026 hang-at-exit bug.

    aiosqlite >=0.22 keeps the worker in a private ``_thread``; on older
    releases the Connection itself was a ``threading.Thread``. We only touch
    an object that is genuinely a Thread, so an unexpected layout fails
    loudly (via the caller) rather than silently stamping a junk ``daemon``
    attribute on the Connection and leaving the real worker non-daemon.
    Setting ``daemon`` on an already-started thread raises RuntimeError; that
    is fine — a started worker was already daemon-marked pre-await.
    """
    # CHZ-PY-004: delegate to the single shared implementation so every
    # aiosqlite.connect() site marks its worker identically (no per-file drift).
    from llm_router.aiosqlite_util import mark_worker_daemon
    mark_worker_daemon(conn)


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
    # Secure file before creation (stores sensitive cost/token data)
    if not config.llm_router_db_path.exists():
        config.llm_router_db_path.touch(mode=0o600)
    # aiosqlite.Connection is a threading.Thread subclass that only starts
    # when awaited. Mark it daemon *before* awaiting: if a task holding this
    # connection is dropped at event-loop shutdown (its ``finally: await
    # db.close()`` never runs), a non-daemon worker thread would keep the
    # interpreter alive forever — this was the hang-at-exit bug. Daemon
    # threads cannot block exit; WAL journaling keeps the DB file safe even
    # if such a leaked thread is killed mid-write.
    _conn = aiosqlite.connect(str(config.llm_router_db_path))
    # Mark the worker daemon *before* awaiting (the thread hasn't started yet).
    _mark_worker_daemon(_conn)
    db = await _conn
    # Defensive second pass: on some aiosqlite versions the worker Thread is
    # only reachable after the connection is awaited. Re-mark it daemon so a
    # leaked worker can never keep the interpreter alive at exit (the
    # hang-at-exit bug). is_alive() daemon-setting is a no-op if already set.
    _mark_worker_daemon(db)
    # M-06. WAL mode allows concurrent readers while a writer is active -- but
    # `busy_timeout` must be set FIRST. It governs how long the journal_mode
    # PRAGMA itself waits for the exclusive lock it needs, so setting it
    # afterwards (or not at all, as here) leaves the single statement that most
    # needs it on SQLite's 5-second default. Measured at 12 concurrent cold
    # starts: 1 in 12 raised `database is locked` from this line.
    #
    # The PRAGMA also reports failure by RETURNING the mode in effect rather
    # than raising, so losing the race non-exceptionally yields "delete" and the
    # connection proceeds in rollback-journal mode -- where a writer blocks every
    # reader -- with nothing logged. `sqlite_wal.enable_wal` handles both, but it
    # is synchronous; this is the aiosqlite path, so the same ordering and the
    # same return check are done inline.
    await db.execute("PRAGMA busy_timeout = 5000")
    try:
        _row = await (await db.execute("PRAGMA journal_mode = WAL")).fetchone()
        _mode = (_row[0] if _row else "") or ""
        if _mode.lower() != "wal":
            import logging as _lg

            _lg.getLogger("llm_router").warning(
                "usage.db: WAL not established (mode=%s); continuing in "
                "rollback-journal mode with reduced concurrency", _mode or "unknown",
            )
    except Exception as _wal_exc:  # noqa: BLE001 — a cold-start race must not break routing
        from llm_router import failopen

        failopen.record("CHZ-FO-COST-WAL", _wal_exc)
    await db.execute(CREATE_TABLE)
    await db.execute(CREATE_CLAUDE_USAGE_TABLE)
    await db.execute(CREATE_ROUTING_DECISIONS_TABLE)
    await db.execute(CREATE_SAVINGS_STATS_TABLE)
    await db.execute(CREATE_SEMANTIC_CACHE_TABLE)
    await db.execute(CREATE_SEMANTIC_CACHE_INDEX)
    await db.execute(CREATE_CORRECTIONS_TABLE)
    await db.execute(CREATE_MODEL_QUALITY_TRENDS_TABLE)
    await db.execute(CREATE_BENCHMARK_RESULTS_TABLE)
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
        MIGRATE_CLAUDE_USAGE_CACHE_TOKENS
        + MIGRATE_ADD_CODEX_USAGE_TABLE
        + MIGRATE_ADD_GEMINI_USAGE_TABLE
        + MIGRATE_USAGE_ROUTING_OVERHEAD
        + MIGRATE_CLAUDE_USAGE_ADD_SAVINGS
        + MIGRATE_ROUTING_DECISIONS_ADD_FEEDBACK
        + MIGRATE_ROUTING_DECISIONS_ADD_REASON
        + MIGRATE_USAGE_ADD_SAVINGS
        + MIGRATE_USAGE_ADD_TEAM
        + MIGRATE_USAGE_ADD_COMPLEXITY
        + MIGRATE_SAVINGS_STATS_ADD_HOST
        + MIGRATE_SAVINGS_STATS_ADD_TOKENS
        + MIGRATE_SAVINGS_STATS_ADD_MODE
        + MIGRATE_SIBLING_TABLES_ADD_PROVENANCE
        + MIGRATE_ROUTING_DECISIONS_ADD_POLICY
        + MIGRATE_ADD_CORRELATION_ID
        + MIGRATE_ADD_CACHE_METRICS
        + MIGRATE_ROUTING_DECISIONS_ADD_JUDGE_SCORE
        + MIGRATE_ROUTING_DECISIONS_ADD_COMPLEXITY_TRACKING
        + MIGRATE_ROUTING_DECISIONS_ADD_AUDIT
        + MIGRATE_ROUTING_DECISIONS_ADD_CAPABILITIES
        + MIGRATE_ADD_MODEL_QUALITY_TRENDS
        + MIGRATE_ROUTING_DECISIONS_ADD_REAL_FLAG
        + MIGRATE_ROUTING_DECISIONS_MARK_CONTAMINATED
        + MIGRATE_ADD_QUOTA_SNAPSHOTS_TABLE
        + MIGRATE_ROUTING_DECISIONS_ADD_SUBJECT
        + MIGRATE_ROUTING_DECISIONS_ADD_PROVENANCE
        # Defined in v6.2 and never applied: compression_stats was declared,
        # log_compression_stat wrote to it, and the table did not exist. The
        # write raised OperationalError straight into bash-compress's bare
        # `except (ImportError, Exception): pass`, so every compression was
        # recorded nowhere and the absence looked like 'nothing compressed'.
        + MIGRATE_ADD_COMPRESSION_STATS
        + MIGRATE_USAGE_PROVENANCE_CUTOVER_TABLE
    )
    for stmt in all_migrations:
        await _safe_migrate(db, stmt)

    # Phase 2: replace the DEFAULT-0 lie on historical rows with an honest NULL.
    await _apply_provenance_cutover(db)

    # Quality tracking indices for v6.4 (created after migrations so judge_score exists)
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_routing_quality ON routing_decisions(final_model, judge_score, timestamp DESC) WHERE judge_score IS NOT NULL"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_model_quality_trends ON model_quality_trends(model, window_start DESC)"
    )

    # Plan 07 Cat E — bandit aggregation index. The bandit groups outcomes by
    # (profile, subject, final_model) on every selection; without this index the
    # query degenerates into a full scan once routing_decisions exceeds ~10k rows.
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_routing_bandit "
        "ON routing_decisions(profile, subject, final_model) "
        "WHERE subject IS NOT NULL"
    )

    # Plan 07 Cat G.3 — index for chronological regression-detector scans:
    # `SELECT … FROM benchmark_results WHERE policy = ? AND benchmark = ? ORDER BY timestamp`.
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_benchmark_results_lookup "
        "ON benchmark_results(policy, benchmark, timestamp)"
    )

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
    except Exception as exc:
        # Empty identity means rows are attributed to nobody. Team reporting then
        # shows a plausible, quietly incomplete picture.
        from llm_router import failopen
        failopen.record("CHZ-FO-COST-IDENTITY", exc)
        return "", ""


async def log_usage(
    response: LLMResponse,
    task_type: TaskType,
    profile: RoutingProfile,
    success: bool = True,
    correlation_id: str | None = None,
    complexity: str = "moderate",
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
        complexity: Task complexity level (simple, moderate, complex).
    """
    # PRIMARY GUARD: a test must not write to the production database. See
    # `_refuse_unisolated_test_write` for why the fingerprint below was not enough.
    if _refuse_unisolated_test_write(get_config().llm_router_db_path):
        return

    # Secondary, retained: rejects the exact synthetic shapes used in some test
    # LLMResponse fixtures (input_tokens=100, output_tokens∈{50,100},
    # cost_usd∈{0.001,0.003}).
    #
    # This was ONCE THE ONLY GUARD, and its comment claimed unisolated tests "can
    # never pollute the real ~/.llm-router/usage.db". Measured against the rows that
    # reached production, it would have blocked 0 of 28,536 (0.0%): the fixtures in
    # use are in=62/out=164, in=74/out=1, in=97/out=126 — none match. It is kept only
    # because it costs nothing; it is not load-bearing and must not be treated as such.
    if (
        os.environ.get("LLM_ROUTER_ALLOW_STUBS") != "1"
        and response.input_tokens == 100
        and response.output_tokens in (50, 100)
        and response.cost_usd in (0.001, 0.003)
    ):
        return

    user_id, project_id = _get_team_identity()
    db = await _get_db()
    try:
        # Local providers (ollama, codex) are free — override any calculated cost
        cost_usd = 0.0 if response.provider in {"ollama", "codex", "gemini_cli"} else response.cost_usd

        # v9.4.0: compute counterfactual baseline so the dashboard's
        # realized-savings metric has data. Previously baseline_model was
        # NULL and potential_cost_usd/saved_usd defaulted to 0.0, so every
        # routed call appeared to save nothing.
        baseline_model = _pricing.savings_baseline_model()
        potential_cost_usd = _claude_cost(
            baseline_model,
            response.input_tokens,
            response.output_tokens,
            cache_write_t=response.cache_creation_input_tokens,
            cache_read_t=response.cache_read_input_tokens,
        )
        saved_usd = potential_cost_usd - cost_usd

        await db.execute(
            # C-02. `is_simulated` was declared (ALTER TABLE, above) and filtered
            # on (`get_savings_by_period`) but NEVER WRITTEN -- this is the only
            # INSERT into `usage`, and it omitted the column. The filter
            # `AND is_simulated IS NOT 1` therefore excluded nothing, ever, while
            # reading as protective.
            #
            # The measured consequence: reported savings +$83.49, actual -$1.15
            # once fixtures were removed. 1,813 test rows carried REAL model
            # names, so the name-based `_is_test_model` filter could not see them
            # -- and applying it moved the figure FURTHER from truth (+$87.96).
            #
            # Provenance is stamped here, at write time, by the same
            # `detect_synthetic()` the routing ledger uses. A name heuristic
            # applied at read time cannot be made correct; this can.
            """INSERT INTO usage (model, provider, task_type, profile,
               input_tokens, output_tokens, cost_usd, latency_ms, success,
               user_id, project_id, correlation_id, complexity,
               baseline_model, potential_cost_usd, saved_usd, is_simulated)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                response.model,
                response.provider,
                task_type.value,
                profile.value,
                response.input_tokens,
                response.output_tokens,
                cost_usd,
                response.latency_ms,
                1 if success else 0,
                user_id or None,
                project_id or None,
                correlation_id,
                complexity,
                baseline_model,
                potential_cost_usd,
                saved_usd,
                1 if _detect_synthetic() else 0,
            ),
        )
        await db.commit()
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


def format_spend_for_display(spend_usd: float) -> str:
    """Render a spend figure for humans, or "Unknown" when it is not a number.

    Spend getters return ``inf`` when a component could not be read (fail
    closed). That sentinel is correct for a cap COMPARISON and wrong for a
    dashboard: a user told they spent "$inf" learns less than one told
    "Unknown", and a fabricated infinity is the same class of lie as a
    fabricated zero.
    """
    import math

    if spend_usd is None or not math.isfinite(spend_usd):
        return "Unknown"
    return f"${spend_usd:.2f}" if spend_usd >= 1.0 else f"${spend_usd:.4f}"


# ── Provenance: the one clause every money surface must carry ────────────────
#
# T-05 (audit 2026-09-22). The 2026-09-21 C-02 fix added write-time provenance
# to `usage` and a filter to ONE reader, `get_savings_by_period`. Five sibling
# surfaces read the same rows with no filter at all, including the one that
# broadcasts to a shared Slack/Discord channel — a synthetic $3.00 went out
# under the same code path that correctly reported $0.00 on the dashboard.
#
# The fix is a shared fragment rather than six hand-written copies, because six
# copies is how the first five came to disagree. `tests/test_t05_money_surfaces
# _are_provenance_filtered.py` enumerates the surfaces and fails when a new one
# appears without this clause.

PROVENANCE_COLUMN = "is_simulated"


def production_only(include_simulated: bool = False, *, prefix: str = "AND",
                    table: str = "") -> str:
    """WHERE fragment that keeps synthetic rows out of a money figure.

    FAIL-CLOSED, and the `= 0` is the whole point. `IS NOT 1` admits NULL, so a
    row whose provenance was never established counts as production — the same
    defect as `is_evaluable` treating a missing field as real. Only a row
    explicitly stamped 0 at write time is production data.

    `include_simulated` is the named escape hatch for tests that exercise the
    aggregate arithmetic over rows they wrote themselves (pytest stamps every
    such row synthetic). No production caller passes it, and the default stays
    exclusive.
    """
    if include_simulated:
        # An empty string is only safe where the caller APPENDS the fragment
        # (`f"{where} {production_only(...)}"`). Five callers instead EMBED it
        # (`f"WHERE {production_only(..., prefix='')} AND date(...)"`), and there
        # an empty fragment composes to the literal `WHERE  AND date(...)` —
        # `sqlite3.OperationalError: near "AND": syntax error`. So a caller that
        # supplies no prefix is embedding, and gets a predicate that is always
        # true rather than nothing at all.
        #
        # Found by the agent updating the tests for this change, on the first
        # call that passed `include_simulated=True`. The escape hatch had never
        # been exercised on these five surfaces.
        return "" if prefix else "1=1"
    col = f"{table}.{PROVENANCE_COLUMN}" if table else PROVENANCE_COLUMN
    return f"{prefix} {col} = 0".strip()


async def _count_unknown_provenance(db, where: str) -> int:
    """How many rows in this window predate the provenance writer.

    Reported alongside every routing_decisions figure so the denominator is
    visible. A number quoted without saying how much of its population has
    unrecorded origin is a rate without its denominator (repo CLAUDE.md).
    Returns 0 on failure rather than raising — this is disclosure, and it must
    never be the reason a report cannot render.
    """
    try:
        cur = await db.execute(f"SELECT COUNT(*) FROM routing_decisions {where}")
        row = await cur.fetchone()
        return int(row[0]) if row else 0
    except Exception:  # noqa: BLE001
        return 0


def routing_production_only(include_synthetic: bool = False, *, prefix: str = "AND") -> str:
    """WHERE fragment excluding `routing_decisions` rows that SAY they are synthetic.

    NOT symmetric with :func:`production_only`, and the asymmetry is the point.

    `usage.is_simulated` is a money column: an unmeasured row counted as dollars
    is the C-02 failure, so NULL is excluded and the filter is fail-closed.
    `routing_decisions.provenance` is a three-state attribution column
    (`llm_router.attribution`): `runtime` = real traffic, `unattributed`/`test` =
    synthetic, NULL = written before the writer existed. There are thousands of
    those NULL rows and they are genuinely UNKNOWN, not genuinely synthetic;
    dropping them would silently shrink every share denominator — "a filter that
    drops nothing has not been shown to work" has a twin, which is a filter that
    drops everything and reports a clean zero.

    So this excludes only what is explicitly marked, and every caller that turns
    these rows into a number ALSO reports `unknown_provenance_rows`, so the
    denominator is visible rather than assumed.
    """
    if include_synthetic:
        return ""
    from llm_router.attribution import UNATTRIBUTED_PROVENANCE
    marked = ", ".join(f"'{v}'" for v in sorted(UNATTRIBUTED_PROVENANCE))
    return f"{prefix} (provenance IS NULL OR provenance NOT IN ({marked}))".strip()


ROUTING_UNKNOWN_PROVENANCE_SQL = "provenance IS NULL"
"""Rows whose origin predates the provenance writer. Counted and disclosed, never
promoted into either bucket — see `llm_router.attribution.Attribution.UNKNOWN`."""


async def get_monthly_spend(*, include_simulated: bool = False) -> float:
    """Get total USD spent on external LLMs in the current calendar month.

    RED1-07: uses a LOCAL-time month boundary to match get_daily_spend* (both
    reference the user's local calendar), so "today" for the daily cap is always
    a consistent subset of "this month" for the monthly cap. Previously this used
    a UTC 'start of month' while the daily functions used 'localtime', so at
    non-UTC offsets the two caps' reset windows disagreed by up to the offset
    around a month boundary.

    Returns:
        Total spend as a float. Returns 0.0 if no usage data exists.
    """
    db = await _get_db()
    try:
        # Mirror get_daily_spend's frame exactly: convert the stored (UTC)
        # timestamp to LOCAL, then compare the local year-month. Using
        # strftime(..., 'localtime') on both sides keeps the reference frame
        # identical to the daily function (which does date(timestamp,'localtime')
        # = date('now','localtime')), so daily-today is always inside monthly-now.
        cursor = await db.execute(
            # T-05. This gates a REAL budget cap. A benchmark run's synthetic
            # dollars could trip it and throttle legitimate routing — the T-05
            # failure pointed the opposite way from the broadcast one.
            f"SELECT COALESCE(SUM(cost_usd), 0) FROM usage "
            f"WHERE {production_only(include_simulated, prefix='')} AND "
            "strftime('%Y-%m', timestamp, 'localtime') = "
            "strftime('%Y-%m', 'now', 'localtime')"
        )
        row = await cursor.fetchone()
        winning = float(row[0]) if row else 0.0
        # RED1-2-01: include this month's billable-but-rejected attempts, so the
        # monthly hard-block ceiling sees the same real spend the daily cap does.
        return winning + await _rejected_attempt_spend(db, "month")
    finally:
        await db.close()


async def _rejected_attempt_spend(db, period: str = "day", task_type: str | None = None) -> float:
    """RED1-08/RED1-2-01: sum billable-but-REJECTED provider attempts for a period.

    ``cost.log_usage`` only records the WINNING attempt to the ``usage`` table,
    so a paid model that was tried, billed, then rejected (by a contract gate or
    quality escalation) is invisible to the cap-checks that read ``usage``. The
    execution ledger records every attempt with ``rejected`` + ``measured_cost_usd``,
    and the winning attempt is separately marked ``accepted`` — so summing only
    ``rejected=1`` rows gives exactly the extra cost missing from ``usage``, with
    no double-count.

    ``period`` is "day" (local calendar day) or "month" (local calendar month),
    matching the frames of get_daily_spend*/get_monthly_spend respectively.
    FAIL CLOSED (owner decision 2026-08-12). Any error returns ``inf``, not 0.0.

    Returning 0.0 under-reports total spend, so the cap comparison PASSES — a
    guard that cannot read the ledger did not reject, it silently approved. Same
    failure direction as the budget TOCTOU race and the savings query that
    rendered "$0.00 saved": failing where it looks harmless, which is why it
    survived. ``inf`` makes every cap comparison deny.

    Routing is not broken by this: free and local providers do not consult the
    cap, so work continues — money is simply not spent against a total we cannot
    account for. Display consumers must render non-finite spend as "Unknown"
    (see :func:`format_spend_for_display`); a dashboard showing "$inf" is its own
    fabrication.
    """
    try:
        if period == "month":
            time_pred = (
                "strftime('%Y-%m', ts, 'unixepoch', 'localtime') = "
                "strftime('%Y-%m', 'now', 'localtime')"
            )
        else:  # day
            time_pred = "date(ts, 'unixepoch', 'localtime') = date('now','localtime')"
        where = (
            f"rejected = 1 AND COALESCE(measured_cost_usd, 0) > 0 AND {time_pred}"
        )
        params: tuple = ()
        if task_type is not None:
            where += " AND task_type = ?"
            params = (task_type,)
        cursor = await db.execute(
            f"SELECT COALESCE(SUM(measured_cost_usd), 0) FROM execution_events WHERE {where}",
            params,
        )
        row = await cursor.fetchone()
        return float(row[0]) if row else 0.0
    except Exception as exc:  # noqa: BLE001 — must not raise into the routing path
        # A MISSING TABLE is not an unreadable one. On a fresh install
        # execution_events does not exist yet, and that genuinely means "no
        # rejected attempts" — returning inf there would deny every paid route on
        # a new machine until the first ledger write, which is an outage dressed
        # as prudence. Fail closed on the unknown, not on the known-empty.
        if "no such table" in str(exc).lower():
            return 0.0
        from llm_router import failopen
        failopen.record("CHZ-FO-COST-CAP-LEDGER-READ", exc)
        # inf, not 0.0 — see the docstring. Deny rather than approve blind.
        return float("inf")


async def _rejected_attempt_spend_today(db, task_type: str | None = None) -> float:
    """Back-compat shim: today's rejected-attempt spend (delegates to _rejected_attempt_spend)."""
    return await _rejected_attempt_spend(db, "day", task_type)


async def get_daily_spend(*, include_simulated: bool = False) -> float:
    """Get total USD spent on external LLMs today (local calendar day).

    Includes both winning calls (``usage`` table) and billable-but-rejected
    provider attempts (execution ledger, RED1-08), so the cap-check sees the real
    cumulative spend — not just the spend of calls that happened to be accepted.

    Returns:
        Total spend as a float. Returns 0.0 if no usage data exists.
    """
    db = await _get_db()
    try:
        cursor = await db.execute(
            # T-05: gates a real daily cap — see get_monthly_spend.
            f"SELECT COALESCE(SUM(cost_usd), 0) FROM usage "
            f"WHERE {production_only(include_simulated, prefix='')} AND "
            "date(timestamp,'localtime') = date('now','localtime')"
        )
        row = await cursor.fetchone()
        winning = float(row[0]) if row else 0.0
        return winning + await _rejected_attempt_spend_today(db)
    finally:
        await db.close()


async def get_daily_spend_by_task_type(task_type: str, *,
                                       include_simulated: bool = False) -> float:
    """Get total USD spent on external LLMs today for a specific task type.

    Args:
        task_type: Task type string (e.g., 'query', 'code', 'research', 'generate', 'analyze').

    Returns:
        Total spend for that task type as a float. Returns 0.0 if no usage data exists.
    """
    db = await _get_db()
    try:
        cursor = await db.execute(
            # T-05: gates a real per-task cap — see get_monthly_spend.
            f"SELECT COALESCE(SUM(cost_usd), 0) FROM usage "
            f"WHERE {production_only(include_simulated, prefix='')} AND "
            "date(timestamp,'localtime') = date('now','localtime') AND task_type = ?",
            (task_type,),
        )
        row = await cursor.fetchone()
        winning = float(row[0]) if row else 0.0
        # RED1-08: include rejected billable attempts for this task type.
        return winning + await _rejected_attempt_spend_today(db, task_type)
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
    except Exception as exc:
        # Best-effort by design, but a budget alert that never fires means the
        # user learns about an overrun from the bill.
        from llm_router import failopen
        failopen.record("CHZ-FO-COST-BUDGET-ALERT", exc)


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
        "today": "WHERE date(timestamp, 'localtime') = date('now', 'localtime')",
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


def _validate_routing_insert(
    final_model: str,
    final_provider: str,
    cost_usd: float,
) -> None:
    """Validate routing_decisions insert parameters. Raises ValueError on invalid data.
    
    Prevents contaminated test data from entering production database.
    
    Checks:
    - final_provider is in allowlist of real providers
    - final_model doesn't look like test data
    - cost_usd is within plausible range
    
    Raises:
        ValueError: On invalid provider, test-like model, or implausible cost
    """
    # R11 / T-20 (third instance). This hand-listed 'gemini' and omitted
    # 'google' -- the name model_registry actually assigns to every Gemini
    # model. So every real Gemini routing decision raised ValueError below,
    # was swallowed at router.py's `except Exception: log.warning(...)`, and
    # never reached routing_decisions.
    #
    # The canonical sets were created by the commit that fixed the SECOND
    # instance of this class, in quota_tracker, and this site was not updated.
    # Importing beats re-listing: a set that is copied is a set that drifts.
    from llm_router.model_registry import GOOGLE_PROVIDERS, OPENAI_PROVIDERS

    VALID_PROVIDERS = frozenset({
        'ollama', 'codex',
        'claude_subscription', 'subscription', 'anthropic',
        'perplexity', 'groq', 'deepseek', 'cc',
        'claude',  # variations
    }) | GOOGLE_PROVIDERS | OPENAI_PROVIDERS

    # Check provider is valid
    if final_provider not in VALID_PROVIDERS:
        raise ValueError(
            f"routing_decisions insert rejected: invalid provider '{final_provider}'. "
            f"Valid providers: {sorted(VALID_PROVIDERS)}. "
            f"If this is a test, use LLM_ROUTER_DB_PATH to isolate test databases."
        )

    # Check model doesn't look like test data
    if not final_model or final_model.startswith('test/'):
        raise ValueError(
            f"routing_decisions insert rejected: model '{final_model}' looks like test data. "
            f"Real models include 'gpt-4o', 'claude-opus', 'gemini-pro', etc. "
            f"If this is a test, use LLM_ROUTER_DB_PATH to isolate test databases."
        )

    # Check cost is plausible
    if cost_usd < 0 or cost_usd > 100:
        raise ValueError(
            f"routing_decisions insert rejected: cost_usd={cost_usd} is implausible. "
            f"Expected 0 < cost < 100 USD. Real costs: Haiku ~$0.00002, Opus ~$0.015 per 1K tokens."
        )


#: Written into `routing_decisions.provenance` on every new row.
PROVENANCE_RUNTIME = "runtime"      # a real routing decision, made while serving a user
PROVENANCE_TEST = "test"            # produced under a test harness or with stubs allowed
PROVENANCE_UNATTRIBUTED = "unattributed"   # retroactively marked; see 0aab32f


def _write_provenance() -> str:
    """Where this row came from, decided by the writer at insert time.

    Until now nothing wrote this column, so EVERY row — genuine or synthetic — was born
    `NULL`. `0aab32f` then marked one known-bad population `unattributed` after the fact,
    which made `provenance IS NULL` *look* like "real traffic" when it actually meant
    "not yet cleaned up". A second synthetic population (2,373 rows, one prompt_hash,
    a fixed 3.200:1 model split) sat inside that NULL set and was reported as routing.

    Recording origin at the point of writing is the only version of this that cannot
    drift: a cleanup pass can always be out of date, and a reader cannot recover a fact
    the writer never stored.

    `LLM_ROUTER_ALLOW_STUBS=1` counts as test provenance. It is the documented escape hatch
    for writing stub data deliberately, and data written through an escape hatch is not
    user traffic — the flag says so.
    """
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("LLM_ROUTER_ALLOW_STUBS") == "1":
        return PROVENANCE_TEST
    return PROVENANCE_RUNTIME


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
    subject: str | None = None,
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
    # Validate inputs before database insert
    _validate_routing_insert(final_model, final_provider, cost_usd)

    # THIS is the path that put 28,536 synthetic rows into a user's real database and
    # made the dashboard report a 69% gpt-4o-mini share the router never chose.
    # `_validate_routing_insert` above rejects obviously-fake models ("test/..."), but
    # these rows named a real model with realistic tokens and costs, so nothing stopped
    # them. Isolation, not plausibility, is the property that matters here.
    if _refuse_unisolated_test_write(get_config().llm_router_db_path):
        return

    db = await _get_db()
    try:
        # Track complexity mismatch: if requested_complexity differs from final complexity,
        # a pressure downgrade occurred (e.g., complex→moderate when budget high)
        complexity_downgraded = 1 if requested_complexity and requested_complexity != complexity else 0

        # Shadow mode: record what capability-aware routing WOULD have decided,
        # without letting it touch the decision above. `capability_routing_enabled`
        # gates it, so this is None on every install that has not opted in.
        # Fail-open — a shadow observation must never cost us the real record.
        capabilities_json: str | None = None
        try:
            from llm_router.capabilities import (
                capability_routing_enabled,
                detect_capabilities,
                serialize_capability_decision,
            )

            if capability_routing_enabled():
                capabilities_json = serialize_capability_decision(
                    detect_capabilities(prompt, task_type)
                )
        except Exception as _cap_err:  # noqa: BLE001
            # Module-local import: cost.py has no module-level logger, and
            # adding one here for a shadow path would be a wider change than
            # this warrants.
            import logging

            logging.getLogger("llm_router").debug(
                "capability_shadow_detection_failed: %s", _cap_err
            )

        await db.execute(
            """INSERT INTO routing_decisions
               (prompt_hash, task_type, profile, classifier_type, classifier_model,
                classifier_confidence, classifier_latency_ms, complexity,
                recommended_model, base_model, was_downshifted, budget_pct_used,
                quality_mode, final_model, final_provider, success,
                input_tokens, output_tokens, cost_usd, latency_ms, reason_code,
                correlation_id, requested_complexity, complexity_downgraded, subject,
                provenance, capabilities_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                subject,
                _write_provenance(),
                capabilities_json,
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
            except Exception as exc:
                # The judge is optional, but a permanently failing judge means
                # quality telemetry is empty rather than good.
                from llm_router import failopen
                failopen.record("CHZ-FO-COST-JUDGE-EVAL", exc)
    finally:
        await db.close()


async def get_quality_report(days: int = 7, *, include_synthetic: bool = False) -> dict:
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
    # T-05. `where` feeds EVERY query in this function, so the provenance filter
    # goes here once rather than being re-decided per query — six hand-written
    # copies is how the surfaces came to disagree in the first place.
    where = (f"WHERE timestamp >= datetime('now', '-{days} days') "
             f"{routing_production_only(include_synthetic)}")
    unknown_where = (f"WHERE timestamp >= datetime('now', '-{days} days') "
                     f"AND {ROUTING_UNKNOWN_PROVENANCE_SQL}")

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
                "unknown_provenance_rows": await _count_unknown_provenance(db, unknown_where),
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

        # By model — ATTRIBUTED ONLY, i.e. decisions this router actually made.
        #
        # A row with classifier_type='unknown' is one where the classifier never ran, so
        # it says nothing about routing behaviour. Mixing the two made the dashboard
        # report the opposite of the truth: 28,536 such rows (69.4% of the table, every
        # one of them classifier_type='unknown') all named openai/gpt-4o-mini and were
        # written by an unisolated TEST SUITE into the user's real ~/.llm-router/usage.db.
        # The report showed a 69% gpt-4o-mini share for a model the router never chose,
        # while the actual top destination — ollama/hermes3:8b, local, 38.6% — appeared
        # as 11.7%. Understating local routing threefold is the exact class of dishonesty
        # this codebase's audit exists to remove, sitting in its most-read surface.
        #
        # The unattributed rows are REPORTED, not dropped. Hiding them would restore a
        # tidy number and lose the signal that something is writing rows nobody can
        # account for — which is how this went unnoticed in the first place.
        attributed = f"{where} AND classifier_type != 'unknown'"
        cursor = await db.execute(
            f"""SELECT final_model, COUNT(*), AVG(latency_ms), COALESCE(SUM(cost_usd), 0)
                FROM routing_decisions {attributed}
                GROUP BY final_model ORDER BY COUNT(*) DESC"""
        )
        by_model = {
            r[0]: {"count": r[1], "avg_latency": float(r[2]), "total_cost": float(r[3])}
            for r in await cursor.fetchall()
        }

        cursor = await db.execute(
            f"""SELECT final_model, COUNT(*) FROM routing_decisions
                {where} AND classifier_type = 'unknown'
                GROUP BY final_model ORDER BY COUNT(*) DESC"""
        )
        unattributed_by_model = {r[0]: r[1] for r in await cursor.fetchall()}
        unattributed_total = sum(unattributed_by_model.values())

        # Unattributed rows carry COST, so they do not merely mis-decorate a table.
        # `total_cost_usd` feeds RouteredTeam._apply_budget_pressure in
        # integrations/agno.py, which downshifts every model to the budget profile once
        # spend crosses a threshold. Measured on this database: $3.62 of the last 30
        # days' $39.79 (9.1%) is unattributed, so a real deployment could be downshifted
        # early by spend that never happened.
        #
        # `total_cost_usd` is left as-is because changing what agno reads changes runtime
        # behaviour, which is a separate decision from reporting. This exposes the honest
        # figure alongside it so that decision can be made on numbers rather than guesses.
        cursor = await db.execute(
            f"""SELECT COALESCE(SUM(cost_usd), 0) FROM routing_decisions
                {where} AND classifier_type != 'unknown'"""
        )
        attributed_cost = (await cursor.fetchone())[0]

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
            # Decisions the classifier actually made. NOT the same as total_decisions.
            "by_model": by_model,
            "attributed_decisions": int(total) - unattributed_total,
            "attributed_cost_usd": float(attributed_cost),
            # Rows where the classifier never ran. Surfaced deliberately — see above.
            "unattributed_decisions": unattributed_total,
            "unattributed_by_model": unattributed_by_model,
            "unattributed_reason": "classifier did not run (classifier_type='unknown')",
            # T-05. Rows marked synthetic are already excluded by `where`; these
            # are the ones whose origin was never recorded. Disclosed rather than
            # dropped: they are UNKNOWN, not proven fake, and silently deleting
            # them would shrink the denominator behind every rate above.
            "unknown_provenance_rows": await _count_unknown_provenance(db, unknown_where),
        }
    finally:
        await db.close()


# ── Claude Code token tracking ───────────────────────────────────────────────


# v9.2.2 — per-million-token rates split by token component, matching Claude
# Code's upstream 4-component billing formula:
#   cost = input_t × input_$/M + output_t × output_$/M
#        + cache_write_t × cache_write_$/M + cache_read_t × cache_read_$/M
#
# The numbers no longer live here. These three tables held their own copies of
# the Opus and Haiku rates and both were stale ($15/$75 is Opus *3*); they are
# now projections of llm_router.pricing, so the shape callers rely on survives and
# the drift does not. The per-provider key lists are deliberate: _claude_cost
# must not silently price a Gemini model just because pricing.py knows it.
def _rate_table(names: tuple[str, ...]) -> dict[str, dict[str, float]]:
    """Project ``names`` out of llm_router.pricing into the legacy table shape."""
    out: dict[str, dict[str, float]] = {}
    for name in names:
        rates = _pricing.rates_per_m(name)
        if rates is not None:
            out[name] = rates
    return out


CLAUDE_RATES_PER_M: dict[str, dict[str, float]] = _rate_table(("haiku", "sonnet", "opus"))

# v9.3.0 — OpenAI models commonly invoked from Codex CLI.
OPENAI_RATES_PER_M: dict[str, dict[str, float]] = _rate_table(
    ("gpt-5.5", "gpt-5.4", "gpt-5-mini", "o3", "o3-mini", "gpt-4o", "gpt-4o-mini")
)


def _codex_cost(
    model: str,
    input_t: int,
    output_t: int,
    *,
    cache_write_t: int = 0,
    cache_read_t: int = 0,
) -> float:
    """Compute the $ cost of an OpenAI/Codex API call using the 4-component formula.

    Same shape as _claude_cost but uses OPENAI_RATES_PER_M. Unknown models
    return 0.0 (graceful — non-OpenAI routes don't trip this). v9.3.0.
    """
    rates = OPENAI_RATES_PER_M.get(model)
    if rates is None:
        return 0.0
    return (
        input_t       * rates["input"]       +
        output_t      * rates["output"]      +
        cache_write_t * rates["cache_write"] +
        cache_read_t  * rates["cache_read"]
    ) / 1_000_000


def _get_codex_baseline_for_task(task_type: str | None, complexity: str | None) -> str:
    """Pick the realistic Codex baseline — what would have been used without routing.

    Codex CLI defaults to gpt-5.4 / gpt-5.5 depending on availability. We use
    gpt-5.4 as the conservative middle baseline; complex tasks would have
    escalated to o3; simple queries would have used gpt-5-mini.

    Env override: LLM_ROUTER_CODEX_BASELINE (default: gpt-5.4). v9.3.0.
    """
    import os
    override = os.environ.get("LLM_ROUTER_CODEX_BASELINE", "").strip().lower()
    if override in OPENAI_RATES_PER_M:
        return override
    if task_type == "research":
        return "o3"
    if complexity == "complex":
        return "o3"
    if task_type in ("query",):
        return "gpt-5-mini"
    return "gpt-5.4"


# v9.3.1 — Gemini models commonly invoked from Gemini CLI.
GEMINI_RATES_PER_M: dict[str, dict[str, float]] = _rate_table(
    (
        "gemini-2.5-flash",
        "gemini-2.5-pro",
        "gemini-2.0-flash",
        "gemini-2.0-pro",
        "gemini-1.5-flash",
        "gemini-1.5-pro",
    )
)


def _gemini_cost(
    model: str,
    input_t: int,
    output_t: int,
    *,
    cache_write_t: int = 0,
    cache_read_t: int = 0,
) -> float:
    """Compute the $ cost of a Gemini API call using the 4-component formula.

    Same shape as _claude_cost / _codex_cost but uses GEMINI_RATES_PER_M.
    Unknown models return 0.0. v9.3.1.
    """
    rates = GEMINI_RATES_PER_M.get(model)
    if rates is None:
        return 0.0
    return (
        input_t       * rates["input"]       +
        output_t      * rates["output"]      +
        cache_write_t * rates["cache_write"] +
        cache_read_t  * rates["cache_read"]
    ) / 1_000_000


def _get_gemini_baseline_for_task(task_type: str | None, complexity: str | None) -> str:
    """Pick the realistic Gemini baseline — what would have been used without routing.

    Gemini CLI defaults to gemini-2.5-pro for complex tasks, gemini-2.5-flash
    for everything else. Simple queries could have gone to gemini-2.0-flash
    if user explicitly picked the cheap path.

    Env override: LLM_ROUTER_GEMINI_BASELINE (default: gemini-2.5-pro). v9.3.1.
    """
    import os
    override = os.environ.get("LLM_ROUTER_GEMINI_BASELINE", "").strip().lower()
    if override in GEMINI_RATES_PER_M:
        return override
    if task_type == "research":
        return "gemini-2.5-pro"
    if complexity == "complex":
        return "gemini-2.5-pro"
    if task_type in ("query",):
        return "gemini-2.0-flash"
    return "gemini-2.5-flash"


def _claude_cost(
    model: str,
    input_t: int,
    output_t: int,
    *,
    cache_write_t: int = 0,
    cache_read_t: int = 0,
) -> float:
    """Compute the actual $ cost of a Claude API call using the 4-component formula.

    Unknown models return 0.0 (graceful — non-Claude routes don't trip this).

    WP-05: ``CLAUDE_RATES_PER_M`` is keyed by FAMILY ALIAS ("opus", "sonnet",
    "haiku"), so a full model ID such as ``claude-opus-5`` missed the table and
    priced at 0.0 — silently, because the miss is indistinguishable from a
    legitimately-unpriced non-Claude route. Passing a baseline model ID through
    here therefore produced a zero baseline and NEGATIVE savings. Resolve
    through llm_router.pricing before giving up, so an ID that the price table knows
    can never read as free.
    """
    rates = CLAUDE_RATES_PER_M.get(model)
    if rates is None:
        # Claude IDs only. Widening this to every priced model would change what
        # the function means: callers rely on non-Claude routes costing 0.0 here
        # and being priced on their own provider's path.
        _resolved = _pricing.resolve(model)
        if _resolved is not None and _resolved.startswith("claude-"):
            rates = _pricing.rates_per_m(_resolved)
    if rates is None:
        return 0.0
    return (
        input_t       * rates["input"]       +
        output_t      * rates["output"]      +
        cache_write_t * rates["cache_write"] +
        cache_read_t  * rates["cache_read"]
    ) / 1_000_000


def calc_savings(
    model: str,
    tokens_used: int,
    *,
    task_type: str | None = None,
    complexity: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
    routing_overhead_usd: float = 0.0,
) -> tuple[float, float]:
    """Calculate net cost and time savings vs. the counterfactual baseline model.

    v9.2.2 — Three changes from the legacy behaviour:

    1. **Cache-aware**: when sub-component token counts are provided,
       cost is computed via the 4-component formula (input + output +
       cache_write + cache_read at separate rates) rather than a single
       lumped per-1K rate. The lumped path is kept for back-compat.
    2. **One baseline** (WP-05): the counterfactual is always
       `pricing.savings_baseline_model()`, regardless of task_type or
       complexity. The former task-aware picker (Haiku for simple Q&A,
       Sonnet for code, Opus for complex work) was a second savings policy
       and disagreed with every other surface by up to 5x.
    3. **No floor**: returned savings are NOT clamped to >= 0. Routing
       overhead can exceed gross savings on small prompts; that should
       surface as a negative number, not be hidden.

    Args:
        model: The model that was actually used.
        tokens_used: Total tokens (used when sub-components are all 0).
        task_type: Routing task type — when given, drives baseline selection.
        complexity: Complexity tier — used with task_type for baseline.
        input_tokens, output_tokens, cache_creation_input_tokens,
        cache_read_input_tokens: Sub-component token counts for the
            4-component cost formula. Pass when known from the API response.
        routing_overhead_usd: Estimated classifier + Ollama cost for this
            call. Subtracted from gross savings to give the realized number.

    Returns:
        (net_cost_saved_usd, net_time_saved_sec). May be negative.
    """
    # WP-05: one baseline, whatever the task. This used to credit against a
    # task-aware "realistic" model (query -> Haiku), on the reasoning that
    # measuring a Haiku-appropriate query at Opus rates overstates savings. That
    # reasoning priced a counterfactual nobody performs — a subscriber runs their
    # top model, they do not hand-pick a cheaper Claude per prompt — and it put
    # this function 5x apart from savings_logger, the dashboard and session-end
    # on the identical call. task_type/complexity remain in the signature for
    # callers and telemetry; they no longer select a baseline.
    baseline = _pricing.savings_baseline_model()

    # Cache-aware path: when any sub-component count is provided, use the
    # 4-component formula. Otherwise fall back to the lumped per-1K rate.
    sub_total = input_tokens + output_tokens + cache_creation_input_tokens + cache_read_input_tokens
    if sub_total > 0:
        actual_cost = _claude_cost(
            model, input_tokens, output_tokens,
            cache_write_t=cache_creation_input_tokens,
            cache_read_t=cache_read_input_tokens,
        )
        baseline_cost = _claude_cost(
            baseline, input_tokens, output_tokens,
            cache_write_t=cache_creation_input_tokens,
            cache_read_t=cache_read_input_tokens,
        )
        # Time savings still use lumped tokens — granularity not worth the noise here.
        effective_tokens = sub_total
    else:
        tokens_k = tokens_used / 1000
        actual_cost = tokens_k * MODEL_COST_PER_1K.get(model, 0)
        baseline_cost = tokens_k * MODEL_COST_PER_1K.get(baseline, MODEL_COST_PER_1K["opus"])
        effective_tokens = tokens_used

    gross_cost_saved = baseline_cost - actual_cost
    cost_saved = gross_cost_saved - routing_overhead_usd

    if effective_tokens > 0:
        actual_time = effective_tokens / MODEL_SPEED_TPS.get(model, 120)
        baseline_time = effective_tokens / MODEL_SPEED_TPS.get(baseline, MODEL_SPEED_TPS["opus"])
        time_saved = baseline_time - actual_time
    else:
        time_saved = 0.0

    # No floor — let negative savings surface honestly.
    return cost_saved, time_saved


async def log_claude_usage(
    model: str,
    tokens_used: int,
    complexity: str,
    *,
    task_type: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
    routing_overhead_usd: float = 0.0,
    cost_saved_usd: float | None = None,
) -> dict:
    """Log a Claude Code model invocation and its savings vs. the task-aware baseline.

    v9.2.2 — Extended signature accepts the 4 sub-component token counts plus
    routing overhead. `tokens_used` is computed from sub-components when 0
    (so callers with structured API responses can pass only the new kwargs).
    `cost_saved_usd` kwarg is accepted for backward compat with the router.py
    caller but is recomputed authoritatively from calc_savings.

    Returns:
        Dict with ``cost_saved_usd`` (net) and ``time_saved_sec`` (net).
    """
    # Safeguard: detect if running in test context and validate isolation
    import sys
    if "pytest" in sys.modules:
        config = get_config()
        prod_path = paths.state_path("usage.db")
        if str(config.llm_router_db_path) == str(prod_path):
            raise RuntimeError(
                "CRITICAL: log_claude_usage is writing to production database in test context!\n"
                "Tests must use the temp_db fixture to isolate the database.\n"
                f"Production path: {prod_path}\n"
                f"Config path: {config.llm_router_db_path}\n"
                "Fix: Add temp_db fixture to your test method parameters."
            )

    if tokens_used == 0:
        tokens_used = (input_tokens + output_tokens
                       + cache_creation_input_tokens + cache_read_input_tokens)

    cost_saved, time_saved = calc_savings(
        model, tokens_used,
        task_type=task_type, complexity=complexity,
        input_tokens=input_tokens, output_tokens=output_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
        routing_overhead_usd=routing_overhead_usd,
    )

    # cost_saved_usd kwarg (back-compat with router.py:999) is ignored —
    # the authoritative value comes from calc_savings above.
    _ = cost_saved_usd

    db = await _get_db()
    try:
        await db.execute(
            # T-05: provenance stamped at write time, by the same
            # `_detect_synthetic()` the `usage` ledger uses. A read-time
            # name heuristic cannot be made correct; this can.
            "INSERT INTO claude_usage ("
            "  model, tokens_used, complexity,"
            "  cost_saved_usd, time_saved_sec,"
            "  input_tokens, output_tokens,"
            "  cache_creation_input_tokens, cache_read_input_tokens,"
            "  routing_overhead_usd, is_simulated"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                model, tokens_used, complexity,
                cost_saved, time_saved,
                input_tokens, output_tokens,
                cache_creation_input_tokens, cache_read_input_tokens,
                routing_overhead_usd,
                1 if _detect_synthetic() else 0,
            ),
        )
        await db.commit()
    finally:
        await db.close()

    return {"cost_saved_usd": cost_saved, "time_saved_sec": time_saved}


async def log_codex_usage(
    model: str,
    tokens_used: int,
    complexity: str,
    *,
    task_type: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
    routing_overhead_usd: float = 0.0,
) -> dict:
    """Log an OpenAI/Codex model invocation and its savings vs. the realistic baseline.

    Parallel to log_claude_usage but writes to codex_usage and uses
    OPENAI_RATES_PER_M / _get_codex_baseline_for_task. v9.3.0.

    Returns:
        Dict with cost_saved_usd (net) and time_saved_sec (net).
    """
    import sys as _sys
    if "pytest" in _sys.modules:
        config = get_config()
        prod_path = paths.state_path("usage.db")
        if str(config.llm_router_db_path) == str(prod_path):
            raise RuntimeError(
                "CRITICAL: log_codex_usage is writing to production database in test context!\n"
                "Tests must use the temp_db fixture to isolate the database."
            )

    if tokens_used == 0:
        tokens_used = (input_tokens + output_tokens
                       + cache_creation_input_tokens + cache_read_input_tokens)

    baseline = _get_codex_baseline_for_task(task_type, complexity)

    # 4-component cost when sub-component tokens are provided.
    sub_total = input_tokens + output_tokens + cache_creation_input_tokens + cache_read_input_tokens
    if sub_total > 0:
        actual_cost = _codex_cost(
            model, input_tokens, output_tokens,
            cache_write_t=cache_creation_input_tokens,
            cache_read_t=cache_read_input_tokens,
        )
        baseline_cost = _codex_cost(
            baseline, input_tokens, output_tokens,
            cache_write_t=cache_creation_input_tokens,
            cache_read_t=cache_read_input_tokens,
        )
        effective_tokens = sub_total
    else:
        # Lumped fallback — average input+output rate as a coarse approximation.
        rates_actual = OPENAI_RATES_PER_M.get(model, {})
        rates_base = OPENAI_RATES_PER_M.get(baseline, OPENAI_RATES_PER_M["gpt-5.4"])
        actual_rate_avg = (rates_actual.get("input", 0) + rates_actual.get("output", 0)) / 2
        base_rate_avg = (rates_base["input"] + rates_base["output"]) / 2
        actual_cost = tokens_used * actual_rate_avg / 1_000_000
        baseline_cost = tokens_used * base_rate_avg / 1_000_000
        effective_tokens = tokens_used

    gross_cost_saved = baseline_cost - actual_cost
    cost_saved = gross_cost_saved - routing_overhead_usd

    # Time savings: OpenAI doesn't publish standard TPS; rough heuristic
    # treats gpt-5-mini as fastest, o3 as slowest. Numbers calibrate against
    # MODEL_SPEED_TPS shape (~100-200 range).
    tps_map = {
        "gpt-5-mini": 200.0, "gpt-4o-mini": 200.0,
        "gpt-5.4": 100.0, "gpt-5.5": 100.0, "gpt-4o": 110.0,
        "o3": 50.0, "o3-mini": 80.0,
    }
    if effective_tokens > 0:
        actual_time = effective_tokens / tps_map.get(model, 100.0)
        baseline_time = effective_tokens / tps_map.get(baseline, 100.0)
        time_saved = baseline_time - actual_time
    else:
        time_saved = 0.0

    db = await _get_db()
    try:
        await db.execute(
            # T-05: provenance stamped at write time (see log_claude_usage).
            "INSERT INTO codex_usage ("
            "  model, tokens_used, complexity,"
            "  cost_saved_usd, time_saved_sec,"
            "  input_tokens, output_tokens,"
            "  cache_creation_input_tokens, cache_read_input_tokens,"
            "  routing_overhead_usd, is_simulated"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                model, tokens_used, complexity,
                cost_saved, time_saved,
                input_tokens, output_tokens,
                cache_creation_input_tokens, cache_read_input_tokens,
                routing_overhead_usd,
                1 if _detect_synthetic() else 0,
            ),
        )
        await db.commit()
    finally:
        await db.close()

    return {"cost_saved_usd": cost_saved, "time_saved_sec": time_saved}


async def log_gemini_usage(
    model: str,
    tokens_used: int,
    complexity: str,
    *,
    task_type: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
    routing_overhead_usd: float = 0.0,
) -> dict:
    """Log a Gemini CLI model invocation and its savings vs. the realistic baseline.

    Parallel to log_claude_usage / log_codex_usage. Writes to gemini_usage
    table, uses GEMINI_RATES_PER_M / _get_gemini_baseline_for_task. v9.3.1.

    Returns:
        Dict with cost_saved_usd (net) and time_saved_sec (net).
    """
    import sys as _sys
    if "pytest" in _sys.modules:
        config = get_config()
        prod_path = paths.state_path("usage.db")
        if str(config.llm_router_db_path) == str(prod_path):
            raise RuntimeError(
                "CRITICAL: log_gemini_usage is writing to production database in test context!"
            )

    if tokens_used == 0:
        tokens_used = (input_tokens + output_tokens
                       + cache_creation_input_tokens + cache_read_input_tokens)

    baseline = _get_gemini_baseline_for_task(task_type, complexity)

    sub_total = input_tokens + output_tokens + cache_creation_input_tokens + cache_read_input_tokens
    if sub_total > 0:
        actual_cost = _gemini_cost(
            model, input_tokens, output_tokens,
            cache_write_t=cache_creation_input_tokens,
            cache_read_t=cache_read_input_tokens,
        )
        baseline_cost = _gemini_cost(
            baseline, input_tokens, output_tokens,
            cache_write_t=cache_creation_input_tokens,
            cache_read_t=cache_read_input_tokens,
        )
        effective_tokens = sub_total
    else:
        rates_actual = GEMINI_RATES_PER_M.get(model, {})
        rates_base = GEMINI_RATES_PER_M.get(baseline, GEMINI_RATES_PER_M["gemini-2.5-pro"])
        actual_rate_avg = (rates_actual.get("input", 0) + rates_actual.get("output", 0)) / 2
        base_rate_avg = (rates_base["input"] + rates_base["output"]) / 2
        actual_cost = tokens_used * actual_rate_avg / 1_000_000
        baseline_cost = tokens_used * base_rate_avg / 1_000_000
        effective_tokens = tokens_used

    gross_cost_saved = baseline_cost - actual_cost
    cost_saved = gross_cost_saved - routing_overhead_usd

    # Time savings: Gemini Flash ~250 tps, Pro ~80 tps based on rough latency reports
    tps_map = {
        "gemini-2.5-flash": 250.0, "gemini-2.0-flash": 280.0,
        "gemini-1.5-flash": 280.0,
        "gemini-2.5-pro": 80.0, "gemini-2.0-pro": 90.0, "gemini-1.5-pro": 90.0,
    }
    if effective_tokens > 0:
        actual_time = effective_tokens / tps_map.get(model, 150.0)
        baseline_time = effective_tokens / tps_map.get(baseline, 150.0)
        time_saved = baseline_time - actual_time
    else:
        time_saved = 0.0

    db = await _get_db()
    try:
        await db.execute(
            # T-05: provenance stamped at write time (see log_claude_usage).
            "INSERT INTO gemini_usage ("
            "  model, tokens_used, complexity,"
            "  cost_saved_usd, time_saved_sec,"
            "  input_tokens, output_tokens,"
            "  cache_creation_input_tokens, cache_read_input_tokens,"
            "  routing_overhead_usd, is_simulated"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                model, tokens_used, complexity,
                cost_saved, time_saved,
                input_tokens, output_tokens,
                cache_creation_input_tokens, cache_read_input_tokens,
                routing_overhead_usd,
                1 if _detect_synthetic() else 0,
            ),
        )
        await db.commit()
    finally:
        await db.close()

    return {"cost_saved_usd": cost_saved, "time_saved_sec": time_saved}


async def get_realized_savings(period: str = "today", *, platform: str = "all",
                               include_simulated: bool = False) -> dict:
    """Honest savings number: gross_saved - routing_overhead.

    Unlike `get_savings_summary`, this surfaces the case where routing
    cost more money than it saved (small prompts where classifier
    latency exceeds the model-cost delta).

    v9.3.0/v9.3.1 — Added `platform` kwarg: "claude", "codex", "gemini",
    or "all" (default). "all" sums across all three usage tables.

    Args:
        period: "today", "week", "month", or "all".
        platform: "claude", "codex", "gemini", or "all".

    Returns:
        Dict with keys: gross_saved_usd, routing_overhead_usd, realized_saved_usd,
        n_rows.
        When platform="all", also includes `by_platform` breakdown dict.
    """
    where_map = {
        "today": "WHERE date(timestamp, 'localtime') = date('now', 'localtime')",
        "week":  "WHERE timestamp >= datetime('now', '-7 days')",
        "month": "WHERE timestamp >= datetime('now', '-30 days')",
        "all":   "",
    }
    where = where_map.get(period, "")
    # T-05. `where` feeds every platform table (claude_usage / codex_usage /
    # gemini_usage), none of which HAD a provenance column before this audit.
    # "all" has no WHERE, so the filter supplies its own keyword.
    where = f"{where} {production_only(include_simulated, prefix='AND' if where else 'WHERE')}".strip()

    async def _query_table(table: str) -> tuple[float, float, int]:
        """(gross, overhead, n_rows).

        R6: the ROW COUNT travels with the money. CLAUDE.md — "a rate without
        its denominator is not a measurement" — and a savings total is the same
        shape: "$47.20 saved" over four calls and over four thousand are
        different claims and rendered identically. It was not returned here, so
        no surface downstream could show it even if it wanted to.
        """
        try:
            cursor = await db.execute(
                f"""SELECT
                    COALESCE(SUM(cost_saved_usd), 0),
                    COALESCE(SUM(routing_overhead_usd), 0),
                    COUNT(*)
                FROM {table} {where}"""
            )
            row = await cursor.fetchone()
            if not row:
                return 0.0, 0.0, 0
            return float(row[0]), float(row[1]), int(row[2])
        except Exception as exc:
            # Table may not exist on older DBs — treat as zero. But a persistent
            # failure here UNDERSTATES savings without any visible symptom.
            from llm_router import failopen
            failopen.record("CHZ-FO-COST-PLATFORM-TABLE", exc)
            return 0.0, 0.0, 0

    db = await _get_db()
    try:
        if platform == "claude":
            gross, overhead, n = await _query_table("claude_usage")
            return {
                "gross_saved_usd": gross,
                "routing_overhead_usd": overhead,
                "realized_saved_usd": gross - overhead,
                "n_rows": n,
            }
        if platform == "codex":
            gross, overhead, n = await _query_table("codex_usage")
            return {
                "gross_saved_usd": gross,
                "routing_overhead_usd": overhead,
                "realized_saved_usd": gross - overhead,
                "n_rows": n,
            }
        if platform == "gemini":
            gross, overhead, n = await _query_table("gemini_usage")
            return {
                "gross_saved_usd": gross,
                "routing_overhead_usd": overhead,
                "realized_saved_usd": gross - overhead,
                "n_rows": n,
            }
        # all
        claude_gross, claude_overhead, claude_n = await _query_table("claude_usage")
        codex_gross, codex_overhead, codex_n = await _query_table("codex_usage")
        gemini_gross, gemini_overhead, gemini_n = await _query_table("gemini_usage")
        gross = claude_gross + codex_gross + gemini_gross
        overhead = claude_overhead + codex_overhead + gemini_overhead
        return {
            "gross_saved_usd": gross,
            "routing_overhead_usd": overhead,
            "realized_saved_usd": gross - overhead,
            "n_rows": claude_n + codex_n + gemini_n,
            "by_platform": {
                "claude": {
                    "gross_saved_usd": claude_gross,
                    "routing_overhead_usd": claude_overhead,
                    "realized_saved_usd": claude_gross - claude_overhead,
                    "n_rows": claude_n,
                },
                "codex": {
                    "gross_saved_usd": codex_gross,
                    "routing_overhead_usd": codex_overhead,
                    "realized_saved_usd": codex_gross - codex_overhead,
                    "n_rows": codex_n,
                },
                "gemini": {
                    "gross_saved_usd": gemini_gross,
                    "routing_overhead_usd": gemini_overhead,
                    "realized_saved_usd": gemini_gross - gemini_overhead,
                    "n_rows": gemini_n,
                },
            },
        }
    finally:
        await db.close()


async def log_quota_snapshot(
    *,
    session_id: str,
    prompt_sequence: int,
    prompt_hash: str | None,
    claude_session_pct: float,
    claude_weekly_pct: float,
    claude_sonnet_pct: float,
    openai_spent_usd: float,
    gemini_spent_usd: float,
    ollama_available: bool,
    cache_age_seconds: float,
    was_cache_fresh: bool,
    routing_decision_id: int | None,
    final_model: str | None,
    final_provider: str | None,
    complexity_requested: str | None,
    complexity_used: str | None,
    was_downgraded: bool,
) -> int:
    """Log per-prompt quota state to quota_snapshots table for audit trail.

    Captures the quota pressure at the moment a prompt arrived, enabling
    retrospective analysis of quota patterns and correlation with routing
    decisions. Each row documents exactly what the router "saw" when making
    its decision.

    Args:
        session_id: Session UUID from session_id.txt
        prompt_sequence: Sequential number of this prompt within the session (0, 1, 2, ...)
        prompt_hash: Hash of the prompt text (for deduplication detection)
        claude_session_pct: Claude session % (5h window) at decision time
        claude_weekly_pct: Claude weekly % (7d window) at decision time
        claude_sonnet_pct: Claude Sonnet % (7d window) at decision time
        openai_spent_usd: OpenAI spend (last 24h) in USD
        gemini_spent_usd: Gemini spend (last 24h) in USD
        ollama_available: Whether Ollama is configured and reachable
        cache_age_seconds: Age of quota cache in seconds
        was_cache_fresh: Whether cache was within TTL
        routing_decision_id: FK to routing_decisions.id (None if Ollama-only)
        final_model: Model that executed the request
        final_provider: Provider of the model
        complexity_requested: Original complexity before pressure downgrade
        complexity_used: Final complexity after pressure downgrade
        was_downgraded: Whether pressure caused complexity downgrade

    Returns:
        ID of the inserted row for correlation tracking
    """
    db = await _get_db()
    try:
        await db.execute(
            """INSERT INTO quota_snapshots (
                session_id, prompt_sequence, prompt_hash,
                claude_session_pct, claude_weekly_pct, claude_sonnet_pct,
                openai_spent_usd, gemini_spent_usd, ollama_available,
                cache_age_seconds, was_cache_fresh,
                routing_decision_id, final_model, final_provider,
                complexity_requested, complexity_used, was_downgraded
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                prompt_sequence,
                prompt_hash,
                claude_session_pct,
                claude_weekly_pct,
                claude_sonnet_pct,
                openai_spent_usd,
                gemini_spent_usd,
                1 if ollama_available else 0,
                cache_age_seconds,
                1 if was_cache_fresh else 0,
                routing_decision_id,
                final_model,
                final_provider,
                complexity_requested,
                complexity_used,
                1 if was_downgraded else 0,
            ),
        )
        await db.commit()

        # Get the row ID that was just inserted
        cursor = await db.execute("SELECT last_insert_rowid()")
        row_id_result = await cursor.fetchone()
        row_id = row_id_result[0] if row_id_result else None
        return row_id or 0
    finally:
        await db.close()


async def get_daily_claude_tokens(*, include_simulated: bool = False) -> int:
    """Get the total number of Claude Code tokens consumed today (UTC).

    Returns:
        Token count as an integer. Returns 0 if no usage today.
    """
    db = await _get_db()
    try:
        cursor = await db.execute(
            f"SELECT COALESCE(SUM(tokens_used), 0) FROM claude_usage "  # T-05
            f"WHERE {production_only(include_simulated, prefix='')} AND "
            "date(timestamp, 'localtime') = date('now', 'localtime')"
        )
        row = await cursor.fetchone()
        return int(row[0]) if row else 0
    finally:
        await db.close()


async def get_daily_claude_breakdown(*, include_simulated: bool = False) -> dict[str, int]:
    """Get today's Claude Code token usage broken down by model.

    Returns:
        Dict mapping model name (e.g. "haiku") to total tokens used today.
        Empty dict if no usage exists.
    """
    db = await _get_db()
    try:
        cursor = await db.execute(
            f"SELECT model, SUM(tokens_used) FROM claude_usage "  # T-05
            f"WHERE {production_only(include_simulated, prefix='')} AND "
            "date(timestamp, 'localtime') = date('now', 'localtime') GROUP BY model"
        )
        rows = await cursor.fetchall()
        return {model: int(tokens) for model, tokens in rows}
    finally:
        await db.close()


async def get_savings_summary(period: str = "today", *,
                              include_simulated: bool = False) -> dict:
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
        "today": "WHERE date(timestamp, 'localtime') = date('now', 'localtime')",
        "week": "WHERE timestamp >= datetime('now', '-7 days')",
        "month": "WHERE timestamp >= datetime('now', '-30 days')",
        "all": "",
    }.get(period, "")
    # T-05. Found by `test_the_surface_list_is_complete`, not by the audit — the
    # audit's own table of unfiltered surfaces missed this one and
    # `get_cache_savings`. Which is the argument for the enumerating test over a
    # hand-written list.
    where = f"{where} {production_only(include_simulated, prefix='AND' if where else 'WHERE')}".strip()

    db = await _get_db()
    try:
        # Check if columns exist (backward compat for old DB schemas)
        try:
            cursor = await db.execute(
                f"SELECT COUNT(*), COALESCE(SUM(tokens_used), 0), "
                f"COALESCE(SUM(cost_saved_usd), 0), COALESCE(SUM(time_saved_sec), 0) "
                f"FROM claude_usage {where}"
            )
        except Exception as exc:
            # Counted as well as flagged: provenance already tells the CALLER, but
            # nothing told an operator how often this fires.
            from llm_router import failopen
            failopen.record("CHZ-FO-COST-SAVINGS-QUERY", exc)
            # RED2-02 (P1): a FAILED QUERY is not a zero-saving week.
            #
            # This branch returned exactly the dict the genuine-zero branch
            # below returns, so a broken telemetry path rendered as a confident
            # "$0.00 saved" on every downstream surface. Two entirely different
            # situations, one indistinguishable output — and it fails in the
            # direction that looks harmless, which is why it survived.
            #
            # `provenance` is what callers key on. The numeric fields stay (as
            # 0.0) so existing consumers do not KeyError, and they must not be
            # DISPLAYED when provenance is "unknown".
            return {
                "total_calls": 0, "total_tokens": 0, "cost_saved_usd": 0.0,
                "time_saved_sec": 0.0, "by_model": {},
                "provenance": "unknown",
                "detail": f"savings query failed: {exc}",
                "saved": Measured.unknown(f"savings query failed: {exc}"),
            }

        row = await cursor.fetchone()
        if not row or row[0] == 0:
            # A real, MEASURED zero: the table was readable and holds no rows
            # for this period. Distinct from the branch above, and now says so.
            return {
                "total_calls": 0, "total_tokens": 0, "cost_saved_usd": 0.0,
                "time_saved_sec": 0.0, "by_model": {},
                "provenance": "measured",
                "detail": "no routed calls in this period",
                "saved": Measured.measured(0.0),
            }

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
            "provenance": "measured",
            "detail": "",
            "saved": Measured.measured(float(cost_saved)),
        }
    finally:
        await db.close()


# ── Baseline pricing configuration ─────────────────────────────────────────────

# Configurable baseline pricing for savings calculations
# Users can override via LLM_ROUTER_SAVINGS_BASELINE env var (default: "sonnet")
# RED2-01: this table held $15/$75 for Opus — the retired Opus 3 rate, 3x the
# current one — and fed the ledger write path, so stored savings were overstated
# by that factor. Haiku's $0.80/$4.00 was wrong too (the real rate is $1.00/$5.00).
# Now derived from llm_router.pricing so a family alias can never carry its own
# number again; the alias resolves to a model ID and the ID carries the price.
BASELINE_PRICING = {
    family: {
        "input": _pricing.input_rate(family),
        "output": _pricing.output_rate(family),
    }
    for family in ("haiku", "sonnet", "opus")
}

# WP-05 removed `_get_baseline_model()` (env-or-sonnet) and
# `_get_baseline_for_task()` (research/complex -> opus, query -> haiku, else
# sonnet). Both were savings-baseline policies of their own, and the tiered one
# contradicted the flat baseline that savings_logger, the dashboard and the
# session-end hook already used — 5x apart on a QUERY call.
#
# They are deleted rather than re-pointed. A second baseline function left
# importable is a second policy waiting for a caller, and this codebase has
# already demonstrated that dead safety/duplicate code gets wired back up
# (RED3-01, RED3-10). The one policy is pricing.savings_baseline_model().


def _get_baseline_cost(in_tokens: int, out_tokens: int, baseline_model: str = None) -> float:
    """Cost of ``in_tokens``/``out_tokens`` at the savings baseline.

    ``baseline_model`` is retained for callers that price against a specific
    model, but it no longer defaults to a second policy: with no argument this
    uses the one baseline. An unpriced model falls back to the baseline rates
    instead of 0.0, so a bad model name can never render as "saved nothing".
    """
    if baseline_model is None:
        in_rate, out_rate = _pricing.savings_baseline_rates()
    else:
        in_rate = _pricing.input_rate(baseline_model)
        out_rate = _pricing.output_rate(baseline_model)
        if in_rate is None or out_rate is None:
            in_rate, out_rate = _pricing.savings_baseline_rates()
    return (in_tokens * in_rate + out_tokens * out_rate) / 1_000_000


def _coverage_counts() -> dict:
    """observed_n / unobserved_n for a rate metric. Never raises.

    WP-07: a rate is a fraction of traffic LLM Router SAW. Shipping it without its
    denominator lets it silently redefine itself when routing degrades.
    """
    try:
        from llm_router import coverage as _coverage

        snap = _coverage.snapshot()
        return {"observed_n": snap.observed_n, "unobserved_n": snap.unobserved_n}
    except Exception as exc:  # noqa: BLE001
        # Zero denominators make every rate render Unknown downstream (correct),
        # but nothing said the telemetry itself was broken.
        from llm_router import failopen
        failopen.record("CHZ-FO-COST-COVERAGE-COUNTS", exc)
        return {"observed_n": 0, "unobserved_n": 0}


async def get_router_efficiency(period: str = "today", *,
                                include_synthetic: bool = False) -> dict:
    """Get router efficiency score: what % of routing decisions matched recommendations.
    
    Analyzes routing_decisions table to compute on-target selection rate.
    
    Args:
        period: Time window. One of "today", "week", "month", or "all".
    
    Returns:
        Dict with keys: total, on_target, efficiency_pct (0-100).
        Returns zeroed values if no routing decisions exist for the period.
    """
    where_map = {
        "today": "WHERE date(timestamp, 'localtime') = date('now', 'localtime')",
        "week": "WHERE timestamp >= datetime('now', '-7 days')",
        "month": "WHERE timestamp >= datetime('now', '-30 days')",
        "all": "",
    }
    where = where_map.get(period, "")
    # T-05. `all` has no WHERE at all, so the provenance filter needs its own
    # keyword — appending "AND ..." to an empty string is a syntax error, and a
    # broken query here fails into a caller that renders zeros as data.
    _prov = routing_production_only(include_synthetic, prefix="AND" if where else "WHERE")
    where = f"{where} {_prov}".strip()

    db = await _get_db()
    try:
        # Count total decisions and on-target decisions
        cursor = await db.execute(
            f"""SELECT COUNT(*), 
                COUNT(CASE WHEN final_model = recommended_model THEN 1 END)
            FROM routing_decisions {where}"""
        )
        row = await cursor.fetchone()
        _cov = _coverage_counts()
        if not row or row[0] == 0:
            # WP-07 / RED2-02: no routing decisions is NOT a 0%-effective
            # router. The previous 0.0 was indistinguishable from a router that
            # got every decision wrong, and it failed in the direction that
            # looks like a real measurement. `efficiency_pct` is None and
            # provenance says why; callers must render "Unknown", not a number.
            return {
                "total": 0,
                "on_target": 0,
                "efficiency_pct": None,
                "provenance": "unknown",
                "detail": "no routing decisions recorded for this period",
                **_cov,
            }

        total, on_target = row
        efficiency_pct = round(on_target / total * 100) if total > 0 else 0
        return {
            "total": int(total),
            "on_target": int(on_target),
            "efficiency_pct": float(efficiency_pct),
            # The rate is over OBSERVED decisions. unobserved_n says how much
            # traffic never reached the decision table at all, so a consumer can
            # tell "90% on-target over everything" from "90% over the 3% we saw".
            "provenance": "measured",
            "detail": "",
            **_cov,
        }
    finally:
        await db.close()


async def get_classifier_overhead(period: str = "today") -> dict:
    """Get routing classifier latency metrics.
    
    Analyzes classifier_latency_ms from routing_decisions to understand
    the time cost of the routing classification step.
    
    Args:
        period: Time window. One of "today", "week", "month", or "all".
    
    Returns:
        Dict with keys: avg_ms (float), min_ms (float), max_ms (float), count (int).
        Returns zeroed values if no routing decisions exist.
    """
    where_map = {
        "today": "WHERE date(timestamp, 'localtime') = date('now', 'localtime')",
        "week": "WHERE timestamp >= datetime('now', '-7 days')",
        "month": "WHERE timestamp >= datetime('now', '-30 days')",
        "all": "",
    }
    where = where_map.get(period, "")
    
    db = await _get_db()
    try:
        cursor = await db.execute(
            f"""SELECT COUNT(*), AVG(classifier_latency_ms), 
                MIN(classifier_latency_ms), MAX(classifier_latency_ms)
            FROM routing_decisions {where}"""
        )
        row = await cursor.fetchone()
        if not row or row[0] == 0:
            return {"avg_ms": 0.0, "min_ms": 0.0, "max_ms": 0.0, "count": 0}
        
        count, avg_ms, min_ms, max_ms = row
        return {
            "count": int(count),
            "avg_ms": float(avg_ms or 0.0),
            "min_ms": float(min_ms or 0.0),
            "max_ms": float(max_ms or 0.0),
        }
    finally:
        await db.close()


async def get_cache_hit_stats(period: str = "today") -> dict:
    """Get prompt caching statistics.
    
    Analyzes semantic_cache table to compute cache hit ratio and savings.
    
    Args:
        period: Time window. One of "today", "week", "month", or "all".
    
    Returns:
        Dict with keys: total_requests, cache_hits, hit_rate_pct (0-100), 
        estimated_saved_usd. Returns zeroed values if no cache data exists.
    """
    where_map = {
        "today": "WHERE date(accessed_at, 'localtime') = date('now', 'localtime')",
        "week": "WHERE accessed_at >= datetime('now', '-7 days')",
        "month": "WHERE accessed_at >= datetime('now', '-30 days')",
        "all": "",
    }
    where = where_map.get(period, "")
    
    db = await _get_db()
    try:
        # Check if semantic_cache table exists
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='semantic_cache'"
        )
        if not await cursor.fetchone():
            return {"total_requests": 0, "cache_hits": 0, "hit_rate_pct": 0.0, "estimated_saved_usd": 0.0}
        
        # Query cache stats
        cursor = await db.execute(
            f"""SELECT COUNT(*), COUNT(CASE WHEN was_hit = 1 THEN 1 END)
            FROM semantic_cache {where}"""
        )
        row = await cursor.fetchone()
        if not row or row[0] == 0:
            return {"total_requests": 0, "cache_hits": 0, "hit_rate_pct": 0.0, "estimated_saved_usd": 0.0}
        
        total_requests, cache_hits = row
        hit_rate = round(cache_hits / total_requests * 100) if total_requests > 0 else 0
        
        # Estimate savings from cache hits (assume avg call would cost ~$0.0001)
        estimated_saved = cache_hits * 0.0001  # Conservative estimate
        
        return {
            "total_requests": int(total_requests),
            "cache_hits": int(cache_hits),
            "hit_rate_pct": float(hit_rate),
            "estimated_saved_usd": round(estimated_saved, 4),
        }
    except Exception as exc:
        from llm_router import failopen
        failopen.record("CHZ-FO-COST-CACHE-STATS", exc)
        return {"total_requests": 0, "cache_hits": 0, "hit_rate_pct": 0.0, "estimated_saved_usd": 0.0}
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
            # T-05: provenance stamped at write time (see log_claude_usage).
            "INSERT INTO savings_stats "
            "(timestamp, session_id, task_type, estimated_claude_cost_saved, external_cost, "
            "model_used, is_simulated) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(),
                session_id,
                task_type,
                estimated_saved,
                external_cost,
                model,
                1 if _detect_synthetic() else 0,
            ),
        )
        await db.commit()
    finally:
        await db.close()


async def get_lifetime_savings_summary(days: int = 30, *,
                                       include_simulated: bool = False) -> dict:
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
    # T-05: savings_stats had no provenance column at all until this audit.
    where = f"{where} {production_only(include_simulated, prefix='AND' if where else 'WHERE')}".strip()
    from llm_router.savings import (
        UNVERIFIED_CALLS_SQL, UNVERIFIED_SAVED_SQL, VERIFIED_SAVED_SQL,
    )
    empty: dict = {
        "total_saved": 0.0,
        "total_external_cost": 0.0,
        "net_savings": 0.0,
        "tasks_routed": 0,
        "unverified_saved": 0.0,
        "unverified_tasks": 0,
        "by_session": [],
    }

    db = await _get_db()
    try:
        cursor = await db.execute(
            f"SELECT COUNT(*), COALESCE(SUM({VERIFIED_SAVED_SQL}), 0), "
            f"COALESCE(SUM(external_cost), 0), "
            f"COALESCE(SUM({UNVERIFIED_SAVED_SQL}), 0), "
            f"COALESCE(SUM({UNVERIFIED_CALLS_SQL}), 0) FROM savings_stats {where}"
        )
        row = await cursor.fetchone()
        if not row or row[0] == 0:
            return empty

        tasks_routed, total_saved, total_external, unverified, unverified_n = row

        cursor = await db.execute(
            f"SELECT session_id, COUNT(*), "
            f"COALESCE(SUM({VERIFIED_SAVED_SQL}), 0), "
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
            "unverified_saved": float(unverified),
            "unverified_tasks": int(unverified_n),
            "by_session": by_session,
        }
    finally:
        await db.close()


def _safe_unlink(p: Path) -> None:
    try:
        p.unlink()
    except OSError:
        pass


def _restore_claim(claim: Path, live: Path) -> None:
    """Append a failed claim's lines back to the live log, then drop the claim.

    Uses append (never replace) so newly-arrived lines in ``live`` are preserved.
    """
    try:
        data = claim.read_text()
    except OSError:
        return
    try:
        with open(live, "a") as f:
            f.write(data)
    except OSError:
        pass
    _safe_unlink(claim)


async def import_savings_log() -> int:
    """Import savings records from the JSONL file into SQLite, then drop the file.

    The PostToolUse hook appends one JSON line per routed call to
    ``~/.llm-router/savings_log.jsonl``.  This function reads all lines and inserts
    them into the ``savings_stats`` table.

    AC-5 (dual-writer race): the log is drained by BOTH this async importer and
    ``session-end.py::_sync_import_savings_log``. Reading-then-truncating unlocked
    let two concurrent drainers read the same rows and double-insert. We instead
    **atomically claim** the log via ``os.replace`` (only one caller wins the
    rename; the rest get ``FileNotFoundError`` and no-op), process the claimed
    copy, and delete it — or append it back on insert failure so nothing is lost.

    Returns:
        Number of records imported (0 if another drainer claimed the log first).
    """
    import asyncio
    import os
    import uuid

    claim = savings_log_path().with_name(
        f"{savings_log_path().name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.claim"
    )
    # Atomic claim — serializes concurrent drainers at the filesystem layer.
    try:
        await asyncio.to_thread(os.replace, str(savings_log_path()), str(claim))
    except OSError:
        return 0  # no live log, or another drainer claimed it first

    try:
        raw = await asyncio.to_thread(claim.read_text)
    except OSError:
        return 0

    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if not lines:
        await asyncio.to_thread(_safe_unlink, claim)
        return 0

    from datetime import datetime, timezone

    db = await _get_db()
    imported = 0
    committed = False
    try:
        for line in lines:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            await db.execute(
                # T-05. An imported entry carries its OWN provenance when the
                # writer recorded one; `_detect_synthetic()` here would describe
                # the importing process, not the call. Absent, it stays NULL =
                # unknown and drops out of money figures — never a default 0,
                # which is the lie C-02 was raised over.
                "INSERT INTO savings_stats "
                "(timestamp, session_id, task_type, estimated_claude_cost_saved, "
                "external_cost, model_used, host, input_tokens, output_tokens, mode, "
                "is_simulated) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entry.get("timestamp", datetime.now(timezone.utc).isoformat()),
                    entry.get("session_id", "unknown"),
                    entry.get("task_type", "unknown"),
                    float(entry.get("estimated_saved", 0.0)),
                    float(entry.get("external_cost", 0.0)),
                    entry.get("model", "unknown"),
                    entry.get("host", "claude_code"),
                    int(entry.get("input_tokens", 0) or 0),
                    int(entry.get("output_tokens", 0) or 0),
                    # None for a record written before the field existed: absent
                    # is not the same as echo, and must not read as one.
                    entry.get("mode"),
                    (None if entry.get("is_simulated") is None
                     else (1 if entry.get("is_simulated") else 0)),
                ),
            )
            imported += 1
        await db.commit()
        committed = True
    finally:
        await db.close()

    if committed:
        await asyncio.to_thread(_safe_unlink, claim)
    else:
        # Insert failed — return the rows to the live log for a later retry.
        await asyncio.to_thread(_restore_claim, claim, savings_log_path())

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
    except Exception as exc:
        from llm_router import failopen
        failopen.record("CHZ-FO-COST-LATENCY-STATS", exc)
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


# ── Cost baseline models and pricing (used for savings calculations) ────────
# The savings baseline is the HOST model: what the user's Claude Code
# subscription would have charged for the same tokens if the work had NOT been
# routed. That host model is the latest Opus. Keep the model id and its price in
# ONE place (LATEST_OPUS_MODEL + _OPUS_PRICING) so a new Opus release or price
# change updates a single source of truth instead of drifting a hardcoded
# literal.
#
# History: the previous constants were $15/$75 labelled "Opus 4.6" — wrong on
# two axes. (1) The version was frozen and silently stale as newer Opus models
# shipped. (2) The *price* was ~3x too high: $15/$75 was the retired
# Opus-4.1-and-earlier tier; Opus 4.5 onward (incl. 4.6/4.7/4.8) is $5/$25 per
# million tokens. Every historical `saved_usd` was therefore ~3x inflated.

LATEST_OPUS_MODEL = "claude-opus-5"
"""The current host Opus model. Bump when a newer Opus ships.

This lagged at claude-opus-4-8 while llm_router.pricing already priced claude-opus-5
— the "bump when a newer Opus ships" instruction was not followed, which is the
same failure mode WP-03 removed by deriving prices rather than restating them.
It is no longer a savings baseline (see pricing.SAVINGS_BASELINE_MODEL); it only
answers "which Opus is current"."""

# Opus per-million-token pricing (input, output) in USD, projected out of
# llm_router.pricing rather than restated. Extend the tuple as new Opus models
# release; the values can also be refreshed at runtime via
# refresh_baseline_pricing_from_api().
_OPUS_MODELS: tuple[str, ...] = (
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-opus-4-5",
)
_OPUS_PRICING: dict[str, tuple[float, float]] = {
    _m: (_p.input, _p.output)
    for _m in _OPUS_MODELS
    if (_p := _pricing.price_for(_m)) is not None
}

BASELINE_MODEL_FOR_SAVINGS = _pricing.savings_baseline_model()
"""Reference model for savings, projected from the ONE policy in llm_router.pricing.

WP-05: this used to be its own binding (``LATEST_OPUS_MODEL``), one of three
competing baselines. It is now a view onto ``pricing.savings_baseline_model()``
so it cannot drift from the dashboard, the session-end hook, or the ledger
writer again. ``LATEST_OPUS_MODEL`` remains, but only as "which Opus is current"
— it is no longer a savings policy."""

_HOST_INPUT_PER_M, _HOST_OUTPUT_PER_M = _pricing.savings_baseline_rates()
"""Baseline per-million-token rates ($/M), from the one savings policy."""

_FREE_PROVIDERS = {"ollama", "codex", "gemini_cli"}
"""Providers that incur zero cost (local or included in subscription)."""


def _host_is_metered() -> bool:
    """True when the host (baseline) model is billed per-token — i.e. the user is
    on the metered API rather than a flat-rate Claude Code subscription.

    On a subscription the marginal cost of a host Opus call is ~$0 until the quota
    cap is hit, so the *real dollars* avoided by routing is ~$0 even though the
    Opus-baseline "avoided" figure is large. This env-driven flag lets the
    savings surfaces report an honest cash number beside the baseline figure.
    Defaults to False (subscription) — the common case and the conservative one
    for a dollar claim (never claim cash we can't prove). Metered is returned
    ONLY when the subscription flag is explicitly turned off. See RETROSPECTIVE
    B-7 / M-2.
    """
    val = os.environ.get("LLM_ROUTER_CLAUDE_SUBSCRIPTION", "").strip().lower()
    if val in ("0", "false", "no", "off"):
        return True   # explicitly metered API mode
    return False      # subscription (explicit true/on, or absent/unknown)


def refresh_baseline_pricing_from_api() -> bool:
    """Best-effort refresh of the latest-Opus baseline price from the Models API.

    Optional and never called at import — the hardcoded ``_OPUS_PRICING`` map is
    the offline source of truth. When credentials and network are available this
    updates ``_HOST_INPUT_PER_M`` / ``_HOST_OUTPUT_PER_M`` for
    ``LATEST_OPUS_MODEL`` so a mid-cycle price change is picked up without a code
    edit. Returns True on success, False (leaving the hardcoded values intact) on
    any failure.
    """
    global _HOST_INPUT_PER_M, _HOST_OUTPUT_PER_M
    try:
        import anthropic

        # Models API metadata/pricing lookup, not a routed LLM completion.
        model = anthropic.Anthropic().models.retrieve(LATEST_OPUS_MODEL)  # llm_router: direct-ok
        pricing = getattr(model, "pricing", None) or {}
        in_pm = pricing.get("input_per_mtok")
        out_pm = pricing.get("output_per_mtok")
        if in_pm and out_pm:
            _HOST_INPUT_PER_M, _HOST_OUTPUT_PER_M = float(in_pm), float(out_pm)
            _OPUS_PRICING[LATEST_OPUS_MODEL] = (_HOST_INPUT_PER_M, _HOST_OUTPUT_PER_M)
            return True
    except Exception as exc:
        # Live pricing refresh failed, so the BASELINE stays at whatever the
        # static table holds. Stale prices produce plausible wrong money — the
        # exact RED2-01 shape, which shipped a 3x overstatement.
        from llm_router import failopen
        failopen.record("CHZ-FO-COST-PRICING-REFRESH", exc)
    return False


async def get_savings_by_period(*, include_simulated: bool = False) -> dict[str, dict]:
    """Return time-bucketed savings aggregates for the savings dashboard.

    Queries the usage table for four periods: today, this week (Mon–Sun),
    this calendar month, and all-time. For each period returns:
        saved_usd:    total dollars saved vs Opus baseline
        actual_usd:   total dollars actually spent on paid API calls
        baseline_usd: what Opus would have cost for the same tokens
        calls:        total routed calls in the period
        efficiency:   baseline_usd / actual_usd multiplier (0 if no paid calls)

    Rows with saved_usd populated (v2.1+) use that directly. Pre-v2.1 rows
    fall back to estimating the Opus baseline from token counts.
    """
    db = await _get_db()
    try:
        # Build period boundaries as SQLite datetime expressions. timestamp is
        # stored UTC; compare in the user's LOCAL timezone (both column and
        # boundary get 'localtime') so "today"/"week"/"month" line up with the
        # user's wall clock instead of UTC — otherwise savings for the last N
        # hours before local midnight leak into the wrong period.
        periods = {
            "today": "date('now','localtime')",
            "week": "date('now','localtime', 'weekday 0', '-6 days')",
            "month": "date('now','localtime', 'start of month')",
            "all_time": "'1970-01-01'",
        }
        # C-02. `include_simulated` mirrors `summarize(include_unevaluable=...)`:
        # a named escape hatch for tests that exercise the aggregate arithmetic
        # over rows they wrote themselves (every such row is stamped synthetic,
        # because pytest sets PYTEST_CURRENT_TEST). No production caller passes
        # it, and the default stays exclusive.
        # T13, fail-closed. `IS NOT 1` admits NULL, so an UNKNOWN row counted as
        # production -- the same defect as `is_evaluable` treating a missing field
        # as real. Only rows explicitly stamped 0 at write time are production.
        _sim_clause = "" if include_simulated else "AND is_simulated = 0"

        result: dict[str, dict] = {}
        for name, since_expr in periods.items():
            rows = await db.execute_fetchall(
                f"""SELECT provider, input_tokens, output_tokens, cost_usd, saved_usd
                    FROM usage
                    WHERE date(timestamp,'localtime') >= {since_expr}
                      AND success = 1
                      {_sim_clause}""",
            )
            actual = baseline = saved_total = 0.0
            calls = 0
            subscription_calls = 0   # T-26: counted, not silently merged
            for provider, in_tok, out_tok, cost, saved_col in rows:
                in_tok = in_tok or 0
                out_tok = out_tok or 0
                cost = cost or 0.0
                # T-26 (audit 2026-09-22). `calls` was incremented BEFORE the
                # subscription skip, so `calls` counted rows the dollar figures
                # then ignored: a period's "N calls, $X saved" quoted two
                # different populations, and the efficiency multiplier divided
                # one by the other. Both now describe the same rows, and the
                # skipped ones are reported separately rather than folded in.
                if provider == "subscription":
                    subscription_calls += 1
                    continue  # CC subscription rows have no token cost data
                calls += 1
                # Always recalculate from actual in/out counts at Opus rates.
                # Stored saved_col used a blended $0.045/1K estimate; accurate
                # pricing requires separate input/output rates ($5/M and $25/M
                # for the latest Opus — see _OPUS_PRICING).
                host_est = (in_tok * _HOST_INPUT_PER_M + out_tok * _HOST_OUTPUT_PER_M) / 1_000_000
                baseline += host_est
                if provider in _FREE_PROVIDERS:
                    saved_total += host_est
                else:
                    actual += cost
                    saved_total += net_saved(host_est, cost)

            efficiency = baseline / actual if actual > 0.001 else 0.0
            # RETROSPECTIVE B-7: report two figures, never conflated.
            #  - baseline_avoided_usd: Opus-baseline vs actual (== legacy saved_usd).
            #  - real_dollars_avoided_usd: dollars the user would ACTUALLY have paid.
            #    ~$0 on a flat-rate subscription (host call is marginal-$0); equals
            #    the baseline figure only in metered API mode.
            real_avoided = saved_total if _host_is_metered() else 0.0
            result[name] = {
                "saved_usd": round(saved_total, 4),  # back-compat alias
                "baseline_avoided_usd": round(saved_total, 4),
                "real_dollars_avoided_usd": round(real_avoided, 4),
                "actual_usd": round(actual, 4),
                "baseline_usd": round(baseline, 4),
                "calls": calls,
                # T-26: rows the dollar figures deliberately exclude,
                # reported rather than merged into `calls`. "12 calls, $0
                # saved" with no explanation is how a working install reads
                # as idle.
                "subscription_calls": subscription_calls,
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
    except Exception as exc:
        # An empty quality map reads as "no quality signal yet", which is what a
        # fresh install looks like. A broken query is indistinguishable from it.
        from llm_router import failopen
        failopen.record("CHZ-FO-COST-QUALITY-AGG", exc)
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
    except Exception as exc:
        from llm_router import failopen
        failopen.record("CHZ-FO-COST-RATING-AGG", exc)
        return {}
    finally:
        await db.close()


async def get_team_savings(
    user_id: str = "",
    project_id: str = "",
    period: str = "week",
    *,
    include_simulated: bool = False,
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
    # Local-timezone period boundaries (timestamp is stored UTC) so "today"/etc.
    # track the user's wall clock, not UTC — see get_savings_by_period above.
    period_map = {
        "today": "date('now','localtime')",
        "week": "date('now','localtime', 'weekday 0', '-6 days')",
        "month": "date('now','localtime', 'start of month')",
        "all": "'1970-01-01'",
    }
    since = period_map.get(period, period_map["week"])

    # T-05. This is the surface team.py broadcasts to Slack/Discord, and it was
    # the one with no provenance filter. Reproduced before the fix: a single
    # synthetic row produced a $3.00 team-savings broadcast.
    where_parts = [f"date(timestamp,'localtime') >= {since}",
                   production_only(include_simulated, prefix="").strip() or "1=1"]
    params: list = []
    if user_id:
        where_parts.append("user_id = ?")
        params.append(user_id)
    if project_id:
        where_parts.append("project_id = ?")
        params.append(project_id)
    where = " AND ".join(where_parts)

    _free = {"ollama", "codex", "gemini_cli", "subscription"}

    # #24: _get_db() was OUTSIDE this try, so the MOST LIKELY failure -- the
    # ledger missing, locked, or permission-denied at OPEN time -- propagated
    # instead of returning a provenance-marked result. The except path below
    # then only covered query failures, which is the rarer case. An honest
    # "unknown" beats an exception a caller may swallow into a silent broadcast.
    db = None
    try:
        db = await _get_db()
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
    except Exception as exc:
        # Zeroes here render as "you routed nothing and saved nothing" — a
        # working install that looks idle.
        from llm_router import failopen
        failopen.record("CHZ-FO-COST-ROUTING-SUMMARY", exc)
        # RED2-02 / #24: the zeros are unavoidable (callers index these keys),
        # but they must not READ as data. provenance="unknown" is what lets
        # team.py print "unknown" instead of "$0.0000" to a Slack channel.
        # failopen.record above counts this internally; a counter the caller
        # cannot see does not stop a false broadcast.
        return {"total_calls": 0, "saved_usd": 0.0, "actual_usd": 0.0, "free_pct": 0.0,
                "top_models": [], "provenance": "unknown",
                "provenance_detail": "usage ledger unreadable"}
    finally:
        if db is not None:
            await db.close()

    total_calls = sum(r[2] for r in rows)
    actual_usd = sum(r[3] for r in rows)
    free_calls = sum(r[2] for r in rows if r[1] in _free)
    free_pct = free_calls / total_calls if total_calls else 0.0

    # Estimate savings vs Opus baseline using token counts
    total_tokens = sum(r[4] for r in rows)
    host_baseline = total_tokens / 1000 * ((_HOST_INPUT_PER_M + _HOST_OUTPUT_PER_M) / 2 / 1000)
    saved_usd = net_saved(host_baseline, actual_usd)
    # INV-COST-006 / AC-2: split baseline-equivalent avoided (counterfactual) from
    # real metered dollars avoided. On a flat-rate subscription host the marginal host
    # cost is ~$0, so real dollars avoided is 0 unless the host is genuinely metered —
    # exactly as get_savings_by_period does. Prior to this, get_team_savings emitted
    # only baseline-avoided as `saved_usd`, which team.py broadcast to Slack/Discord as
    # unqualified cash (audit P0-2).
    real_avoided = saved_usd if _host_is_metered() else 0.0

    top_models = [
        {"model": r[0], "provider": r[1], "calls": r[2], "cost": r[3]}
        for r in rows[:10]
    ]

    return {
        "total_calls": total_calls,
        "saved_usd": saved_usd,                              # back-compat alias (baseline-equivalent)
        "baseline_equivalent_avoided_usd": round(saved_usd, 4),
        "real_dollars_avoided_usd": round(real_avoided, 4),
        "actual_usd": actual_usd,
        "free_pct": free_pct,
        "top_models": top_models,
        # The tag must DISTINGUISH: if it only appeared on the error path it
        # would carry no information, since a caller cannot tell "absent
        # because measured" from "absent because nobody set it".
        "provenance": "measured",
    }


# Re-use module-level baseline constants (defined once near line 1860)
# _HOST_INPUT_PER_M and _HOST_OUTPUT_PER_M are already defined above


async def get_routing_savings_vs_sonnet(days: int = 0, *,
                                        include_synthetic: bool = False) -> dict:
    """Compute savings by comparing actual cost vs the latest-Opus host baseline.

    Uses the routing_decisions table (populated by the router on every call).
    Savings = what the host Opus model would have cost − what we actually paid.

    NOTE: the ``_vs_sonnet`` name is historical and misleading — the baseline is
    the latest Opus (``LATEST_OPUS_MODEL``), never Sonnet. Rename is deferred to
    avoid breaking callers; see RETROSPECTIVE B-8.

    Args:
        days: Look-back window. 0 = all time.

    Returns:
        Dict with ``total_calls``, ``actual_cost``, ``baseline_cost``,
        ``saved``, ``input_tokens``, ``output_tokens``, and ``by_model``.
    """
    # T-05: routing_decisions side.
    where = (
        f"WHERE timestamp >= datetime('now', '-{days} days') AND success = 1"
        if days > 0
        else "WHERE success = 1"
    ) + f" {routing_production_only(include_synthetic)}"
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
        baseline = (in_tok * _HOST_INPUT_PER_M + out_tok * _HOST_OUTPUT_PER_M) / 1_000_000
        saved = net_saved(baseline, actual_cost)

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
            m_baseline = (m_in * _HOST_INPUT_PER_M + m_out * _HOST_OUTPUT_PER_M) / 1_000_000
            by_model[m or "unknown"] = {
                "calls": int(cnt),
                "actual_cost": float(m_cost),
                "baseline_cost": float(m_baseline),
                "saved": net_saved(m_baseline, float(m_cost)),
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
    except Exception as exc:
        from llm_router import failopen
        failopen.record("CHZ-FO-COST-SAVINGS-BREAKDOWN", exc)
        return empty
    finally:
        await db.close()


async def get_cache_savings(period: str = "today", *,
                            include_simulated: bool = False) -> dict[str, float]:
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
            time_filter = "date(timestamp,'localtime') = date('now','localtime')"
        elif period == "week":
            time_filter = "timestamp >= datetime('now', '-7 days')"
        elif period == "month":
            # RED1-07: local-frame month boundary, consistent with the "today"
            # filter above (was a UTC 'start of month').
            time_filter = (
                "strftime('%Y-%m', timestamp, 'localtime') = "
                "strftime('%Y-%m', 'now', 'localtime')"
            )
        else:  # all
            time_filter = "1"

        # Get cache hit stats
        cursor = await db.execute(
            f"""SELECT COUNT(*), COALESCE(SUM(cache_savings_usd), 0)
                FROM usage WHERE {time_filter} AND cache_hit = 1
                  {production_only(include_simulated)}"""
        )
        cached_row = await cursor.fetchone()
        cached_calls, cached_savings = cached_row if cached_row else (0, 0.0)

        # Get total calls for hit rate
        # T-05: the cache-hit RATE's denominator. Filtering the numerator and not
        # this would invent a rate above 100%.
        cursor = await db.execute(
            f"SELECT COUNT(*) FROM usage WHERE {time_filter} {production_only(include_simulated)}")
        total_row = await cursor.fetchone()
        total_calls = total_row[0] if total_row else 0

        cache_hit_rate = (cached_calls / total_calls * 100) if total_calls > 0 else 0.0

        return {
            "total_calls_cached": int(cached_calls),
            "total_savings_usd": float(cached_savings),
            "cache_hit_rate": float(cache_hit_rate),
        }
    except Exception as exc:
        from llm_router import failopen
        failopen.record("CHZ-FO-COST-CACHE-SAVINGS", exc)
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


async def log_quality_trend(
    model: str,
    task_type: str | None,
    avg_score: float,
    sample_count: int,
    trend_direction: str = "stable",
) -> None:
    """Log a quality trend snapshot for a model over a time window.

    Called at session-end or periodically to track rolling quality scores.
    Allows Quality Guard to make decisions based on recent quality degradation.

    Args:
        model: Model identifier (e.g., 'openai/gpt-4o')
        task_type: Optional task type filter (e.g., 'code', 'research')
        avg_score: Average judge score (0–1) over the window
        sample_count: Number of evaluated responses in the window
        trend_direction: 'improving', 'stable', or 'degrading'
    """
    from datetime import datetime, timedelta

    db = await _get_db()
    try:
        now = datetime.now().isoformat()
        window_start = (datetime.now() - timedelta(days=7)).isoformat()
        await db.execute(
            """INSERT INTO model_quality_trends
               (model, task_type, window_start, window_end, avg_score, sample_count, trend_direction)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                model,
                task_type,
                window_start,
                now,
                avg_score,
                sample_count,
                trend_direction,
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
    except Exception as exc:
        from llm_router import failopen
        failopen.record("CHZ-FO-COST-TOKEN-SAVER-STATS", exc)
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
