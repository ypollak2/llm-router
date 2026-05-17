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
WIDTH = 50

# Model names that indicate test/mock data — never show in production reports.
_TEST_MODEL_PATTERNS = {"mock-model", "test-model", "fake-model", "mock", "test"}

# Known valid model prefixes from configured providers.
_KNOWN_MODEL_PREFIXES = {
    "gpt-", "o1", "o3", "o4", "chatgpt-",       # OpenAI
    "claude-", "claude",                           # Anthropic
    "gemini-", "gemma", "gemini",                  # Google
    "llama", "mistral", "mixtral", "qwen",         # Open-source
    "deepseek", "codex", "perplexity",             # Other providers
    "command", "cohere",                           # Cohere
    "phi-", "phi",                                 # Microsoft
}


def _is_test_model(model: str) -> bool:
    """Return True if model name looks like test/mock data."""
    if not model:
        return True
    low = model.lower().strip()
    return low in _TEST_MODEL_PATTERNS or low.startswith("test/") or low.startswith("mock/")


def _is_known_model(model: str) -> bool:
    """Return True if model name matches a known provider pattern."""
    if not model or model == "?":
        return False
    low = model.lower().strip()
    # Check against known prefixes
    for prefix in _KNOWN_MODEL_PREFIXES:
        if low.startswith(prefix):
            return True
    # Ollama models often have format name:tag
    if ":" in low:
        return True
    return False


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
        # Exclude rows with test/mock model names at the data level
        clean = [r for r in all_rows if not _is_test_model(r.get("model", ""))]
        paid  = [r for r in clean
                 if r.get("provider") not in _FREE_PROVIDERS | {"subscription"}]
        cc    = [r for r in clean if r.get("provider") == "subscription"]
        free  = [r for r in clean if r.get("provider") in _FREE_PROVIDERS]
        return paid, cc, free
    except Exception:
        return [], [], []


_PERIODS = [
    ("today",     "date(timestamp, 'localtime') = date('now', 'localtime')"),
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
                float(r.get("estimated_saved", 0.0)),
                float(r.get("external_cost", 0.0)),
                r.get("model", "unknown"),
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
        # Skip test/mock model rows entirely — they should never be in production
        # data, but if they leak through, exclude from user-facing reports.
        if _is_test_model(model):
            continue
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


def _smart_bar(pct: float, width: int = 16) -> str:
    """Color-coded progress bar: green < 30%, yellow < 60%, orange < 80%, red >= 80%."""
    filled = max(0, min(width, round(pct / 100 * width)))
    if pct < 30:
        color = _C_GREEN
    elif pct < 60:
        color = _C_YELLOW
    elif pct < 80:
        color = _C_ORANGE
    else:
        color = _C_RED
    return color + "━" * filled + _RESET + _C_DARK + "─" * (width - filled) + _RESET


def _cc_row(label: str, start_pct: float | None, end_pct: float) -> str:
    """Format one CC subscription row with color-coded bar."""
    bar = _smart_bar(end_pct, width=16)
    pct_str = f"{_C_WHITE}{end_pct:>3.0f}%{_RESET}"
    if start_pct is not None:
        delta = end_pct - start_pct
        if abs(delta) < 0.01:
            delta_str = f"{_C_GRAY}no change{_RESET}"
        else:
            sign = "+" if delta >= 0 else ""
            if abs(delta) < 0.1:
                fmt = f"{sign}{delta:.2f}pp"
            else:
                fmt = f"{sign}{delta:.1f}pp"
            delta_color = _C_ORANGE if abs(delta) > 5 else _C_GRAY
            delta_str = f"{delta_color}{fmt}{_RESET}"
        return f"    {_C_GRAY}{label:<12}{_RESET} {bar}  {pct_str}  {delta_str}"
    return f"    {_C_GRAY}{label:<12}{_RESET} {bar}  {pct_str}"


def _format_cc_section(start: dict | None, current: dict, is_live: bool) -> list[str]:
    src = f"{_DIM}live{_RESET}" if is_live else f"{_DIM}cached{_RESET}"
    lines = [f"  {_BOLD}Claude Subscription{_RESET}  {src}", ""]

    s_end = current.get("session_pct", 0.0)
    w_end = current.get("weekly_pct",  0.0)
    n_end = current.get("sonnet_pct",  0.0)

    s_start = start.get("session_pct") if start else None
    w_start = start.get("weekly_pct")  if start else None
    n_start = start.get("sonnet_pct")  if start else None

    lines.append(_cc_row("5h session",  s_start, s_end))
    lines.append(_cc_row("weekly",      w_start, w_end))
    if n_end > 0 or (n_start is not None and n_start > 0):
        lines.append(_cc_row("sonnet",  n_start, n_end))

    return lines


def _format_cc_model_section(cc_rows: list[dict]) -> list[str]:
    """Format per-model CC call counts."""
    models: dict[str, dict] = {}
    for r in cc_rows:
        model = r.get("model", "?")
        if _is_test_model(model):
            continue
        task  = r.get("task_type", "?")
        if model not in models:
            models[model] = {"count": 0, "tasks": {}}
        models[model]["count"] += 1
        models[model]["tasks"][task] = models[model]["tasks"].get(task, 0) + 1

    total = sum(m["count"] for m in models.values())
    lines = [f"    {_C_WHITE}{total}{_RESET} calls  {_C_GRAY}(subscription, $0.00){_RESET}"]
    for model, d in sorted(models.items(), key=lambda x: -x[1]["count"]):
        short = model.split("/", 1)[-1] if "/" in model else model
        if len(short) > 30:
            short = short[:28] + "…"
        top_task = max(d["tasks"], key=d["tasks"].get) if d["tasks"] else "?"
        lines.append(
            f"    {_C_GRAY}{top_task:<12}{_RESET}  {d['count']:>3}×  {short:<32}  {_C_DARK}sub{_RESET}"
        )
    return lines


def _format_routing_section(tools: dict[str, dict]) -> list[str]:
    total_calls = sum(t["count"] for t in tools.values())
    total_in    = sum(t["in"]    for t in tools.values())
    total_out   = sum(t["out"]   for t in tools.values())
    total_cost  = sum(t["cost"]  for t in tools.values())
    total_base  = _sonnet_baseline(total_in, total_out)
    total_saved = max(0.0, total_base - total_cost)
    savings_pct = round(total_saved / total_base * 100) if total_base > 0 else 0

    pct_color = _C_GREEN if savings_pct >= 80 else (_C_YELLOW if savings_pct >= 50 else _C_ORANGE)
    lines = [
        f"    {_C_WHITE}{total_calls}{_RESET} calls  "
        f"${total_cost:.4f} actual  "
        f"${total_base:.4f} baseline  "
        f"{pct_color}{savings_pct}% saved{_RESET}",
    ]
    for tool, d in sorted(tools.items(), key=lambda x: -x[1]["count"]):
        clean_models = {m: c for m, c in d["models"].items() if not _is_test_model(m)}
        if not clean_models:
            continue
        top_model   = max(clean_models, key=clean_models.get)
        model_short = top_model.split("/", 1)[-1] if "/" in top_model else top_model
        if len(model_short) > 22:
            model_short = model_short[:20] + "…"
        cost_color = _C_GREEN if d["cost"] == 0 else _C_GRAY
        lines.append(
            f"    {_C_GRAY}{tool:<12}{_RESET}  {d['count']:>3}×  "
            f"{model_short:<24}  {cost_color}${d['cost']:.4f}{_RESET}"
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
        est_tag  = f" {_C_DARK}~est{_RESET}" if est else ""
        in_k  = f"{in_t  // 1000}k" if in_t  >= 1000 else str(in_t)
        out_k = f"{out_t // 1000}k" if out_t >= 1000 else str(out_t)
        body.append(
            f"    {_C_GRAY}{provider:<10}{_RESET}  {d['calls']:>3}×  "
            f"{in_k}↑ {out_k}↓{est_tag}  {_C_GREEN}${saved:.4f}{_RESET}"
        )

    # Label based on actual providers present
    providers_present = list(by_provider.keys())
    if providers_present == ["ollama"]:
        label = "Local (Ollama)"
    elif providers_present == ["codex"]:
        label = "Prepaid (Codex)"
    else:
        label = "Local / prepaid"
    saved_color = _C_GREEN if total_saved > 0 else _C_GRAY
    lines = [
        f"    {_C_WHITE}{total_calls}{_RESET} calls  ·  "
        f"{saved_color}${total_saved:.4f} saved{_RESET} vs Sonnet  {_C_DARK}{label}{_RESET}"
    ]
    lines += body
    return lines


def _fmt_tok(n: int) -> str:
    """Human-readable token count: 1234 → 1.2k, 1234567 → 1.2M."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _query_router_efficiency() -> dict:
    """Query routing_decisions: return {total, on_target, efficiency_pct}."""
    if not os.path.exists(DB_PATH):
        return {}
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.execute("""
            SELECT
                COUNT(*) as total,
                COUNT(CASE WHEN final_model = recommended_model THEN 1 END) as on_target
            FROM routing_decisions
            WHERE date(timestamp, 'localtime') = date('now', 'localtime')
        """)
        row = cursor.fetchone()
        conn.close()
        if not row or row[0] == 0:
            return {}
        total, on_target = row
        efficiency_pct = (on_target / total) * 100 if total > 0 else 0.0
        return {"total": total, "on_target": on_target, "efficiency_pct": efficiency_pct}
    except Exception:
        return {}


def _query_classifier_overhead() -> dict:
    """Query classifier_latency_ms: return {count, avg_ms, min_ms, max_ms}."""
    if not os.path.exists(DB_PATH):
        return {}
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.execute("""
            SELECT
                COUNT(*) as count,
                AVG(classifier_latency_ms) as avg_ms,
                MIN(classifier_latency_ms) as min_ms,
                MAX(classifier_latency_ms) as max_ms
            FROM routing_decisions
            WHERE date(timestamp, 'localtime') = date('now', 'localtime')
                AND classifier_latency_ms IS NOT NULL
        """)
        row = cursor.fetchone()
        conn.close()
        if not row or row[0] == 0:
            return {}
        count, avg_ms, min_ms, max_ms = row
        return {"count": count, "avg_ms": float(avg_ms) if avg_ms else 0.0,
                "min_ms": float(min_ms) if min_ms else 0.0,
                "max_ms": float(max_ms) if max_ms else 0.0}
    except Exception:
        return {}


# ── ANSI color codes ──────────────────────────────────────────────────────────
# Uses standard 16-color ANSI (bold variants) for universal light/dark support.
# These colors are readable on both white and black terminal backgrounds because
# they use the terminal's own color scheme rather than fixed 256-color values.
_GREEN = "\033[32m"
_CYAN = "\033[36m"
_YELLOW = "\033[33m"
_MAGENTA = "\033[35m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RESET = "\033[0m"

# Semantic color palette — standard ANSI that adapts to terminal theme.
# Bright variants (90-97) are visible on dark backgrounds.
# Normal variants (30-37) are visible on light backgrounds.
# Using bold+normal gives best cross-theme visibility.
_C_CYAN    = "\033[36m"       # Teal — works on both
_C_GREEN   = "\033[32m"       # Green — works on both
_C_YELLOW  = "\033[33m"       # Yellow/brown — works on both
_C_ORANGE  = "\033[33;1m"     # Bold yellow = orange on most terminals
_C_RED     = "\033[31m"       # Red — works on both
_C_MAGENTA = "\033[35m"       # Magenta — works on both
_C_WHITE   = "\033[1m"        # Bold (inherits fg) — always visible
_C_GRAY    = "\033[2m"        # Dim — adapts to terminal fg
_C_DARK    = "\033[2m"        # Dim — same as gray, visible on both

# ── Routing method symbols ────────────────────────────────────────────────────
_METHOD_SYMBOLS = {
    "heuristic": "⚡",
    "heuristic-weak": "⚡",
    "build-fast-path": "🔨",
    "content-generation-fast-path": "📝",
    "ollama": "🧠",
    "llm": "🧠",
    "context-inherit": "🔗",
    "code-context-inherit": "🔗",
    "override": "📌",
    "fallback": "🔄",
    "unknown": "❓",
}


def _query_routing_logic(session_start: float | None = None) -> list[dict]:
    """Query routing decision breakdown by classification method."""
    if not os.path.exists(DB_PATH):
        return []
    try:
        import json as _json
        tracking_path = os.path.join(STATE_DIR, "model_tracking.jsonl")
        if not os.path.exists(tracking_path):
            return []

        methods: dict[str, dict] = {}
        cutoff = session_start or 0

        with open(tracking_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = _json.loads(line)
                    ts = r.get("timestamp", 0)
                    if ts < cutoff:
                        continue
                    method = r.get("classification_method", "unknown")
                    if method not in methods:
                        methods[method] = {"hits": 0, "total_confidence": 0.0}
                    methods[method]["hits"] += 1
                    methods[method]["total_confidence"] += r.get("classification_confidence", 0.0)
                except Exception:
                    continue

        result = []
        for method, d in sorted(methods.items(), key=lambda x: -x[1]["hits"]):
            avg_conf = d["total_confidence"] / d["hits"] if d["hits"] > 0 else 0.0
            symbol = _METHOD_SYMBOLS.get(method, "❓")
            # Group display name
            if method in ("heuristic", "heuristic-weak"):
                reason = "Cached patterns / Static rules"
            elif method in ("build-fast-path", "content-generation-fast-path"):
                reason = "Fast-path pattern match"
            elif method in ("ollama", "llm"):
                reason = "LLM complexity classification"
            elif method in ("context-inherit", "code-context-inherit"):
                reason = "Session context inherited"
            elif method == "override":
                reason = "Manual override / policy"
            elif method == "fallback":
                reason = "No classifier matched"
            else:
                reason = "Unknown"
            result.append({
                "method": method, "symbol": symbol, "hits": d["hits"],
                "avg_confidence": avg_conf, "reason": reason,
            })
        return result
    except Exception:
        return []


def _query_cache_hit_stats() -> dict:
    """Query semantic_cache: return {total_requests, cache_hits, hit_rate_pct, estimated_saved_usd}."""
    if not os.path.exists(DB_PATH):
        return {}
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.execute("""
            SELECT
                COUNT(*) as total_requests,
                SUM(CASE WHEN cache_hit = 1 THEN 1 ELSE 0 END) as cache_hits,
                ROUND(SUM(CASE WHEN cache_hit = 1 THEN tokens_saved ELSE 0 END) * 0.003 / 1000, 4) as estimated_saved
            FROM semantic_cache
            WHERE date(timestamp, 'localtime') = date('now', 'localtime')
        """)
        row = cursor.fetchone()
        conn.close()
        if not row or row[0] == 0:
            return {}
        total_requests, cache_hits, estimated_saved = row
        cache_hits = cache_hits or 0
        estimated_saved = float(estimated_saved) if estimated_saved else 0.0
        hit_rate_pct = (cache_hits / total_requests) * 100 if total_requests > 0 else 0.0
        return {"total_requests": total_requests, "cache_hits": cache_hits,
                "hit_rate_pct": hit_rate_pct, "estimated_saved_usd": estimated_saved}
    except Exception:
        return {}


def _query_savings_by_task_type() -> list[dict]:
    """Query savings_stats and usage: return list of {task_type, calls, saved} sorted by saved DESC."""
    if not os.path.exists(DB_PATH):
        return []
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.execute("""
            SELECT
                task_type,
                COUNT(*) as calls,
                SUM(estimated_claude_cost_saved) as saved
            FROM savings_stats
            WHERE date(timestamp, 'localtime') = date('now', 'localtime')
            GROUP BY task_type
            ORDER BY saved DESC
        """)
        rows = cursor.fetchall()
        conn.close()
        result = []
        for task_type, calls, saved in rows:
            result.append({"task_type": task_type or "unknown", "calls": calls, "saved": float(saved) if saved else 0.0})
        return result
    except Exception:
        return []


def _query_daily_14d() -> list[tuple[str, int, int, float]]:
    """Return last 14 days of daily usage: [(date_label, calls, tokens, saved), ...]."""
    if not os.path.exists(DB_PATH):
        return []
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute("""
            SELECT date(timestamp, 'localtime') as day,
                   COUNT(*) as calls,
                   COALESCE(SUM(input_tokens),0) as in_tok,
                   COALESCE(SUM(output_tokens),0) as out_tok,
                   COALESCE(SUM(cost_usd),0) as cost,
                   provider
            FROM usage
            WHERE success=1
              AND timestamp >= datetime('now', '-14 days')
            GROUP BY day, provider
            ORDER BY day
        """).fetchall()
        conn.close()

        # Aggregate per day with savings calculation
        from collections import OrderedDict
        daily: OrderedDict[str, dict] = OrderedDict()
        for day, calls, in_tok, out_tok, cost, provider in rows:
            if day not in daily:
                daily[day] = {"calls": 0, "tokens": 0, "saved": 0.0}
            daily[day]["calls"] += calls
            daily[day]["tokens"] += in_tok + out_tok
            baseline = _sonnet_baseline(in_tok, out_tok)
            if provider in _FREE_PROVIDERS:
                daily[day]["saved"] += baseline
            elif provider != "subscription":
                daily[day]["saved"] += max(0.0, baseline - cost)

        return [
            (day, d["calls"], d["tokens"], d["saved"])
            for day, d in daily.items()
        ]
    except Exception:
        return []






def _format_routing_logic(session_start: float | None) -> list[str]:
    """Format routing decision method breakdown."""
    data = _query_routing_logic(session_start)
    if not data:
        return []

    total_hits = sum(d["hits"] for d in data)
    if total_hits == 0:
        return []

    zero_cost = 0

    for d in data:
        method = d["method"]
        if method in ("heuristic", "heuristic-weak", "build-fast-path",
                       "content-generation-fast-path", "context-inherit",
                       "code-context-inherit"):
            zero_cost += d["hits"]
        elif method not in ("ollama", "llm"):
            zero_cost += d["hits"]

    zero_pct = round(zero_cost / total_hits * 100) if total_hits > 0 else 0
    pct_color = _C_GREEN if zero_pct >= 80 else (_C_YELLOW if zero_pct >= 50 else _C_ORANGE)
    lines = [
        f"  {_BOLD}Routing{_RESET}  {_C_GREEN}●{_RESET} "
        f"{_C_WHITE}{total_hits}{_RESET} decisions · "
        f"{pct_color}{zero_pct}% zero-cost{_RESET}"
    ]
    # Find max method name length for alignment
    max_name = max(len(d["method"]) for d in data)
    for d in data:
        pct = (d["hits"] / total_hits) * 100
        symbol = d.get("symbol", "❓")
        name = d["method"]
        lines.append(
            f"    {symbol} {_C_GRAY}{name:<{max_name}}{_RESET}"
            f"  {_C_WHITE}{d['hits']:>3}{_RESET}"
            f"  {_C_DARK}{pct:>3.0f}%{_RESET}"
        )
    return lines

def _sparkline(values: list[float]) -> str:
    """Render a sparkline using Unicode block characters."""
    if not values:
        return ""
    chars = " ▁▂▃▄▅▆▇█"
    max_val = max(values) if max(values) > 0 else 1
    return "".join(
        chars[min(len(chars) - 1, round(v / max_val * (len(chars) - 1)))]
        for v in values
    )


def _format_cumulative_section(periods: list[tuple[str, int, int, int, float]]) -> list[str]:
    """Format cumulative savings with sparkline and rich colors."""
    if not periods or all(p[1] == 0 for p in periods):
        return []

    period_map = {label: (calls, ti, to, saved) for label, calls, ti, to, saved in periods}
    all_time = period_map.get("all time", (0, 0, 0, 0.0))
    today_d = period_map.get("today", (0, 0, 0, 0.0))
    month_d = period_map.get("this month", (0, 0, 0, 0.0))

    lifetime_saved = all_time[3]
    saved_hero = f"${lifetime_saved:.2f}" if lifetime_saved >= 1.0 else f"${lifetime_saved:.4f}"
    today_s = f"${today_d[3]:.2f}" if today_d[3] >= 1.0 else f"${today_d[3]:.4f}"

    lines: list[str] = [
        f"  {_BOLD}Savings{_RESET}",
        "",
        f"    {_C_GREEN}{_BOLD}{saved_hero}{_RESET}  {_C_GRAY}lifetime{_RESET}"
        f"    {_C_WHITE}{today_s}{_RESET}  {_C_GRAY}today{_RESET}",
        "",
    ]

    # Period grid — vertical for readability
    for label, calls, _ti, _to, saved in periods:
        s = f"${saved:.2f}" if saved >= 1.0 else f"${saved:.4f}"
        call_str = f"{calls:,}" if calls >= 1000 else str(calls)
        short_label = {"today": "today", "this week": "week", "this month": "month", "all time": "all"}.get(label, label)
        lines.append(
            f"    {_C_GRAY}{short_label:<6}{_RESET}"
            f"  {_C_WHITE}{s:>8}{_RESET}"
            f"  {_C_DARK}{call_str:>6}{_RESET}"
        )

    # Yearly projection
    from datetime import datetime as _dt
    days_this_month = max(1, _dt.now().day)
    month_saved = month_d[3]
    weekly_data = period_map.get("this week", (0, 0, 0, 0.0))
    weekly_saved = weekly_data[3]
    today_saved = today_d[3]
    month_tok = month_d[1] + month_d[2]
    weekly_tok = weekly_data[1] + weekly_data[2]
    today_tok = today_d[1] + today_d[2]
    rate_usd = 0.0
    if month_saved > 0:
        rate_usd, rate_tok, basis = month_saved / days_this_month, month_tok / days_this_month, "30-day avg"
    elif weekly_saved > 0:
        rate_usd, rate_tok, basis = weekly_saved / 7, weekly_tok / 7, "7-day avg"
    elif today_saved > 0:
        rate_usd, rate_tok, basis = today_saved, today_tok, "today"
    if rate_usd > 0:
        lines.append(
            f"    {_C_DARK}≈ ${rate_usd * 365:.0f}/yr · {_fmt_tok(int(rate_tok * 365))} tok/yr{_RESET}  {_DIM}({basis}){_RESET}"
        )

    # 14-day sparkline
    daily_14d = _query_daily_14d()
    if daily_14d:
        total_calls = sum(d[1] for d in daily_14d)
        total_tokens = sum(d[2] for d in daily_14d)
        total_14d_saved = sum(d[3] for d in daily_14d)
        avg_calls = total_calls // max(len(daily_14d), 1)
        spark_values = [float(d[1]) for d in daily_14d]
        spark = _sparkline(spark_values)
        lines.append("")
        lines.append(f"  {_BOLD}14 Days{_RESET}  {_C_CYAN}{spark}{_RESET}")
        saved_14 = f"${total_14d_saved:.2f}" if total_14d_saved >= 1.0 else f"${total_14d_saved:.4f}"
        lines.append(
            f"    {_C_WHITE}{total_calls}{_RESET} calls · "
            f"{_C_WHITE}{_fmt_tok(total_tokens)}{_RESET} tok · "
            f"{_C_GREEN}{saved_14}{_RESET} saved · "
            f"avg {_C_WHITE}{avg_calls}{_RESET}/day"
        )

    # Quality metrics
    quality_parts: list[str] = []

    efficiency = _query_router_efficiency()
    if efficiency:
        fallbacks = efficiency["total"] - efficiency["on_target"]
        if fallbacks == 0:
            quality_parts.append(f"{_C_GREEN}0{_RESET} fallbacks ({efficiency['total']})")
        else:
            quality_parts.append(f"{_C_ORANGE}{fallbacks}{_RESET}/{efficiency['total']} fallbacks")

    overhead = _query_classifier_overhead()
    if overhead and overhead['count'] > 0:
        ms = overhead['avg_ms']
        ms_color = _C_GREEN if ms < 50 else (_C_YELLOW if ms < 200 else _C_ORANGE)
        quality_parts.append(f"{ms_color}{ms:.0f}ms{_RESET} avg routing")

    cache_stats = _query_cache_hit_stats()
    if cache_stats:
        hr = cache_stats['hit_rate_pct']
        hr_color = _C_GREEN if hr >= 50 else _C_GRAY
        quality_parts.append(f"{hr_color}{hr:.0f}%{_RESET} cache hit")

    if quality_parts:
        lines.append(f"    {_C_DARK}{' · '.join(quality_parts)}{_RESET}")

    return lines




def _query_session_complexity_breakdown(session_start: float) -> tuple[dict, int]:
    """Query usage data grouped by task complexity.

    Returns ({complexity: [(short_model, count, cost, provider), ...]}, filtered_test_count)
    """
    if not os.path.exists(DB_PATH):
        return {}, 0
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
        filtered_test_calls = 0
        for r in rows:
            complexity = r["complexity"] or "moderate"
            model = r["model"] or "unknown"
            cnt = r["cnt"]
            cost = r["total_cost"]
            provider = r["provider"]

            # Filter out test/mock models from production reports
            if _is_test_model(model):
                filtered_test_calls += cnt
                continue

            if complexity not in by_complexity:
                by_complexity[complexity] = []

            short_model = model.split("/")[-1] if "/" in model else model
            if len(short_model) > 20:
                short_model = short_model[:18] + "…"

            by_complexity[complexity].append((short_model, cnt, cost, provider))

        return by_complexity, filtered_test_calls
    except Exception:
        return {}, 0


def _format_complexity_breakdown(session_start: float) -> list[str]:
    """Format session breakdown by task complexity."""
    complexity_data, filtered_test_calls = _query_session_complexity_breakdown(session_start)

    if not complexity_data:
        return []
    
    _COMPLEXITY_COLORS = {"simple": _C_GREEN, "moderate": _C_YELLOW, "complex": _C_ORANGE}
    lines = [f"    {_C_GRAY}Model selection by complexity{_RESET}"]

    total_calls = sum(
        cnt for models in complexity_data.values()
        for _, cnt, _, _ in models
    )
    free_calls = 0
    total_cost = 0.0

    for complexity in ["simple", "moderate", "complex"]:
        if complexity not in complexity_data:
            continue

        models_list = complexity_data[complexity]
        cnt_sum = sum(cnt for _, cnt, _, _ in models_list)
        cost_sum = sum(cost for _, _, cost, _ in models_list)
        total_cost += cost_sum

        model_str_parts = []
        for model, cnt, cost, provider in models_list:
            if provider in ("ollama", "codex"):
                free_calls += cnt
            model_str_parts.append(f"{model} ({cnt}×)")

        model_str = " · ".join(model_str_parts)
        c_color = _COMPLEXITY_COLORS.get(complexity, _C_GRAY)
        cost_tag = f"{_C_GRAY}${cost_sum:.4f}{_RESET}" if cost_sum > 0 else f"{_C_GREEN}free{_RESET}"

        lines.append(
            f"    {c_color}{complexity:<10}{_RESET} {cnt_sum:>2}×  {model_str}  {cost_tag}"
        )

    if total_calls > 0:
        paid_calls = total_calls - free_calls
        avg_cost = total_cost / total_calls if total_calls else 0
        lines.append(
            f"    {_C_WHITE}{total_calls}{_RESET} routed = "
            f"{_C_GREEN}{free_calls}{_RESET} local + "
            f"{paid_calls} external"
            + (f" + {_C_DARK}{filtered_test_calls} excluded{_RESET}" if filtered_test_calls > 0 else "")
            + f"  {_C_DARK}·{_RESET} avg ${avg_cost:.4f}/call"
        )

    return lines

def _format(tools: dict[str, dict], cc_rows: list[dict], free_rows: list[dict],
            paid_rows: list[dict],
            start: dict | None, current: dict | None, is_live: bool,
            cumulative: list[tuple[str, int, int, int, float]] | None = None,
            session_start: float | None = None) -> str:
    div = f"{_C_DARK}{'─' * (WIDTH - 4)}{_RESET}"
    lines = ["", f"  {_C_CYAN}{_BOLD}⚡ LLM Router{_RESET}  {_C_GRAY}session summary{_RESET}", f"  {div}"]

    if current:
        lines.append("")
        lines += _format_cc_section(start, current, is_live)

    if cc_rows:
        lines.append("")
        lines += _format_cc_model_section(cc_rows)

    session_lines: list[str] = []
    if free_rows:
        session_lines += _format_free_section(free_rows, paid_rows)
    if tools:
        if session_lines:
            session_lines.append("")
        session_lines += _format_routing_section(tools)
    if session_start is not None:
        complexity_lines = _format_complexity_breakdown(session_start)
        if complexity_lines:
            if session_lines:
                session_lines.append("")
            session_lines += complexity_lines

    if session_lines:
        lines.append("")
        lines.append(f"  {_BOLD}This Session{_RESET}")
        lines += session_lines

    if session_start is not None:
        routing_lines = _format_routing_logic(session_start)
        if routing_lines:
            lines.append("")
            lines += routing_lines

    if cumulative:
        cum_lines = _format_cumulative_section(cumulative)
        if cum_lines:
            lines.append("")
            lines.append(f"  {_C_DARK}{'─' * (WIDTH - 4)}{_RESET}")
            lines.append("")
            lines += cum_lines

    lines.append("")
    lines.append(f"  {div}")
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


# ── Data collection ────────────────────────────────────────────────────────────

def _collect_report_data(
    session_start: float,
    paid_rows: list[dict],
    cc_rows: list[dict],
    free_rows: list[dict],
    tools: dict[str, dict],
    start: dict | None,
    current: dict | None,
    is_live: bool,
    cumulative: list[tuple[str, int, int, int, float]],
) -> dict:
    """Gather all metrics into a single data dict for the renderer."""
    session_id = None
    try:
        with open(SESSION_ID_FILE) as f:
            session_id = f.read().strip()
    except Exception:
        pass

    return {
        "session_id": session_id,
        "duration_secs": time.time() - session_start,
        "cc_start": start,
        "cc_current": current,
        "cc_is_live": is_live,
        "routing_logic": _query_routing_logic(session_start),
        "cumulative": cumulative,
        "daily_14d": _query_daily_14d(),
        "efficiency": _query_router_efficiency(),
        "overhead": _query_classifier_overhead(),
        "cache_stats": _query_cache_hit_stats(),
        "paid_rows": paid_rows,
        "cc_rows": cc_rows,
        "free_rows": free_rows,
        "tools": tools,
        "complexity_data": _query_session_complexity_breakdown(session_start),
        "savings_by_task": _query_savings_by_task_type(),
    }


# ── Entry point ────────────────────────────────────────────────────────────────

def _read_session_spend() -> dict | None:
    """Read the real-time session spend file if it exists."""
    try:
        with open(SESSION_SPEND_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None





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

    has_cumulative = any(calls > 0 for _, calls, *_ in cumulative)

    if not tools and not cc_rows and not current and not free_rows and not has_cumulative:
        sys.exit(0)

    # Try Cyber-Grid (Rich) renderer; fall back to legacy ANSI
    summary = None
    try:
        from llm_router.hooks.cyber_grid import render_cyber_grid
        report_data = _collect_report_data(
            session_start, paid_rows, cc_rows, free_rows, tools,
            start, current, is_live, cumulative,
        )
        summary = render_cyber_grid(report_data)
    except Exception:
        pass

    if not summary:
        summary = _format(tools, cc_rows, free_rows, paid_rows, start, current, is_live, cumulative, session_start)

    # Append session spend + real savings panel (v8.8.0)
    spend = _read_session_spend()
    if spend and spend.get("call_count", 0) > 0:
        total = spend.get("total_usd", 0.0)
        calls = spend.get("call_count", 0)
        tokens_reclaimed = spend.get("tokens_reclaimed", 0)
        net_savings = spend.get("net_savings_usd", 0.0)
        opus_equiv = spend.get("opus_equivalent_usd", 0.0)
        ext_min = spend.get("extension_minutes", 0.0)
        gate_rate = spend.get("gate_pass_rate", 100.0)
        gates_p = spend.get("gates_passed", 0)
        gates_f = spend.get("gates_failed", 0)

        # Build savings panel
        lines = []
        if opus_equiv > 0:
            pct_saved = (net_savings / opus_equiv * 100) if opus_equiv > 0 else 0
            # Progress bar showing how much was preserved
            bar_len = 20
            filled = int(pct_saved / 100 * bar_len)
            bar = _C_GREEN + "━" * filled + _C_GRAY + "─" * (bar_len - filled) + _RESET
            lines.append(f"  Quota Preserved  {bar} {pct_saved:.0f}%")
            if tokens_reclaimed > 0:
                tok_k = tokens_reclaimed / 1000
                lines.append(f"  {tok_k:.0f}K tokens reclaimed" + (f" · +{ext_min:.0f}min runway" if ext_min >= 1 else ""))
            lines.append(f"  Opus would cost:  ${opus_equiv:.4f}")
            lines.append(f"  Actually spent:   ${total:.4f}")
            lines.append(f"  Net preserved:    {_C_GREEN}${net_savings:.4f}{_RESET}")
        else:
            lines.append(f"  Session spend: ${total:.4f} across {calls} call(s)")

        # Gate quality line
        if gates_p + gates_f > 0:
            lines.append(f"  Quality gates: {gates_p}/{gates_p + gates_f} passed ({gate_rate:.0f}%)")

        if spend.get("anomaly_flag"):
            lines.insert(0, f"  {_C_RED}⚠  ANOMALY: spend rate exceeded threshold{_RESET}")

        spend_block = "\n".join(lines)
        summary = summary.rstrip("  " + "═" * (WIDTH - 2)) + "\n" + spend_block + "\n" + "  " + "═" * (WIDTH - 2)

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
                    summary = summary.rstrip("  " + "═" * (WIDTH - 2)) + "\n【TRENDS】\n" + trend_output + "\n" + "  " + "═" * (WIDTH - 2)
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
                summary = summary.rstrip("  " + "═" * (WIDTH - 2)) + config_note + "\n" + "  " + "═" * (WIDTH - 2)
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
                summary = summary.rstrip("  " + "═" * (WIDTH - 2)) + eval_note + "\n" + "  " + "═" * (WIDTH - 2)
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
                summary = summary.rstrip("  " + "═" * (WIDTH - 2)) + quota_timeline + "\n" + "  " + "═" * (WIDTH - 2)
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
