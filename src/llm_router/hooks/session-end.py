#!/usr/bin/env python3
# llm-router-hook-version: 4
"""Stop hook — session routing summary with real cost data from SQLite."""

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
USAGE_JSON = os.path.join(STATE_DIR, "usage.json")

SONNET_INPUT_PER_M  = 3.0
SONNET_OUTPUT_PER_M = 15.0


def _read_session_start() -> float:
    try:
        with open(SESSION_START_FILE) as f:
            return float(f.read().strip())
    except (FileNotFoundError, ValueError, OSError):
        return time.time() - 3600


def _session_start_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _query_session_data(session_start: float) -> list[dict]:
    if not os.path.exists(DB_PATH):
        return []
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT task_type, model, input_tokens, output_tokens, cost_usd
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
    tools: dict[str, dict] = {}
    for r in rows:
        tool = r.get("task_type", "unknown")
        model = r.get("model", "?")
        in_tok  = r.get("input_tokens")  or 0
        out_tok = r.get("output_tokens") or 0
        cost    = r.get("cost_usd")      or 0.0
        if tool not in tools:
            tools[tool] = {"count": 0, "in": 0, "out": 0, "cost": 0.0, "models": {}}
        tools[tool]["count"]  += 1
        tools[tool]["in"]     += in_tok
        tools[tool]["out"]    += out_tok
        tools[tool]["cost"]   += cost
        tools[tool]["models"][model] = tools[tool]["models"].get(model, 0) + 1
    return tools


def _sonnet_baseline(in_tok: int, out_tok: int) -> float:
    return (in_tok * SONNET_INPUT_PER_M + out_tok * SONNET_OUTPUT_PER_M) / 1_000_000


def _cc_line() -> str:
    try:
        with open(USAGE_JSON) as f:
            d = json.load(f)
        stale = (time.time() - d.get("updated_at", 0)) > 1800
        s, w, n = d.get("session_pct", 0), d.get("weekly_pct", 0), d.get("sonnet_pct", 0)
        mark = "  ⚠ stale" if stale else ""
        return f"CC  session {s:.0f}%  ·  weekly {w:.0f}%  ·  sonnet {n:.0f}%{mark}"
    except Exception:
        return ""


def _format(tools: dict[str, dict]) -> str:
    if not tools:
        return ""

    total_calls   = sum(t["count"] for t in tools.values())
    total_in      = sum(t["in"]    for t in tools.values())
    total_out     = sum(t["out"]   for t in tools.values())
    total_cost    = sum(t["cost"]  for t in tools.values())
    total_base    = _sonnet_baseline(total_in, total_out)
    total_saved   = max(0.0, total_base - total_cost)
    savings_pct   = round(total_saved / total_base * 100) if total_base > 0 else 0

    # ── header ───────────────────────────────────────────────────────────────
    header = f"  {total_calls} calls  ·  ${total_cost:.4f}  ·  {savings_pct}% saved vs Sonnet"
    width  = max(60, len(header) + 2)
    lines  = [f"{'─' * width}", header]

    # ── per-tool rows ─────────────────────────────────────────────────────────
    lines.append("")
    for tool, d in sorted(tools.items(), key=lambda x: -x[1]["count"]):
        top_model = max(d["models"], key=d["models"].get) if d["models"] else "?"
        # strip provider prefix (e.g. "openai/gpt-4o" → "gpt-4o")
        model_short = top_model.split("/", 1)[-1] if "/" in top_model else top_model
        # truncate long model names
        if len(model_short) > 22:
            model_short = model_short[:20] + "…"
        lines.append(
            f"  {tool:<12}  {d['count']:>3}×  {model_short:<24}  ${d['cost']:.4f}"
        )

    # ── footer ────────────────────────────────────────────────────────────────
    lines.append("")
    cc = _cc_line()
    if cc:
        lines.append(f"  {cc}")
    lines.append(f"{'─' * width}")

    return "\n".join(lines)


def main() -> None:
    try:
        json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        pass

    session_start = _read_session_start()
    rows  = _query_session_data(session_start)
    if not rows:
        sys.exit(0)

    summary = _format(_aggregate(rows))
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
