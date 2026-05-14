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


def _cc_row(label: str, start_pct: float | None, end_pct: float) -> str:
    """Format one CC subscription row.

    start_pct=None means no snapshot available (first session or missing file).
    """
    bar = _bar(end_pct)
    if start_pct is not None:
        delta = end_pct - start_pct
        # When raw delta is non-zero but rounds to 0.0, show higher precision
        # When delta truly is 0.0 (or negligible <0.01), say so clearly
        if abs(delta) < 0.01:
            return f"  {label:<16}  {bar}  {end_pct:>4.1f}%  (no change this session)"
        elif abs(delta) < 0.1:
            # Sub-0.1pp delta: show 2 decimal places to avoid misleading +0.0pp
            sign = "+" if delta >= 0 else ""
            return (
                f"  {label:<16}  {bar}  "
                f"{start_pct:>4.1f}% → {end_pct:>4.1f}%  ({sign}{delta:.2f}pp this session)"
            )
        else:
            sign = "+" if delta >= 0 else ""
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
        if _is_test_model(model):
            continue
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
        f"{total_calls} calls  ·  ${total_cost:.4f} actual  ·  "
        f"${total_base:.4f} baseline  ·  {savings_pct}% saved",
        "",
    ]
    for tool, d in sorted(tools.items(), key=lambda x: -x[1]["count"]):
        # Filter test/mock models from display
        clean_models = {m: c for m, c in d["models"].items() if not _is_test_model(m)}
        if not clean_models:
            continue
        top_model   = max(clean_models, key=clean_models.get)
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

    # Label based on actual providers present
    providers_present = list(by_provider.keys())
    if providers_present == ["ollama"]:
        label = "Local models (Ollama)"
    elif providers_present == ["codex"]:
        label = "Prepaid models (Codex)"
    else:
        label = "Local / prepaid models"
    lines = [f"  {label}  {total_calls} calls  ·  ${total_saved:.4f} saved vs Sonnet", ""]
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
_GREEN = "\033[32m"
_CYAN = "\033[36m"
_YELLOW = "\033[33m"
_MAGENTA = "\033[35m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RESET = "\033[0m"

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


def _render_braille_chart(daily_data: list[tuple[str, int, int, float]]) -> list[str]:
    """Render a Braille-pattern bar chart for 14-day daily usage."""
    if not daily_data:
        return []

    from datetime import datetime as _dt

    _BRAILLE_BARS = [" ", "\u28e0", "\u28f4", "\u28f6", "\u28ff"]

    chart_rows = 6
    levels = chart_rows * 4

    calls_list = [d[1] for d in daily_data]
    max_calls = max(calls_list) if calls_list else 1
    if max_calls == 0:
        max_calls = 1

    heights = []
    for calls in calls_list:
        h = round((calls / max_calls) * levels)
        if calls > 0 and h == 0:
            h = 1
        heights.append(h)

    lines = []
    pipe = "\u2502"

    for row_idx in range(chart_rows):
        row_bottom = (chart_rows - 1 - row_idx) * 4

        if row_idx == 0:
            y_label = f"{_GREEN}{max_calls:>5}{_RESET} "
        elif row_idx == chart_rows // 2:
            y_label = f"{_DIM}{max_calls // 2:>5}{_RESET} "
        else:
            y_label = "      "

        bar_chars = ""
        for h in heights:
            fill = max(0, min(4, h - row_bottom))
            bar_chars += f" {_GREEN}{_BRAILLE_BARS[fill]}{_RESET} "

        lines.append(f"  {y_label}{pipe}{bar_chars}")

    n = len(daily_data)
    corner = "\u2570"
    dash = "\u2500"
    axis = dash * (n * 3 + 1)
    pad = " " * 6
    lines.append(f"  {pad}{corner}{axis}")

    day_labels = ""
    weekday_labels = ""
    for day_str, *_ in daily_data:
        try:
            dt = _dt.strptime(day_str, "%Y-%m-%d")
            day_labels += f" {dt.day:>2d}"
            wk = dt.strftime("%a")[:2]
            weekday_labels += f" {_DIM}{wk}{_RESET}"
        except Exception:
            day_labels += f" {day_str[-2:]}"
            weekday_labels += "   "

    lines.append(f"  {pad} {day_labels}")
    lines.append(f"  {pad} {weekday_labels}")

    total_calls = sum(d[1] for d in daily_data)
    total_tokens = sum(d[2] for d in daily_data)
    total_saved = sum(d[3] for d in daily_data)
    avg_calls = total_calls // max(len(daily_data), 1)
    saved_str = f"${total_saved:.2f}" if total_saved >= 1.0 else f"${total_saved:.4f}"
    dot = "\u00b7"
    lines.append("")
    lines.append(
        f"  {_DIM}Total{_RESET}  {_BOLD}{total_calls}{_RESET} {_DIM}calls{_RESET}  {dot}  "
        f"{_BOLD}{_fmt_tok(total_tokens)}{_RESET} {_DIM}tok{_RESET}  {dot}  "
        f"{_GREEN}{saved_str}{_RESET} {_DIM}saved{_RESET}  {dot}  "
        f"{_DIM}avg{_RESET} {avg_calls}{_DIM}/day{_RESET}"
    )

    return lines


def _box_top(title: str, width: int = 62) -> str:
    # Strip ANSI codes for width calculation
    import re as _re
    plain = _re.sub(r'\033\[[0-9;]*m', '', title)
    inner = width - 4 - len(plain)
    return f"  {_DIM}╭─{_RESET} {_BOLD}{title}{_RESET} {_DIM}{'─' * max(0, inner)}╮{_RESET}"


def _box_mid(text: str, width: int = 62) -> str:
    import re as _re
    plain = _re.sub(r'\033\[[0-9;]*m', '', text)
    padding = width - 4 - len(plain)
    return f"  {_DIM}│{_RESET} {text}{' ' * max(0, padding)} {_DIM}│{_RESET}"


def _box_bot(width: int = 62) -> str:
    return f"  {_DIM}╰{'─' * (width - 2)}╯{_RESET}"



def _format_routing_logic(session_start: float | None) -> list[str]:
    """Format routing decision method breakdown table."""
    data = _query_routing_logic(session_start)
    if not data:
        return []

    total_hits = sum(d["hits"] for d in data)
    if total_hits == 0:
        return []

    lines = []
    lines.append(_box_top(f"{_CYAN}Routing Logic{_RESET}"))
    lines.append(_box_mid(""))
    lines.append(_box_mid(f"  {_DIM}Method                  Hits    Pct   Reason{_RESET}"))
    lines.append(_box_mid(f"  {_DIM}" + "─" * 54 + f"{_RESET}"))

    # Compute zero-cost (heuristic + fast-path + inherit) vs classifier
    zero_cost = 0
    classifier_cost = 0

    for d in data:
        pct = (d["hits"] / total_hits) * 100
        method_display = d["method"][:20]
        symbol = d["symbol"]
        reason = d["reason"][:28]

        # Color code: green for zero-cost, yellow for LLM-based
        if d["method"] in ("heuristic", "heuristic-weak", "build-fast-path",
                           "content-generation-fast-path", "context-inherit",
                           "code-context-inherit"):
            color = _GREEN
            zero_cost += d["hits"]
        elif d["method"] in ("ollama", "llm"):
            color = _YELLOW
            classifier_cost += d["hits"]
        else:
            color = _DIM
            zero_cost += d["hits"]

        lines.append(_box_mid(
            f"  {symbol} {color}{method_display:<20}{_RESET} {d['hits']:>4}  {pct:>4.0f}%   {_DIM}{reason}{_RESET}"
        ))

    lines.append(_box_mid(f"  {_DIM}" + "─" * 54 + f"{_RESET}"))

    zero_pct = round(zero_cost / total_hits * 100) if total_hits > 0 else 0
    lines.append(_box_mid(
        f"  {_GREEN}⚡ Zero-cost decisions: {zero_cost}/{total_hits} ({zero_pct}%){_RESET}"
    ))
    if classifier_cost > 0:
        cls_pct = round(classifier_cost / total_hits * 100)
        lines.append(_box_mid(
            f"  {_YELLOW}🧠 Classifier overhead: {classifier_cost}/{total_hits} ({cls_pct}%){_RESET}"
        ))

    lines.append(_box_mid(""))
    lines.append(_box_bot())
    return lines

def _format_cumulative_section(periods: list[tuple[str, int, int, int, float]]) -> list[str]:
    """Format cumulative savings with box frames, bars, vertical chart, and hero metrics."""
    if not periods or all(p[1] == 0 for p in periods):
        return []

    period_map = {label: (calls, ti, to, saved) for label, calls, ti, to, saved in periods}
    all_time = period_map.get("all time", (0, 0, 0, 0.0))
    today_d = period_map.get("today", (0, 0, 0, 0.0))
    month_d = period_map.get("this month", (0, 0, 0, 0.0))

    lines: list[str] = []

    # ── Hero metrics ─────────────────────────────────────────────
    lifetime_saved = all_time[3]
    lifetime_tok = all_time[1] + all_time[2]
    lifetime_calls = all_time[0]
    saved_hero = f"${lifetime_saved:.2f}" if lifetime_saved >= 1.0 else f"${lifetime_saved:.4f}"

    lines.append(_box_top("Lifetime Savings"))
    lines.append(_box_mid(""))
    lines.append(_box_mid(
        f"  💰 {_GREEN}{_BOLD}{saved_hero}{_RESET} {_DIM}saved{_RESET}    {_fmt_tok(lifetime_tok)} tokens    {lifetime_calls:,} calls"
    ))

    # Delta line: today + month
    today_s = f"${today_d[3]:.2f}" if today_d[3] >= 1.0 else f"${today_d[3]:.4f}"
    month_s = f"${month_d[3]:.2f}" if month_d[3] >= 1.0 else f"${month_d[3]:.4f}"
    lines.append(_box_mid(
        f"     ▲ {today_s} today  ·  ▲ {month_s} this month"
    ))
    lines.append(_box_mid(""))
    lines.append(_box_bot())

    # ── Savings by period ────────────────────────────────────────
    lines.append("")
    lines.append(_box_top("Savings by Period"))
    lines.append(_box_mid(""))

    max_saved = max((p[4] for p in periods), default=1.0)
    if max_saved <= 0:
        max_saved = 1.0
    bar_width = 24

    for label, calls, total_in, total_out, saved in periods:
        if saved >= 1.0:
            saved_str = f"${saved:>6.2f}"
        else:
            saved_str = f"${saved:>6.4f}"
        filled = max(0, min(bar_width, round(saved / max_saved * bar_width)))
        bar = "█" * filled + "░" * (bar_width - filled)
        lines.append(_box_mid(
            f"  {label:<12} {saved_str}  {bar}  {calls:>5,}"
        ))

    lines.append(_box_mid(""))

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
        lines.append(_box_mid(
            f"  📈 ~${rate_usd * 365:.0f}/year  ·  ~{_fmt_tok(int(rate_tok * 365))} tok/year  ({basis})"
        ))
        lines.append(_box_mid(""))

    lines.append(_box_bot())

    # ── 14-day vertical chart ────────────────────────────────────
    daily_14d = _query_daily_14d()
    if daily_14d:
        lines.append("")
        lines.append(_box_top("14-Day Usage"))
        lines.append(_box_mid(""))
        chart_lines = _render_braille_chart(daily_14d)
        for cl in chart_lines:
            # Strip the leading 2 spaces since _box_mid adds its own padding
            lines.append(f"  │ {cl.lstrip():<60}│")
        lines.append(_box_mid(""))
        lines.append(_box_bot())

    # Part 2 metrics — router efficiency, cache hits, classifier overhead, task-type breakdown
    lines.append("")
    lines.append("  ⚙️  Quality & Performance")

    # Router efficiency — measures how often the final model matched the classifier's
    # recommendation (i.e. no fallback was needed). This is NOT a quality metric.
    efficiency = _query_router_efficiency()
    if efficiency:
        total = efficiency["total"]
        on_target = efficiency["on_target"]
        fallbacks = total - on_target
        pct = efficiency["efficiency_pct"]
        if fallbacks == 0:
            lines.append(f"  No fallbacks today ({total} routing decisions)")
        else:
            lines.append(
                f"  Fallback rate: {fallbacks}/{total} decisions ({100 - pct:.0f}% needed fallback)"
            )

    # Cache hit ratio
    cache_stats = _query_cache_hit_stats()
    if cache_stats:
        lines.append(
            f"  ⚡ {cache_stats['hit_rate_pct']:.1f}% cache hit rate "
            f"({cache_stats['cache_hits']} hits, ${cache_stats['estimated_saved_usd']:.4f} saved)"
        )

    # Classifier overhead
    overhead = _query_classifier_overhead()
    if overhead and overhead['count'] > 0:
        lines.append(f"  Routing overhead: ~{overhead['avg_ms']:.1f}ms avg (classifier)")

    # Task-type breakdown
    task_breakdown = _query_savings_by_task_type()
    if task_breakdown:
        lines.append("")
        lines.append("  Top task categories (today):")
        for item in task_breakdown[:5]:  # Show top 5
            task_type = item['task_type']
            calls = item['calls']
            saved = item['saved']
            lines.append(f"    {task_type:<12} {calls:>3} calls  ${saved:.4f} saved")

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
    
    # Reconciliation and insight
    if total_calls > 0:
        paid_calls = total_calls - free_calls
        free_pct = round(free_calls / total_calls * 100)
        avg_cost = total_cost / total_calls if total_calls else 0
        lines.append("")
        lines.append(
            f"  Total: {total_calls} routed = "
            f"{free_calls} local/prepaid + {paid_calls} external"
            + (f" + {filtered_test_calls} excluded (test)" if filtered_test_calls > 0 else "")
        )
        lines.append(
            f"  {free_pct}% zero-cost · avg ${avg_cost:.4f}/call"
        )

    return lines

def _format(tools: dict[str, dict], cc_rows: list[dict], free_rows: list[dict],
            paid_rows: list[dict],
            start: dict | None, current: dict | None, is_live: bool,
            cumulative: list[tuple[str, int, int, int, float]] | None = None,
            session_start: float | None = None) -> str:
    lines = [f"{_BOLD}╔" + "═" * (WIDTH - 2) + f"╗{_RESET}"]
    lines.append(f"{_BOLD}║  {_CYAN}LLM Router · Session Summary{_RESET}{_BOLD}" + " " * (WIDTH - 32) + f"║{_RESET}")
    lines.append(f"{_BOLD}╚" + "═" * (WIDTH - 2) + f"╝{_RESET}")

    if current:
        lines.append("")
        lines.append(_box_top("Claude Subscription"))
        for cl in _format_cc_section(start, current, is_live):
            lines.append(f"  │ {cl.lstrip():<60}│")
        lines.append(_box_bot())

    if cc_rows:
        lines.append("")
        lines += _format_cc_model_section(cc_rows)

    # Session routing in a box
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
        lines.append(_box_top("This Session"))
        lines.append(_box_mid(""))
        for sl in session_lines:
            lines.append(f"  │ {sl.lstrip():<60}│")
        lines.append(_box_mid(""))
        lines.append(_box_bot())

    # Routing logic table
    if session_start is not None:
        routing_lines = _format_routing_logic(session_start)
        if routing_lines:
            lines.append("")
            lines += routing_lines

    if cumulative:
        cum_lines = _format_cumulative_section(cumulative)
        if cum_lines:
            lines.append("")
            lines += cum_lines

    lines.append("")
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
