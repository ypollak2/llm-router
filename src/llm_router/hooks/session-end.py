#!/usr/bin/env python3
# llm-router-hook-version: 3
"""Stop hook — session routing summary with real cost data from SQLite.

Fires when a Claude Code session ends. Queries the routing_decisions SQLite
table for calls made since session start and prints:
  - Which tools were called and how many times
  - Which models actually handled the tasks
  - Real external cost (from cost_usd column)
  - Estimated savings vs a Claude Sonnet 4.6 baseline

Baseline: Claude Sonnet 4.6 @ $3/M input + $15/M output tokens.
Savings = what Sonnet would have cost - what the external model actually cost.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

STATE_DIR = os.path.expanduser("~/.llm-router")
SESSION_START_FILE = os.path.join(STATE_DIR, "session_start.txt")
DB_PATH = os.path.join(STATE_DIR, "usage.db")

# Sonnet 4.6 pricing (the baseline — what Claude Code itself runs on)
SONNET_INPUT_PER_M = 3.0    # $3 per million input tokens
SONNET_OUTPUT_PER_M = 15.0  # $15 per million output tokens

# Tool display config: (display_label, typical_model_short_name)
TOOL_LABELS: dict[str, tuple[str, str]] = {
    "llm_query":    ("llm_query   ", "Haiku   "),
    "llm_code":     ("llm_code    ", "Sonnet  "),
    "llm_generate": ("llm_generate", "Flash   "),
    "llm_research": ("llm_research", "Perplx  "),
    "llm_analyze":  ("llm_analyze ", "Sonnet  "),
    "llm_edit":     ("llm_edit    ", "Haiku   "),
    "llm_image":    ("llm_image   ", "Imagen  "),
    "llm_stream":   ("llm_stream  ", "varies  "),
    "llm_route":    ("llm_route   ", "auto    "),
}


def _bar(count: int, max_count: int, width: int = 16) -> str:
    if max_count == 0:
        return "░" * width
    filled = round(count / max_count * width)
    return "█" * filled + "░" * (width - filled)


def _read_session_start() -> float:
    try:
        with open(SESSION_START_FILE) as f:
            return float(f.read().strip())
    except (FileNotFoundError, ValueError, OSError):
        return time.time() - 3600  # default: last hour


def _session_start_iso(session_start: float) -> str:
    return datetime.fromtimestamp(session_start, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _query_session_data(session_start: float) -> list[dict]:
    """Read usage rows since session_start from SQLite."""
    if not os.path.exists(DB_PATH):
        return []
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT task_type, model, input_tokens, output_tokens,
                   cost_usd, success
            FROM usage
            WHERE timestamp >= ? AND success = 1
            ORDER BY rowid
            """,
            (_session_start_iso(session_start),),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _aggregate(rows: list[dict]) -> dict[str, dict]:
    """Aggregate per-row data into per-tool totals."""
    tools: dict[str, dict] = {}
    for r in rows:
        task = r.get("task_type", "unknown")
        tool = f"llm_{task}"
        model = r.get("model", "unknown")
        in_tok = r.get("input_tokens") or 0
        out_tok = r.get("output_tokens") or 0
        cost = r.get("cost_usd") or 0.0

        if tool not in tools:
            tools[tool] = {
                "count": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "actual_cost": 0.0,
                "top_model": {},
            }
        tools[tool]["count"] += 1
        tools[tool]["input_tokens"] += in_tok
        tools[tool]["output_tokens"] += out_tok
        tools[tool]["actual_cost"] += cost
        tools[tool]["top_model"][model] = tools[tool]["top_model"].get(model, 0) + 1

    return tools


def _sonnet_baseline(in_tok: int, out_tok: int) -> float:
    """What these tokens would have cost routed through Claude Sonnet 4.6."""
    return (in_tok * SONNET_INPUT_PER_M + out_tok * SONNET_OUTPUT_PER_M) / 1_000_000


def _format_summary(tools: dict[str, dict]) -> str:
    if not tools:
        return ""

    total_calls = sum(t["count"] for t in tools.values())
    total_in_tok = sum(t["input_tokens"] for t in tools.values())
    total_out_tok = sum(t["output_tokens"] for t in tools.values())
    total_actual = sum(t["actual_cost"] for t in tools.values())
    total_baseline = _sonnet_baseline(total_in_tok, total_out_tok)
    total_saved = max(0.0, total_baseline - total_actual)

    max_count = max(t["count"] for t in tools.values())

    lines = [
        "━━ Session Routing Summary ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]

    for tool, data in sorted(tools.items(), key=lambda x: -x[1]["count"]):
        label, default_model = TOOL_LABELS.get(tool, (f"{tool:<12}", "?       "))
        n = data["count"]
        bar = _bar(n, max_count)

        # Show the most-used model for this tool
        top_model = max(data["top_model"], key=data["top_model"].get) if data["top_model"] else "?"
        model_short = top_model.split("/", 1)[-1][:8] if "/" in top_model else top_model[:8]

        # Per-tool savings
        baseline = _sonnet_baseline(data["input_tokens"], data["output_tokens"])
        actual = data["actual_cost"]
        saved = max(0.0, baseline - actual)

        lines.append(
            f"  {label}  {bar}  {n:3}x  {model_short:<8}  "
            f"${actual:.4f} actual  ~${saved:.4f} saved"
        )

    lines.append(f"  {'─' * 65}")

    # Token summary
    if total_in_tok or total_out_tok:
        lines.append(
            f"  Tokens: {total_in_tok:,} in + {total_out_tok:,} out"
            f"  |  Actual cost: ${total_actual:.4f}"
            f"  |  Sonnet baseline: ${total_baseline:.4f}"
        )

    lines.append(
        f"  {total_calls} tasks routed  |  ~${total_saved:.4f} saved vs Sonnet 4.6"
    )
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    return "\n".join(lines)


def main() -> None:
    try:
        json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        pass

    session_start = _read_session_start()
    rows = _query_session_data(session_start)

    if not rows:
        sys.exit(0)

    tools = _aggregate(rows)
    summary = _format_summary(tools)
    if not summary:
        sys.exit(0)

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "Stop",
            "systemMessage": summary,
        }
    }))


if __name__ == "__main__":
    main()
