#!/usr/bin/env python3
# llm-router-hook-version: 14
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

# Import timeout config from llm_router package if available
try:
    from llm_router.timeout_config import subprocess_timeout, http_timeout
except ImportError:
    # Fallback to hardcoded defaults if llm_router not installed
    def subprocess_timeout() -> int:
        return int(os.environ.get("LLM_ROUTER_SUBPROCESS_TIMEOUT", "15"))
    def http_timeout() -> int:
        return int(os.environ.get("LLM_ROUTER_HTTP_TIMEOUT", "10"))

STATE_DIR            = os.path.expanduser("~/.llm-router")
SESSION_START_FILE   = os.path.join(STATE_DIR, "session_start.txt")
SESSION_ID_FILE      = os.path.join(STATE_DIR, "session_id.txt")
SESSION_CC_SNAP_FILE = os.path.join(STATE_DIR, "session_start_cc_pct.json")
DB_PATH              = os.path.join(STATE_DIR, "usage.db")
USAGE_JSON           = os.path.join(STATE_DIR, "usage.json")
STAR_CTA_FILE        = os.path.join(STATE_DIR, "star_cta_shown.txt")
SAVINGS_LOG_PATH     = os.path.join(STATE_DIR, "savings_log.jsonl")
SESSION_SPEND_FILE   = os.path.join(STATE_DIR, "session_spend.json")

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
            capture_output=True, text=True, timeout=subprocess_timeout(),
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
        # Persist for routing pressure only — do NOT write SESSION_CC_SNAP_FILE here.
        # Writing the snapshot from _fetch_live_usage() causes mid-session usage-refresh
        # calls to clobber the session-start baseline, making start == end (delta = 0).
        # SESSION_CC_SNAP_FILE is updated only once: in main(), after the delta is computed.
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(USAGE_JSON, "w") as f:
            json.dump({**result, "highest_pressure": max(s, w, n) / 100.0}, f)
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


def _render_quota_timeline(session_id: str | None, db_path: str) -> str:
    """Render per-prompt Claude quota timeline for audit trail.
    
    Shows how weekly quota pressure changed throughout the session,
    correlated with routing decisions and complexity downgrade events.
    Returns an empty string if no session_id or no quota snapshots found.
    """
    if not session_id:
        return ""
    
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Query quota snapshots in order
        cursor.execute("""
            SELECT prompt_sequence, timestamp, final_model, final_provider,
                   claude_weekly_pct, was_cache_fresh, was_downgraded
            FROM quota_snapshots
            WHERE session_id = ?
            ORDER BY prompt_sequence
        """, (session_id,))
        
        rows = cursor.fetchall()
        conn.close()
        
        if not rows:
            return ""
        
        # Build timeline
        lines = ["\n  Claude Quota — Session Timeline", "  " + "─" * 60]
        lines.append(f"  {'#':<4} {'Time':<8} {'Model':<18} {'Weekly%':>8}  {'Fresh':>5}")
        
        for row in rows:
            seq = row["prompt_sequence"]
            ts = row["timestamp"]
            model = row["final_model"] or "?"
            weekly_pct = row["claude_weekly_pct"]
            fresh = "✓" if row["was_cache_fresh"] else "⚠"
            down = "↓" if row["was_downgraded"] else ""
            
            # Parse timestamp and extract time
            try:
                time_str = ts[11:19] if ts and len(ts) > 11 else "?"
            except (IndexError, TypeError):
                time_str = "?"
            
            pct_str = f"{weekly_pct*100:.0f}%"
            model_short = model[:18] if len(model) > 18 else model
            
            lines.append(f"  {seq:<4} {time_str:<8} {model_short:<18} {pct_str:>8}  {fresh:>5} {down}")
        
        if rows:
            start_pct = rows[0]["claude_weekly_pct"] * 100
            end_pct = rows[-1]["claude_weekly_pct"] * 100
            delta_pct = end_pct - start_pct
            lines.append("  " + "─" * 60)
            delta_str = f"+{delta_pct:.0f}pp" if delta_pct > 0 else f"{delta_pct:.0f}pp"
            lines.append(f"  Weekly quota: {start_pct:.0f}% → {end_pct:.0f}% ({delta_str})")
        
        return "\n".join(lines)
    except Exception:
        return ""  # Silently fail if quota timeline unavailable


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


_PERIODS = [
    ("today",     "date(timestamp) = date('now')"),
    ("this week", "timestamp >= datetime('now', '-7 days')"),
    ("this month","timestamp >= datetime('now', 'start of month')"),
    ("all time",  "1=1"),
]


def _sync_import_savings_log() -> None:
    """Flush JSONL savings records into savings_stats before querying cumulative data.

    The PostToolUse hook appends one JSON line per routed Codex/Ollama call to
    ``savings_log.jsonl``.  These records bypass the MCP server so they are never
    written to the ``usage`` table.  Without this flush, the cumulative totals in
    the session summary are one-session behind for free-provider calls.

    This is a synchronous, stdlib-only version of ``cost.import_savings_log()``.
    """
    if not os.path.exists(SAVINGS_LOG_PATH) or not os.path.exists(DB_PATH):
        return
    try:
        with open(SAVINGS_LOG_PATH) as f:
            raw = f.read().strip()
    except OSError:
        return
    if not raw:
        return

    records = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
            records.append((
                r.get("timestamp", ""),
                r.get("session_id", ""),
                r.get("task_type", "unknown"),
                float(r.get("estimated_claude_cost_saved", 0.0)),
                float(r.get("external_cost", 0.0)),
                r.get("model_used", ""),
                r.get("host", "claude_code"),
            ))
        except (json.JSONDecodeError, KeyError, ValueError):
            continue

    if not records:
        return
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""
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
        """)
        conn.executemany(
            "INSERT INTO savings_stats "
            "(timestamp, session_id, task_type, estimated_claude_cost_saved, external_cost, model_used, host) "
            "VALUES (?,?,?,?,?,?,?)",
            records,
        )
        conn.commit()
        conn.close()
        # Truncate only after successful commit
        with open(SAVINGS_LOG_PATH, "w") as f:
            f.write("")
    except Exception:
        pass


def _query_cumulative_savings() -> list[tuple[str, int, int, int, float]]:
    """Return list of (label, calls, total_in_tokens, total_out_tokens, saved_usd) per period."""
    if not os.path.exists(DB_PATH):
        return []
    results = []
    try:
        conn = sqlite3.connect(DB_PATH)
        # Check if savings_stats table exists (created on first JSONL import)
        has_savings_stats = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='savings_stats'"
        ).fetchone() is not None

        for label, where in _PERIODS:
            rows = conn.execute(
                f"""
                SELECT provider, COUNT(*), COALESCE(SUM(input_tokens),0),
                       COALESCE(SUM(output_tokens),0), COALESCE(SUM(cost_usd),0)
                FROM usage
                WHERE success=1 AND {where}
                GROUP BY provider
                """
            ).fetchall()
            calls = total_in = total_out = 0
            saved = 0.0
            for provider, cnt, in_tok, out_tok, cost in rows:
                calls   += cnt
                total_in  += in_tok
                total_out += out_tok
                baseline = _sonnet_baseline(in_tok, out_tok)
                if provider in _FREE_PROVIDERS:
                    saved += baseline          # free = full Sonnet cost avoided
                elif provider != "subscription":
                    saved += max(0.0, baseline - cost)

            # Also include pre-computed savings from savings_stats (Codex/Ollama JSONL records
            # that bypass the MCP server and are never in the usage table)
            if has_savings_stats:
                ss_rows = conn.execute(
                    f"SELECT COUNT(*), COALESCE(SUM(estimated_claude_cost_saved),0) "
                    f"FROM savings_stats WHERE {where}"
                ).fetchone()
                if ss_rows:
                    calls += ss_rows[0]
                    saved += ss_rows[1]

            results.append((label, calls, total_in, total_out, saved))
        conn.close()
    except Exception:
        pass
    return results


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


def _fmt_tok(n: int) -> str:
    """Human-readable token count: 1234 → 1.2k, 1234567 → 1.2M."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _format_cumulative_section(periods: list[tuple[str, int, int, int, float]]) -> list[str]:
    """Format daily/weekly/monthly/all-time cumulative savings table + yearly projection."""
    if not periods or all(p[1] == 0 for p in periods):
        return []
    lines = ["  Cumulative savings (vs Sonnet baseline)", ""]
    period_map = {label: (calls, ti, to, saved) for label, calls, ti, to, saved in periods}
    for label, calls, total_in, total_out, saved in periods:
        total_tok = total_in + total_out
        tok_str   = _fmt_tok(total_tok)
        lines.append(
            f"  {label:<12}  {calls:>5} calls  {tok_str:>7} tok  ${saved:.4f} saved"
        )
    # Yearly projection — prefer 30-day monthly rate, fall back to 7-day, then today
    from datetime import datetime as _dt
    days_this_month = max(1, _dt.now().day)  # days elapsed since start of month
    month_data   = period_map.get("this month", (0, 0, 0, 0.0))
    weekly_data  = period_map.get("this week",  (0, 0, 0, 0.0))
    today_data   = period_map.get("today",      (0, 0, 0, 0.0))
    month_saved  = month_data[3]
    weekly_saved = weekly_data[3]
    today_saved  = today_data[3]
    month_tok    = month_data[1]  + month_data[2]
    weekly_tok   = weekly_data[1] + weekly_data[2]
    today_tok    = today_data[1]  + today_data[2]
    if month_saved > 0:
        rate_usd, rate_tok, basis = month_saved / days_this_month, month_tok / days_this_month, "30-day avg"
    elif weekly_saved > 0:
        rate_usd, rate_tok, basis = weekly_saved / 7, weekly_tok / 7, "7-day avg"
    elif today_saved > 0:
        rate_usd, rate_tok, basis = today_saved, today_tok, "today"
    else:
        rate_usd = 0.0
    if rate_usd > 0:
        lines.append("")
        lines.append(
            f"  📈 Projection: ~${rate_usd * 365:.0f}/year"
            f" · ~{_fmt_tok(int(rate_tok * 365))} tok/year"
            f"  (based on {basis})"
        )
    return lines




def _query_session_complexity_breakdown(session_start: float) -> dict:
    """Query usage data grouped by task complexity.
    
    Returns {complexity: [(short_model, count, cost, provider), ...]}
    """
    if not os.path.exists(DB_PATH):
        return {}
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT complexity, model, COUNT(*) as cnt, 
                   COALESCE(SUM(cost_usd), 0) as total_cost,
                   provider
            FROM usage
            WHERE timestamp >= ? AND success = 1
            GROUP BY complexity, model
            ORDER BY complexity, cnt DESC
            """,
            (_session_start_iso(session_start),),
        ).fetchall()
        conn.close()
        
        by_complexity = {}
        for r in rows:
            complexity = r["complexity"] or "moderate"
            model = r["model"]
            cnt = r["cnt"]
            cost = r["total_cost"]
            provider = r["provider"]
            
            if complexity not in by_complexity:
                by_complexity[complexity] = []
            
            short_model = model.split("/")[-1] if "/" in model else model
            if len(short_model) > 20:
                short_model = short_model[:18] + "…"
            
            by_complexity[complexity].append((short_model, cnt, cost, provider))
        
        return by_complexity
    except Exception:
        return {}


def _format_complexity_breakdown(session_start: float) -> list[str]:
    """Format session breakdown by task complexity."""
    complexity_data = _query_session_complexity_breakdown(session_start)
    
    if not complexity_data:
        return []
    
    lines = ["  Model selection by task complexity (this session)", ""]
    
    total_calls = sum(
        cnt for models in complexity_data.values() 
        for _, cnt, _, _ in models
    )
    free_calls = 0
    total_cost = 0.0
    
    # Process in order: simple, moderate, complex
    for complexity in ["simple", "moderate", "complex"]:
        if complexity not in complexity_data:
            continue
        
        models_list = complexity_data[complexity]
        cnt_sum = sum(cnt for _, cnt, _, _ in models_list)
        cost_sum = sum(cost for _, _, cost, _ in models_list)
        total_cost += cost_sum
        
        # Format model list
        model_str_parts = []
        for model, cnt, cost, provider in models_list:
            if provider in ("ollama", "codex"):
                free_calls += cnt
            model_str_parts.append(f"{model} ({cnt}×)")
        
        model_str = " · ".join(model_str_parts)
        cost_tag = f"  [${ cost_sum:.4f}]" if cost_sum > 0 else "  [free]"
        
        lines.append(
            f"  {complexity:<10} {cnt_sum:>2}×    {model_str:<45} {cost_tag}"
        )
    
    # Efficiency insight
    if total_calls > 0:
        free_pct = round(free_calls / total_calls * 100)
        lines.append("")
        lines.append(
            f"  💡 Insight: {free_pct}% free models · "
            f"avg cost ~${(total_cost/total_calls if total_calls else 0):.4f}/call"
        )
    
    return lines

def _format(tools: dict[str, dict], cc_rows: list[dict], free_rows: list[dict],
            paid_rows: list[dict],
            start: dict | None, current: dict | None, is_live: bool,
            cumulative: list[tuple[str, int, int, int, float]] | None = None,
            session_start: float | None = None) -> str:
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

    if session_start is not None:
        complexity_lines = _format_complexity_breakdown(session_start)
        if complexity_lines:
            lines.append("")
            lines += complexity_lines

    if cumulative:
        cum_lines = _format_cumulative_section(cumulative)
        if cum_lines:
            lines.append("")
            lines += cum_lines


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

def _read_session_spend() -> dict | None:
    """Read the real-time session spend file if it exists."""
    try:
        with open(SESSION_SPEND_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None



def _get_session_routing_analysis(session_start: float) -> str:
    """Get routing analysis summary for the session using model_tracking.
    
    Returns formatted string with routing statistics, or empty string if no data.
    Also logs routing patterns to chronicle for future reference.
    """
    try:
        import sys
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
        from llm_router.model_tracking import (
            get_session_routing_summary,
            format_session_summary,
            log_routing_patterns_to_chronicle,
        )
        summary = get_session_routing_summary(since_timestamp=session_start)
        if summary.get("total_decisions", 0) > 0:
            # Log routing patterns to chronicle for architectural insights
            log_routing_patterns_to_chronicle(summary)
            return format_session_summary(summary)
    except Exception:
        pass  # Graceful failure — never break session-end
    return ""


def _build_and_save_learned_profile() -> None:
    """Build learned routing profile from corrections and save to disk.

    This is called at session-end to update ~/.llm-router/learned_routes.json
    with any new routing patterns learned from user corrections (llm_reroute).
    """
    try:
        # Import here to avoid dependency issues in hook context
        import sys
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

        from llm_router.memory.profiles import (
            build_learned_profile,
            save_learned_profile,
        )

        profile = build_learned_profile()
        if profile:
            save_learned_profile(profile)
    except Exception:
        pass  # Graceful failure — never break session-end


def main() -> None:
    try:
        json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        pass

    session_start               = _read_session_start()
    paid_rows, cc_rows, free_rows = _query_session_data(session_start)
    tools                       = _aggregate(paid_rows) if paid_rows else {}
    start, current, is_live     = _get_cc_usage()
    _sync_import_savings_log()          # flush JSONL before cumulative query
    cumulative                  = _query_cumulative_savings()
    _build_and_save_learned_profile()   # v6.1: build profile from corrections
    routing_analysis            = _get_session_routing_analysis(session_start)

    has_cumulative = any(calls > 0 for _, calls, *_ in cumulative)

    if not tools and not cc_rows and not current and not free_rows and not has_cumulative and not routing_analysis:
        sys.exit(0)

    summary = _format(tools, cc_rows, free_rows, paid_rows, start, current, is_live, cumulative, session_start)
    
    # Append session routing analysis if available
    if routing_analysis:
        summary = summary.rstrip("─" * WIDTH) + "\n" + routing_analysis + "─" * WIDTH

    # Append session spend one-liner if available (v4.0)
    spend = _read_session_spend()
    if spend and spend.get("call_count", 0) > 0:
        total = spend.get("total_usd", 0.0)
        calls = spend.get("call_count", 0)
        top   = spend.get("top_model", "")
        top_short = top.split("/", 1)[-1] if top and "/" in top else top
        spend_line = f"  💰 Session API spend: ${total:.4f} across {calls} call(s)"
        if top_short:
            spend_line += f" · top model: {top_short}"
        if spend.get("anomaly_flag"):
            spend_line = "  ⚠️  ANOMALY DETECTED: " + spend_line.lstrip()
        summary = summary.rstrip("─" * WIDTH) + "\n" + spend_line + "\n" + "─" * WIDTH

    # Retrospective output removed per user preference

    # Append mid-session trends if any snapshots exist
    try:
        from llm_router.monitoring.periodic import load_session_snapshots, analyze_session_trends, format_trend_summary
        snapshots = load_session_snapshots()
        if len(snapshots) > 1:
            trends = analyze_session_trends(snapshots)
            if trends.get("snapshot_count", 0) > 0:
                trend_output = format_trend_summary(trends)
                if trend_output and "No snapshots" not in trend_output:
                    summary = summary.rstrip("─" * WIDTH) + "\n【TRENDS】\n" + trend_output + "\n" + "─" * WIDTH
    except Exception:
        pass  # Graceful failure — never break session-end

    # Check for service configuration changes (periodic scan)
    try:
        from llm_router.auto_profile import should_rescan, rescan_and_update
        if should_rescan():
            updated, changes = rescan_and_update()
            if updated and changes:
                changes_str = ", ".join(changes)
                config_note = f"\n  🔄 Profile updated: {changes_str}"
                summary = summary.rstrip("─" * WIDTH) + config_note + "\n" + "─" * WIDTH
    except Exception:
        pass  # Graceful failure — never break session-end

    # Check for model evaluation (7-day TTL — benchmark available models)
    try:
        import asyncio
        from llm_router.model_evaluator import EVAL_CACHE_PATH, EVAL_TTL_SECONDS
        
        should_eval = (
            not EVAL_CACHE_PATH.exists() or 
            (time.time() - EVAL_CACHE_PATH.stat().st_mtime) > EVAL_TTL_SECONDS
        )
        
        if should_eval:
            from llm_router.model_evaluator import evaluate_available_models
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(evaluate_available_models(task_types=["reasoning"]))
                loop.close()
                eval_note = "\n  📊 Model benchmarks updated (next: 7 days)"
                summary = summary.rstrip("─" * WIDTH) + eval_note + "\n" + "─" * WIDTH
            except Exception:
                pass  # Don't fail session if eval fails
    except Exception:
        pass  # Graceful failure

    # ── Add quota timeline for session-end reporting ──────────────────────────────
    # Shows per-prompt Claude quota pressure for audit and visibility.
    try:
        session_id = None
        try:
            with open(SESSION_ID_FILE) as f:
                session_id = f.read().strip()
        except Exception:
            pass
        
        if session_id:
            quota_timeline = _render_quota_timeline(session_id, DB_PATH)
            if quota_timeline:
                summary = summary.rstrip("─" * WIDTH) + quota_timeline + "\n" + "─" * WIDTH
    except Exception:
        pass  # Graceful failure — never break session-end

    print(json.dumps({"systemMessage": summary}))

    # Update the session-start snapshot AFTER the delta has been reported,
    # so the NEXT session starts from today's end-of-session baseline.
    if current and is_live:
        try:
            with open(SESSION_CC_SNAP_FILE, "w") as f:
                json.dump(current, f)
        except OSError:
            pass


if __name__ == "__main__":
    main()
