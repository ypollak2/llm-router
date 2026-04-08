#!/usr/bin/env python3
# llm-router-hook-version: 12
"""Stop hook — unified session summary: CC subscription delta + external routing costs."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

STATE_DIR            = os.path.expanduser("~/.llm-router")
SESSION_START_FILE   = os.path.join(STATE_DIR, "session_start.txt")
SESSION_CC_SNAP_FILE = os.path.join(STATE_DIR, "session_start_cc_pct.json")
DB_PATH              = os.path.join(STATE_DIR, "usage.db")
USAGE_JSON           = os.path.join(STATE_DIR, "usage.json")
STAR_CTA_FILE        = os.path.join(STATE_DIR, "star_cta_shown.txt")

# Show star CTA once the user has saved at least this much (lifetime)
STAR_CTA_THRESHOLD_USD = 0.50

SONNET_INPUT_PER_M  = 3.0
SONNET_OUTPUT_PER_M = 15.0
WIDTH = 64


# ── Claude subscription ────────────────────────────────────────────────────────

def _fetch_live_usage() -> dict | None:
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
        s = float(data.get("five_hour",       {}).get("utilization", 0.0))
        w = float(data.get("seven_day",        {}).get("utilization", 0.0))
        n = float(data.get("seven_day_sonnet", {}).get("utilization", 0.0))
        result = {"session_pct": round(s, 1), "weekly_pct": round(w, 1),
                  "sonnet_pct": round(n, 1), "updated_at": time.time()}
        # Persist for routing pressure
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(USAGE_JSON, "w") as f:
            json.dump({**result, "highest_pressure": max(s, w, n) / 100.0}, f)
        # Update the session-start snapshot with current live values so the
        # NEXT session's delta is computed against today's end-of-session baseline,
        # not a stale snapshot. This is the fallback if session-start fails to refresh.
        with open(SESSION_CC_SNAP_FILE, "w") as f:
            json.dump(result, f)
        return result
    except Exception:
        return None


def _read_json(path: str) -> dict | None:
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _get_cc_usage() -> tuple[dict | None, dict | None, bool]:
    """Return (start_snapshot, current_usage, is_live)."""
    start  = _read_json(SESSION_CC_SNAP_FILE)
    live   = _fetch_live_usage()
    if live:
        return start, live, True
    cached = _read_json(USAGE_JSON)
    return start, cached, False


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


def _query_session_data(session_start: float) -> tuple[list[dict], list[dict], list[dict]]:
    """Return (paid_rows, cc_rows, free_rows) split by provider type."""
    if not os.path.exists(DB_PATH):
        return [], [], []
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
        paid  = [r for r in all_rows
                 if r.get("provider") not in _FREE_PROVIDERS | {"subscription"}]
        cc    = [r for r in all_rows if r.get("provider") == "subscription"]
        free  = [r for r in all_rows if r.get("provider") in _FREE_PROVIDERS]
        return paid, cc, free
    except Exception:
        return [], [], []


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


def _cc_row(label: str, start_pct: float | None, end_pct: float) -> str:
    """Format one CC subscription row.

    start_pct=None means no snapshot available (first session or missing file).
    """
    bar = _bar(end_pct)
    if start_pct is not None:
        delta = end_pct - start_pct
        sign  = "+" if delta >= 0 else ""
        return (
            f"  {label:<16}  {bar}  "
            f"{start_pct:>4.1f}% → {end_pct:>4.1f}%  ({sign}{delta:.1f}pp this session)"
        )
    return f"  {label:<16}  {bar}  {end_pct:>4.1f}%"


def _format_cc_section(start: dict | None, current: dict, is_live: bool) -> list[str]:
    src   = "live" if is_live else "cached ⚠"
    lines = [f"  Claude Code subscription  ({src})"]
    lines.append("")

    s_end = current.get("session_pct", 0.0)
    w_end = current.get("weekly_pct",  0.0)
    n_end = current.get("sonnet_pct",  0.0)

    s_start = start.get("session_pct") if start else None
    w_start = start.get("weekly_pct")  if start else None
    n_start = start.get("sonnet_pct")  if start else None

    lines.append(_cc_row("session (5h)",  s_start, s_end))
    lines.append(_cc_row("weekly (all)",  w_start, w_end))
    if n_end > 0 or (n_start is not None and n_start > 0):
        lines.append(_cc_row("weekly sonnet", n_start, n_end))

    return lines


def _format_cc_model_section(cc_rows: list[dict]) -> list[str]:
    """Format per-model CC call counts — same table style as external routing."""
    models: dict[str, dict] = {}
    for r in cc_rows:
        model = r.get("model", "?")
        task  = r.get("task_type", "?")
        if model not in models:
            models[model] = {"count": 0, "tasks": {}}
        models[model]["count"] += 1
        models[model]["tasks"][task] = models[model]["tasks"].get(task, 0) + 1

    total = sum(m["count"] for m in models.values())
    lines = [f"  Claude Code models  {total} calls  (subscription, $0.00)"]
    lines.append("")
    for model, d in sorted(models.items(), key=lambda x: -x[1]["count"]):
        # Short model name: haiku / sonnet / opus
        short = model.split("/", 1)[-1] if "/" in model else model
        if len(short) > 30:
            short = short[:28] + "…"
        top_task = max(d["tasks"], key=d["tasks"].get) if d["tasks"] else "?"
        lines.append(f"  {top_task:<12}  {d['count']:>3}×  {short:<32}  (sub)")
    return lines


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
    """Format free-model (Ollama / Codex) session savings.

    Codex doesn't track tokens; we estimate from the avg tokens/call across paid rows.
    """
    if not free_rows:
        return []

    # Compute avg tokens/call from paid rows (for Codex estimation)
    paid_with_tokens = [r for r in paid_rows if (r.get("input_tokens") or 0) > 0]
    if paid_with_tokens:
        avg_in  = sum(r.get("input_tokens",  0) for r in paid_with_tokens) / len(paid_with_tokens)
        avg_out = sum(r.get("output_tokens", 0) for r in paid_with_tokens) / len(paid_with_tokens)
    else:
        avg_in, avg_out = 500.0, 300.0  # conservative fallback

    # Aggregate by provider
    by_provider: dict[str, dict] = {}
    for r in free_rows:
        p = r.get("provider", "?")
        if p not in by_provider:
            by_provider[p] = {"calls": 0, "in": 0, "out": 0, "estimated": False}
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


def _format(tools: dict[str, dict], cc_rows: list[dict], free_rows: list[dict],
            paid_rows: list[dict],
            start: dict | None, current: dict | None, is_live: bool) -> str:
    lines = ["─" * WIDTH]

    if current:
        lines += _format_cc_section(start, current, is_live)

    if cc_rows:
        lines.append("")
        lines += _format_cc_model_section(cc_rows)

    if free_rows:
        lines.append("")
        lines += _format_free_section(free_rows, paid_rows)

    if tools:
        lines.append("")
        lines += _format_routing_section(tools)

    # Combined savings tip
    paid_saved = _total_saved(tools) if tools else 0.0
    free_saved = sum(
        max(0.0, _sonnet_baseline(
            r.get("input_tokens", 0) or 0,
            r.get("output_tokens", 0) or 0,
        ))
        for r in free_rows
    )
    total_saved = paid_saved + free_saved
    if total_saved >= 0.001:
        lines.append("")
        if _should_show_star_cta(total_saved):
            lines.append(
                f'  💡 Saved ~${total_saved:.2f} this session with llm-router'
            )
            lines.append("")
            lines.append(
                '  ⭐  Enjoying the savings? A star on GitHub helps others find it:'
            )
            lines.append(
                '      github.com/ypollak2/llm-router'
            )
            lines.append(
                '      (run `llm-router share` to post your savings)'
            )
        else:
            lines.append(
                f'  💡 Saved ~${total_saved:.2f} this session · '
                f'run `llm-router share` to post it'
            )

    lines.append("─" * WIDTH)
    return "\n".join(lines)


# ── Star CTA ───────────────────────────────────────────────────────────────────

def _lifetime_saved() -> float:
    """Return total lifetime savings (USD) across all providers."""
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
    """Return True the first time lifetime savings crosses STAR_CTA_THRESHOLD_USD."""
    if session_saved <= 0.0:
        return False
    if os.path.exists(STAR_CTA_FILE):
        return False
    lifetime = _lifetime_saved()
    if lifetime >= STAR_CTA_THRESHOLD_USD:
        # Mark as shown so it only fires once
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

    session_start               = _read_session_start()
    paid_rows, cc_rows, free_rows = _query_session_data(session_start)
    tools                       = _aggregate(paid_rows) if paid_rows else {}
    start, current, is_live     = _get_cc_usage()

    if not tools and not cc_rows and not current and not free_rows:
        sys.exit(0)

    summary = _format(tools, cc_rows, free_rows, paid_rows, start, current, is_live)
    print(json.dumps({"systemMessage": summary}))


if __name__ == "__main__":
    main()
