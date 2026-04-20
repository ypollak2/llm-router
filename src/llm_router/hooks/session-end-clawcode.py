#!/usr/bin/env python3
# llm-router-hook-version: 1
"""Stop hook (claw-code variant) — session summary: external routing costs + free-model savings.

Identical to session-end.py but omits Claude Code subscription pressure sections
(claw-code has no Anthropic OAuth — every call is a paid API call).
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

STATE_DIR            = os.path.expanduser("~/.llm-router")
SESSION_START_FILE   = os.path.join(STATE_DIR, "session_start.txt")
DB_PATH              = os.path.join(STATE_DIR, "usage.db")
STAR_CTA_FILE        = os.path.join(STATE_DIR, "star_cta_shown.txt")

STAR_CTA_THRESHOLD_USD = 0.50

SONNET_INPUT_PER_M  = 3.0
SONNET_OUTPUT_PER_M = 15.0
WIDTH = 64


# ── External routing (SQLite) ──────────────────────────────────────────────────

def _read_session_start() -> float:
    try:
        with open(SESSION_START_FILE) as f:
            return float(f.read().strip())
    except (FileNotFoundError, ValueError, OSError):
        return time.time() - 3600


def _session_start_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


_FREE_PROVIDERS = {"ollama", "codex"}


def _query_session_data(session_start: float) -> tuple[list[dict], list[dict]]:
    """Return (paid_rows, free_rows) split by provider type."""
    if not os.path.exists(DB_PATH):
        return [], []
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT task_type, model, provider, input_tokens, output_tokens, cost_usd
            FROM usage
            WHERE timestamp >= ? AND success = 1
            ORDER BY rowid
            """,
            (_session_start_iso(session_start),),
        ).fetchall()
        conn.close()
        all_rows = [dict(r) for r in rows]
        paid = [r for r in all_rows
                if r.get("provider") not in _FREE_PROVIDERS | {"subscription"}]
        free = [r for r in all_rows if r.get("provider") in _FREE_PROVIDERS]
        return paid, free
    except Exception:
        return [], []


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

def _bar(pct: float, bar_width: int = 20) -> str:
    filled = max(0, min(bar_width, round(pct / 100 * bar_width)))
    return "█" * filled + "░" * (bar_width - filled)


def _format_routing_section(tools: dict[str, dict]) -> list[str]:
    total_calls = sum(t["count"] for t in tools.values())
    total_in    = sum(t["in"]    for t in tools.values())
    total_out   = sum(t["out"]   for t in tools.values())
    total_cost  = sum(t["cost"]  for t in tools.values())
    total_base  = _sonnet_baseline(total_in, total_out)
    total_saved = max(0.0, total_base - total_cost)
    savings_pct = round(total_saved / total_base * 100) if total_base > 0 else 0

    lines = [
        f"  External routing  "
        f"{total_calls} calls  ·  ${total_cost:.4f}  ·  {savings_pct}% saved vs Sonnet",
        "",
    ]
    for tool, d in sorted(tools.items(), key=lambda x: -x[1]["count"]):
        top_model   = max(d["models"], key=d["models"].get) if d["models"] else "?"
        model_short = top_model.split("/", 1)[-1] if "/" in top_model else top_model
        if len(model_short) > 22:
            model_short = model_short[:20] + "…"
        lines.append(
            f"  {tool:<12}  {d['count']:>3}×  {model_short:<24}  ${d['cost']:.4f}"
        )
    return lines


def _total_saved(tools: dict[str, dict]) -> float:
    total_in   = sum(t["in"]   for t in tools.values())
    total_out  = sum(t["out"]  for t in tools.values())
    total_cost = sum(t["cost"] for t in tools.values())
    baseline   = _sonnet_baseline(total_in, total_out)
    return max(0.0, baseline - total_cost)


def _format_free_section(free_rows: list[dict], paid_rows: list[dict]) -> list[str]:
    if not free_rows:
        return []

    paid_with_tokens = [r for r in paid_rows if (r.get("input_tokens") or 0) > 0]
    if paid_with_tokens:
        avg_in  = sum(r.get("input_tokens",  0) for r in paid_with_tokens) / len(paid_with_tokens)
        avg_out = sum(r.get("output_tokens", 0) for r in paid_with_tokens) / len(paid_with_tokens)
    else:
        avg_in, avg_out = 500.0, 300.0

    by_provider: dict[str, dict] = {}
    for r in free_rows:
        p = r.get("provider", "?")
        if p not in by_provider:
            by_provider[p] = {"calls": 0, "in": 0, "out": 0}
        by_provider[p]["calls"] += 1
        by_provider[p]["in"]    += r.get("input_tokens",  0) or 0
        by_provider[p]["out"]   += r.get("output_tokens", 0) or 0

    total_saved = 0.0
    total_calls = len(free_rows)
    body: list[str] = []
    for provider, d in sorted(by_provider.items(), key=lambda x: -x[1]["calls"]):
        in_t, out_t = d["in"], d["out"]
        est = False
        if in_t == 0 and out_t == 0:
            in_t  = int(avg_in  * d["calls"])
            out_t = int(avg_out * d["calls"])
            est   = True
        baseline = _sonnet_baseline(in_t, out_t)
        saved    = max(0.0, baseline)
        total_saved += saved
        est_tag  = " ~est" if est else ""
        in_k  = f"{in_t  // 1000}k" if in_t  >= 1000 else str(in_t)
        out_k = f"{out_t // 1000}k" if out_t >= 1000 else str(out_t)
        body.append(
            f"  {provider:<10}  {d['calls']:>3}×  "
            f"{in_k}↑ {out_k}↓{est_tag:<5}  ${saved:.4f} saved"
        )

    lines = [f"  Free models  {total_calls} calls  ·  ${total_saved:.4f} saved vs Sonnet"
             + "  (Ollama/Codex)", ""]
    lines += body
    return lines


def _format(tools: dict[str, dict], free_rows: list[dict], paid_rows: list[dict]) -> str:
    lines = ["─" * WIDTH]

    if free_rows:
        lines += _format_free_section(free_rows, paid_rows)

    if tools:
        lines.append("")
        lines += _format_routing_section(tools)


    lines.append("─" * WIDTH)
    return "\n".join(lines)


# ── Star CTA ───────────────────────────────────────────────────────────────────

def _lifetime_saved() -> float:
    if not os.path.exists(DB_PATH):
        return 0.0
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT provider, input_tokens, output_tokens, cost_usd "
            "FROM usage WHERE success=1"
        ).fetchall()
        conn.close()
        saved = 0.0
        for provider, in_tok, out_tok, cost in rows:
            base = ((in_tok or 0) * SONNET_INPUT_PER_M
                    + (out_tok or 0) * SONNET_OUTPUT_PER_M) / 1_000_000
            if provider in _FREE_PROVIDERS:
                saved += base
            elif provider != "subscription":
                saved += max(0.0, base - (cost or 0.0))
        return saved
    except Exception:
        return 0.0


def _should_show_star_cta(session_saved: float) -> bool:
    if session_saved <= 0.0:
        return False
    if os.path.exists(STAR_CTA_FILE):
        return False
    lifetime = _lifetime_saved()
    if lifetime >= STAR_CTA_THRESHOLD_USD:
        try:
            with open(STAR_CTA_FILE, "w") as f:
                f.write(f"{lifetime:.4f}")
        except OSError:
            pass
        return True
    return False


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    try:
        json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        pass

    session_start     = _read_session_start()
    paid_rows, free_rows = _query_session_data(session_start)
    tools             = _aggregate(paid_rows) if paid_rows else {}

    if not tools and not free_rows:
        sys.exit(0)

    summary = _format(tools, free_rows, paid_rows)
    print(json.dumps({"systemMessage": summary}))


if __name__ == "__main__":
    main()
