#!/usr/bin/env python3
# llm-router-hook-version: 6
"""Stop hook — unified session summary: live CC subscription + external routing costs."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

STATE_DIR           = os.path.expanduser("~/.llm-router")
SESSION_START_FILE  = os.path.join(STATE_DIR, "session_start.txt")
DB_PATH             = os.path.join(STATE_DIR, "usage.db")
USAGE_JSON          = os.path.join(STATE_DIR, "usage.json")

SONNET_INPUT_PER_M  = 3.0
SONNET_OUTPUT_PER_M = 15.0


# ── Claude subscription (live refresh) ────────────────────────────────────────

def _fetch_live_usage() -> dict | None:
    """Fetch fresh subscription usage from the OAuth API.

    Returns a dict with session_pct, weekly_pct, sonnet_pct (0-100 floats),
    or None if the fetch fails (caller falls back to usage.json).
    """
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=6,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return None
        token = json.loads(r.stdout.strip()).get("claudeAiOauth", {}).get("accessToken", "")
        if not token:
            return None
    except Exception:
        return None

    try:
        req = urllib.request.Request(
            "https://api.anthropic.com/api/oauth/usage",
            headers={"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
    except Exception:
        return None

    try:
        s = float(data.get("five_hour",        {}).get("utilization", 0.0))
        w = float(data.get("seven_day",         {}).get("utilization", 0.0))
        n = float(data.get("seven_day_sonnet",  {}).get("utilization", 0.0))
        result = {"session_pct": round(s, 1), "weekly_pct": round(w, 1),
                  "sonnet_pct": round(n, 1), "updated_at": time.time()}
        # Persist so routing pressure is up-to-date for the next session
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(USAGE_JSON, "w") as f:
            json.dump({**result, "highest_pressure": max(s, w, n) / 100.0}, f)
        return result
    except Exception:
        return None


def _read_cached_usage() -> dict | None:
    try:
        with open(USAGE_JSON) as f:
            return json.load(f)
    except Exception:
        return None


def _get_cc_usage() -> tuple[dict | None, bool]:
    """Return (usage_dict, is_live). Tries live API first, falls back to file."""
    live = _fetch_live_usage()
    if live:
        return live, True
    cached = _read_cached_usage()
    return cached, False


# ── External routing (SQLite) ──────────────────────────────────────────────────

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
        tool    = r.get("task_type", "unknown")
        model   = r.get("model", "?")
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


# ── Formatting ─────────────────────────────────────────────────────────────────

def _bar(pct: float, width: int = 20) -> str:
    filled = round(pct / 100 * width)
    return "█" * filled + "░" * (width - filled)


def _format_cc_section(usage: dict, is_live: bool, width: int) -> list[str]:
    s = usage.get("session_pct", 0)
    w = usage.get("weekly_pct",  0)
    n = usage.get("sonnet_pct",  0)
    src = "live" if is_live else "cached ⚠"

    lines = [f"  Claude Code subscription  ({src})"]
    lines.append(f"  {'session':<8}  {_bar(s)}  {s:>3.0f}%")
    lines.append(f"  {'weekly':<8}  {_bar(w)}  {w:>3.0f}%")
    if n > 0:
        lines.append(f"  {'sonnet':<8}  {_bar(n)}  {n:>3.0f}%")
    return lines


def _format(tools: dict[str, dict], cc_usage: dict | None, is_live: bool) -> str:
    WIDTH = 62

    total_calls = sum(t["count"] for t in tools.values())
    total_in    = sum(t["in"]    for t in tools.values())
    total_out   = sum(t["out"]   for t in tools.values())
    total_cost  = sum(t["cost"]  for t in tools.values())
    total_base  = _sonnet_baseline(total_in, total_out)
    total_saved = max(0.0, total_base - total_cost)
    savings_pct = round(total_saved / total_base * 100) if total_base > 0 else 0

    lines = ["─" * WIDTH]

    # ── CC subscription block (always show if available) ──────────────────────
    if cc_usage:
        lines += _format_cc_section(cc_usage, is_live, WIDTH)
        lines.append("")

    # ── External routing block ────────────────────────────────────────────────
    if tools:
        lines.append(
            f"  External routing  "
            f"{total_calls} calls  ·  ${total_cost:.4f}  ·  {savings_pct}% saved vs Sonnet"
        )
        lines.append("")
        for tool, d in sorted(tools.items(), key=lambda x: -x[1]["count"]):
            top_model   = max(d["models"], key=d["models"].get) if d["models"] else "?"
            model_short = top_model.split("/", 1)[-1] if "/" in top_model else top_model
            if len(model_short) > 22:
                model_short = model_short[:20] + "…"
            lines.append(
                f"  {tool:<12}  {d['count']:>3}×  {model_short:<24}  ${d['cost']:.4f}"
            )

    lines.append("─" * WIDTH)
    return "\n".join(lines)


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    try:
        json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        pass

    session_start = _read_session_start()
    rows          = _query_session_data(session_start)
    tools         = _aggregate(rows) if rows else {}
    cc_usage, is_live = _get_cc_usage()

    # Nothing to show
    if not tools and not cc_usage:
        sys.exit(0)

    summary = _format(tools, cc_usage, is_live)
    print(json.dumps({"systemMessage": summary}))


if __name__ == "__main__":
    main()
