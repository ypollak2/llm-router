"""PostToolUse[Agent] hook — track Claude Code subscription model calls.

Fires after every Agent subagent completes. Writes an estimated usage record
to the canonical llm-router SQLite database (~/.llm-router/usage.db) so the
dashboard shows CC subscription Agent calls alongside external API calls and
credits them against the right counterfactual baseline.

v9.4.0: switched from the stub ``llm_usage.db`` to the canonical ``usage.db``
and populates baseline_model + potential_cost_usd + saved_usd. The old DB is
now orphaned and may be removed manually.

Token estimation: input from prompt length, output from result length (chars/4).
Cost: 0.0 — CC subscription is flat-rate, not per-call billed.
Model mapping from subagent_type:
  Explore, general-purpose  → claude-haiku-4-5-20251001  (lightweight)
  Plan, Planner, architect   → claude-sonnet-4-6          (moderate)
  code-*, feature-dev*       → claude-sonnet-4-6          (moderate)
  everything else            → claude-sonnet-4-6          (default)
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

# subagent_type → model actually used
_MODEL_MAP = {
    "Explore": "claude-haiku-4-5-20251001",
    "general-purpose": "claude-haiku-4-5-20251001",
    "Plan": "claude-sonnet-4-6",
    "planner": "claude-sonnet-4-6",
    "architect": "claude-sonnet-4-6",
    "code-architect": "claude-sonnet-4-6",
    "code-explorer": "claude-sonnet-4-6",
    "code-reviewer": "claude-sonnet-4-6",
    "feature-dev": "claude-sonnet-4-6",
}
_DEFAULT_MODEL = "claude-sonnet-4-6"

# Lightweight Anthropic subagents are credited against Haiku as baseline.
# Heavier ones (Plan, code-*, etc.) are credited against Sonnet. The dashboard
# layer can still re-bucket if needed — this is the conservative default.
_HAIKU_BASELINE_SUBAGENTS = {"Explore", "general-purpose"}
_BASELINE_HAIKU = "haiku"
_BASELINE_SONNET = "sonnet"

# USD per token. Used for potential_cost_usd / saved_usd estimation.
_PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5-20251001": (0.25e-6, 1.25e-6),
    "claude-sonnet-4-6":         (3.0e-6,  15.0e-6),
    "claude-opus-4-6":           (15.0e-6, 75.0e-6),
}


def _db_path() -> Path:
    """Resolve the canonical usage DB lazily so tests that patch Path.home() work."""
    return Path.home() / ".llm-router" / "usage.db"


def _infer_model(subagent_type: str) -> str:
    return _MODEL_MAP.get(subagent_type, _DEFAULT_MODEL)


def _infer_baseline(subagent_type: str) -> str:
    return _BASELINE_HAIKU if subagent_type in _HAIKU_BASELINE_SUBAGENTS else _BASELINE_SONNET


def _estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate counterfactual API cost — actual billing is $0 via subscription."""
    in_p, out_p = _PRICES.get(model, _PRICES[_DEFAULT_MODEL])
    return round(in_p * input_tokens + out_p * output_tokens, 8)


def _ensure_table(db: sqlite3.Connection) -> None:
    """Create the usage table only if it doesn't exist yet.

    The full schema lives in cost.py — it adds baseline_model, potential_cost_usd,
    saved_usd, and several other columns via migrations. To keep this hook
    forward-compatible when run before cost.py has touched the DB, we mirror
    those columns here. SQLite's ``IF NOT EXISTS`` makes this a no-op when the
    canonical schema is already present.
    """
    db.execute(
        """CREATE TABLE IF NOT EXISTS usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT DEFAULT (datetime('now')),
            model TEXT NOT NULL,
            provider TEXT NOT NULL DEFAULT 'cc',
            task_type TEXT DEFAULT 'code',
            profile TEXT DEFAULT 'balanced',
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cost_usd REAL DEFAULT 0.0,
            latency_ms REAL DEFAULT 0.0,
            success INTEGER DEFAULT 1,
            baseline_model TEXT,
            potential_cost_usd REAL DEFAULT 0.0,
            saved_usd REAL DEFAULT 0.0,
            complexity TEXT DEFAULT 'moderate'
        )"""
    )


def _log_to_db(
    model: str,
    baseline_model: str,
    input_tokens: int,
    output_tokens: int,
    latency_ms: float,
    success: bool,
) -> None:
    db_path = _db_path()
    if not db_path.parent.exists():
        return
    try:
        with sqlite3.connect(str(db_path), timeout=5) as db:
            db.execute("PRAGMA journal_mode=WAL")
            _ensure_table(db)
            potential = _estimate_cost(model, input_tokens, output_tokens)
            # cost_usd = 0 (subscription), so saved_usd = potential − 0 = potential.
            saved = potential
            db.execute(
                """INSERT INTO usage
                   (model, provider, task_type, profile,
                    input_tokens, output_tokens, cost_usd, latency_ms, success,
                    baseline_model, potential_cost_usd, saved_usd)
                   VALUES (?, 'cc', 'code', 'balanced', ?, ?, 0.0, ?, ?, ?, ?, ?)""",
                (
                    model,
                    input_tokens,
                    output_tokens,
                    latency_ms,
                    1 if success else 0,
                    baseline_model,
                    potential,
                    saved,
                ),
            )
            db.commit()
    except Exception:
        pass  # never let tracking break Claude Code


def main() -> None:
    try:
        data = json.loads(sys.stdin.read())
    except Exception:
        sys.exit(0)

    tool_name = data.get("tool_name", "")
    if tool_name != "Agent":
        sys.exit(0)

    tool_input  = data.get("tool_input", {})
    tool_result = data.get("tool_response", data.get("tool_result", {}))

    subagent_type = tool_input.get("subagent_type", "general-purpose")
    prompt        = tool_input.get("prompt", "")
    result_text   = ""
    if isinstance(tool_result, dict):
        result_text = tool_result.get("output", tool_result.get("content", ""))
    elif isinstance(tool_result, str):
        result_text = tool_result

    model         = _infer_model(subagent_type)
    baseline      = _infer_baseline(subagent_type)
    input_tokens  = max(1, len(prompt) // 4)
    output_tokens = max(1, len(result_text) // 4)
    latency_ms    = data.get("duration_ms", 0.0)

    _log_to_db(model, baseline, input_tokens, output_tokens, float(latency_ms), success=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
