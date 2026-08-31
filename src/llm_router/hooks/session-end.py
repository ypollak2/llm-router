#!/usr/bin/env python3
# llm_router-hook-version: 16
"""Stop hook — unified session summary: CC subscription delta + external routing costs."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request
import io
import uuid
from datetime import datetime, timezone

try:
    from rich.console import Console
    from llm_router.ui.session_summary import SessionSummaryDashboard
    HAS_RICH_DASHBOARD = True
except ImportError:
    HAS_RICH_DASHBOARD = False

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

# AC-3: derive the host-baseline price from cost.py's single source of truth
# instead of hardcoding a stale copy. Before this, session-end used $15/$75 (Opus
# 4.6) while cost.py had moved to the current Opus price — so the end-of-session
# summary was mispriced independently of every other surface. Fail-open to the
# prior literals if cost.py can't be imported (a hook must never crash).
try:
    from llm_router.cost import _HOST_INPUT_PER_M as _CI, _HOST_OUTPUT_PER_M as _CO
    HOST_INPUT_PER_M  = float(_CI)
    HOST_OUTPUT_PER_M = float(_CO)
except Exception:
    # D8: fall open to the CURRENT host list price (5/25), matching digest/dashboard.
    # The old 15/75 fallback was ~3x inflated and diverged from every sibling surface
    # on the rare import failure.
    HOST_INPUT_PER_M  = 5.0
    HOST_OUTPUT_PER_M = 25.0
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
        s_resets = data.get("five_hour", {}).get("resets_at", "")
        result = {"session_pct": round(s, 1), "weekly_pct": round(w, 1),
                  "sonnet_pct": round(n, 1), "session_resets_at": s_resets,
                  "updated_at": time.time()}
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


_FREE_PROVIDERS = {"ollama", "codex", "gemini_cli"}

# D2: providers that have their own dedicated dashboard panel (rendered from
# their own usage table). Codex is logged to BOTH `usage` (cost.log_usage forces
# cost_usd=0 for free providers) AND `codex_usage`, so counting it in the
# model_tracking-derived free split double-counts it against `_format_codex_section`.
# Exclude these from the free split — the dedicated panel is the single owner.
_DEDICATED_PANEL_PROVIDERS = {"codex"}


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
        free  = [r for r in clean
                 if r.get("provider") in _FREE_PROVIDERS
                 and r.get("provider") not in _DEDICATED_PANEL_PROVIDERS]  # D2
        return paid, cc, free
    except Exception:
        return [], [], []


_PERIODS = [
    ("today",     "date(timestamp, 'localtime') = date('now', 'localtime')"),
    ("this week", "timestamp >= datetime('now', '-7 days')"),
    ("14 days",   "timestamp >= datetime('now', '-14 days')"),
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

    AC-5 (dual-writer race): this drainer and the async ``cost.import_savings_log``
    both drain the shared log. Reading-then-truncating unlocked let both read the
    same rows and double-insert into ``savings_stats``. We instead **atomically
    claim** the log via ``os.replace`` (only one caller wins the rename; the rest
    get ``FileNotFoundError`` and no-op), then process and delete the claimed copy
    — or append it back on failure so nothing is lost.
    """
    if not os.path.exists(SAVINGS_LOG_PATH) or not os.path.exists(DB_PATH):
        return
    claim = f"{SAVINGS_LOG_PATH}.{os.getpid()}.{uuid.uuid4().hex[:8]}.claim"
    try:
        os.replace(SAVINGS_LOG_PATH, claim)  # atomic claim — serializes drainers
    except OSError:
        return  # no live log, or another drainer claimed it first
    try:
        with open(claim) as f:
            raw = f.read().strip()
    except OSError:
        return
    if not raw:
        try:
            os.remove(claim)
        except OSError:
            pass
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
                int(r.get("input_tokens", 0) or 0),
                int(r.get("output_tokens", 0) or 0),
            ))
        except (json.JSONDecodeError, KeyError, ValueError):
            continue

    if not records:
        try:
            os.remove(claim)
        except OSError:
            pass
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
                host TEXT NOT NULL DEFAULT 'claude_code',
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0
            )
        """)
        # Idempotent migration for DBs created before token columns existed.
        for _col in ("input_tokens", "output_tokens"):
            try:
                conn.execute(f"ALTER TABLE savings_stats ADD COLUMN {_col} INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                pass  # column already present
        conn.executemany(
            "INSERT INTO savings_stats "
            "(timestamp, session_id, task_type, estimated_claude_cost_saved, external_cost, "
            "model_used, host, input_tokens, output_tokens) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            records,
        )
        conn.commit()
        conn.close()
        # Success — drop the processed claim (the live log was already claimed
        # away atomically, so there is nothing to truncate).
        try:
            os.remove(claim)
        except OSError:
            pass
    except Exception:
        # Insert failed — append the claimed rows back to the live log for a
        # later retry (append, never clobber newly-arrived lines), then drop it.
        try:
            with open(claim) as _cf, open(SAVINGS_LOG_PATH, "a") as _lf:
                _lf.write(_cf.read())
        except OSError:
            pass
        try:
            os.remove(claim)
        except OSError:
            pass


def _query_cumulative_savings() -> list[tuple[str, int, int, int, float]]:
    """Return list of (label, calls, total_in_tokens, total_out_tokens, saved_usd) per period.

    v10.1.6: delegates to ``llm_router.dashboard_data`` so the UNION logic
    across legacy ``usage`` + v9.3 per-platform tables + ``savings_stats``
    lives in one place. Pre-v10.1.6 each consumer hand-rolled its own SQL
    and silently missed sources when the schema evolved. The returned
    tuple shape is preserved so downstream renderers don't break — total
    tokens are folded into ``total_in`` (renderer only uses ``ti+to``).
    """
    if not os.path.exists(DB_PATH):
        return []
    try:
        from llm_router.dashboard_data import query_window
    except Exception:
        return []

    label_to_window = {
        "today":      "today",
        "this week":  "week",
        "14 days":    "14d",
        "this month": "month",
        "all time":   "lifetime",
    }
    results: list[tuple[str, int, int, int, float]] = []
    for label, _legacy_where in _PERIODS:
        window = label_to_window.get(label)
        if window is None:
            continue
        try:
            totals = query_window(window, db_path=DB_PATH)
        except Exception:
            continue
        results.append((label, totals.calls, totals.tokens, 0, totals.saved_usd))
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
            tools[tool] = {"count": 0, "in": 0, "out": 0, "cost": 0.0,
                           "models": {}, "model_totals": {}}
        tools[tool]["count"]  += 1
        tools[tool]["in"]     += in_tok
        tools[tool]["out"]    += out_tok
        tools[tool]["cost"]   += cost
        tools[tool]["models"][model] = tools[tool]["models"].get(model, 0) + 1
        # Per-MODEL totals, accumulated from the row that actually carries them.
        # `in`/`out`/`cost` above are the TOOL's totals. The MODELS panel used to
        # reconstruct per-model figures as `tool_total * model_call_count`, which
        # reports a tool that consumed T tokens as sum(counts) * T — an inflation
        # equal to the tool's row count. Keeping the real per-model sums here means
        # no consumer has to reconstruct what was never recoverable.
        mt = tools[tool]["model_totals"].setdefault(
            model, {"calls": 0, "in": 0, "out": 0, "cost": 0.0}
        )
        mt["calls"] += 1
        mt["in"]    += in_tok
        mt["out"]   += out_tok
        mt["cost"]  += cost
    return tools


def _host_baseline(in_tok: int, out_tok: int) -> float:
    """What Opus would charge for the same token volume (matches receipt_store)."""
    return (in_tok * HOST_INPUT_PER_M + out_tok * HOST_OUTPUT_PER_M) / 1_000_000


def _load_config_for_subscription():
    """Return the LLM Router config. Split out so a test can force the failure path."""
    from llm_router.config import get_config

    return get_config()


def _is_subscription_mode() -> bool:
    """True when Claude usage is already covered by a Pro/Max subscription.

    Fails CLOSED to False. If config cannot be read we must not conclude the user
    is a subscriber: that would suppress a cash figure a pay-per-token user is
    entitled to see. A hook must also never crash.
    """
    try:
        cfg = _load_config_for_subscription()
        return bool(getattr(cfg, "llm_router_claude_subscription", False))
    except Exception:
        return False


def _baseline_provenance() -> str:
    """``measured`` | ``estimated`` | ``unknown`` for the baseline projection.

    WP-05 requires every displayed number to state where it came from. Only
    ``measured`` if the savings baseline actually has an empirical calibration
    profile — and it does not: INITIAL_CALIBRATION covers claude-sonnet-4-6 alone,
    so the baseline's output-token count comes from _LEGACY_FALLBACK_OUTPUT and
    the figure is an estimate. Calling that "measured" is precisely the
    claim-accuracy failure the audit scores.
    """
    try:
        from llm_router.calibration import INITIAL_CALIBRATION
        from llm_router.pricing import savings_baseline_model

        model = savings_baseline_model()
        calibrated = {key[0] for key in INITIAL_CALIBRATION}
        return "measured" if model in calibrated else "estimated"
    except Exception:
        return "unknown"


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
    return color + "━" * filled + _RESET + "\033[90m" + "─" * (width - filled) + _RESET


def _cc_row(label: str, start_pct: float | None, end_pct: float) -> str:
    """Format one CC subscription row with color-coded bar."""
    bar = _smart_bar(end_pct, width=16)
    pct_str = f"{_C_WHITE}{end_pct:>3.0f}%{_RESET}"
    if start_pct is not None:
        delta = end_pct - start_pct
        if abs(delta) < 0.01:
            delta_str = f"{_C_MUTED}no change{_RESET}"
        else:
            sign = "+" if delta >= 0 else ""
            if abs(delta) < 0.1:
                fmt = f"{sign}{delta:.2f}pp"
            else:
                fmt = f"{sign}{delta:.1f}pp"
            delta_color = _C_ORANGE if abs(delta) > 5 else _C_LABEL
            delta_str = f"{delta_color}{fmt}{_RESET}"
        return f"    {_C_LABEL}{label:<12}{_RESET} {bar}  {pct_str}  {delta_str}"
    return f"    {_C_LABEL}{label:<12}{_RESET} {bar}  {pct_str}"


def _format_cc_section(start: dict | None, current: dict, is_live: bool) -> list[str]:
    src = f"{_C_MUTED}live{_RESET}" if is_live else f"{_C_MUTED}cached{_RESET}"
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
    lines = [f"    {_C_WHITE}{total}{_RESET} calls  {_C_MUTED}(subscription, $0.00){_RESET}"]
    for model, d in sorted(models.items(), key=lambda x: -x[1]["count"]):
        short = model.split("/", 1)[-1] if "/" in model else model
        if len(short) > 30:
            short = short[:28] + "…"
        top_task = max(d["tasks"], key=d["tasks"].get) if d["tasks"] else "?"
        lines.append(
            f"    {_C_LABEL}{top_task:<12}{_RESET}  {d['count']:>3}×  {short:<32}  {_C_MUTED}sub{_RESET}"
        )
    return lines


def _format_routing_section(
    tools: dict[str, dict], *, subscription: bool | None = None
) -> list[str]:
    """Render the routing panel.

    ``subscription`` is an explicit parameter rather than an ambient config read
    because the branch below changes what the panel says. Left implicit, this
    function's output depended on the developer's own config: the cash-rendering
    tests passed on a pay-per-token machine and failed on a subscriber's — the
    same by-whose-laptop verdict that made the provider-matrix test useless.
    ``None`` means "detect", which is what the hook itself passes.
    """
    if subscription is None:
        subscription = _is_subscription_mode()
    total_calls = sum(t["count"] for t in tools.values())
    total_in    = sum(t["in"]    for t in tools.values())
    total_out   = sum(t["out"]   for t in tools.values())
    total_cost  = sum(t["cost"]  for t in tools.values())
    total_base  = _host_baseline(total_in, total_out)
    # AUD-06 / WP-04: NO max(0.0, ...) here. Routing can cost more than the
    # baseline -- overhead, an escalated cheap attempt, a paid provider on a
    # prompt the subscription covered -- and when it does the honest figure is
    # negative. Clamping made a session that lost money render identically to one
    # that broke even, on the one surface users actually read, while calc_savings
    # and compute_receipt reported the loss correctly all along.
    total_saved = total_base - total_cost
    savings_pct = round(total_saved / total_base * 100) if total_base > 0 else 0
    total_tokens = total_in + total_out

    # Format token count (human-readable)
    if total_tokens >= 1_000_000:
        tokens_str = f"{total_tokens / 1_000_000:.1f}M"
    elif total_tokens >= 1_000:
        tokens_str = f"{total_tokens / 1_000:.1f}k"
    else:
        tokens_str = str(total_tokens)

    pct_color = _C_GREEN if savings_pct >= 80 else (_C_YELLOW if savings_pct >= 50 else _C_ORANGE)
    provenance = _baseline_provenance()

    if subscription:
        # WP-05: a subscriber never had the option of spending this money —
        # their Claude usage is already bought by the subscription, so the
        # counterfactual is quota consumed, not cash outlaid. README already
        # says the value is "quota runway, not cash"; this panel used to
        # contradict it by printing dollars to everyone. The benefit is still
        # reported, in the unit that is actually real for this user: tokens
        # kept off the Claude quota.
        lines = [
            f"    {_C_WHITE}{total_calls}{_RESET} calls  "
            f"{tokens_str} tokens  "
            f"{pct_color}{tokens_str} quota preserved{_RESET}  "
            f"{_C_MUTED}({savings_pct}% of baseline, {provenance}){_RESET}  "
            f"{_C_MUTED}this session{_RESET}",
        ]
    else:
        # AUD-06: a loss is reported in DOLLARS, not as a negative percentage.
        # Unclamping alone produced "-571329% saved" on a small overspending
        # session -- technically honest, operationally unreadable, and a number
        # that shape invites someone to "fix" it by restoring the clamp. The
        # magnitude of a loss is what the user can act on; the ratio is not.
        if total_saved < 0:
            figure = (
                f"{_C_ORANGE}${abs(total_saved):.4f} overspent{_RESET} "
                f"{_C_MUTED}(gross, {provenance}){_RESET}"
            )
        else:
            # D9: this is baseline-avoided GROSS of routing overhead — qualify it so
            # it's not equated with the Codex/Gemini "realized" (gross − overhead).
            # "% saved" is kept contiguous (the savings-clamp test relies on it).
            figure = (
                f"{pct_color}{savings_pct}% saved{_RESET} "
                f"{_C_MUTED}(gross, {provenance}){_RESET}"
            )
        lines = [
            f"    {_C_WHITE}{total_calls}{_RESET} calls  "
            f"{tokens_str} tokens  "
            f"${total_cost:.4f} actual  "
            f"${total_base:.4f} baseline  "
            f"{figure}  "
            # D5: explicit window so this panel isn't read as comparable to the
            # (today) provider panels or the lifetime cumulative panel beside it.
            f"{_C_MUTED}this session{_RESET}",
        ]
    for tool, d in sorted(tools.items(), key=lambda x: -x[1]["count"]):
        clean_models = {m: c for m, c in d["models"].items() if not _is_test_model(m)}
        if not clean_models:
            continue
        top_model   = max(clean_models, key=clean_models.get)
        model_short = top_model.split("/", 1)[-1] if "/" in top_model else top_model
        if len(model_short) > 22:
            model_short = model_short[:20] + "…"

        # Format tool's token count
        tool_tokens = d["in"] + d["out"]
        if tool_tokens >= 1_000:
            tool_tokens_str = f"{tool_tokens / 1_000:.1f}k"
        else:
            tool_tokens_str = str(tool_tokens)

        cost_color = _C_GREEN if d["cost"] == 0 else _C_LABEL
        lines.append(
            # chz-surface-ok: HISTORICAL report of tools that were actually called;
            # renaming them would misreport what happened.
            f"    {_C_LABEL}{tool:<12}{_RESET}  {d['count']:>3}×  "
            f"{tool_tokens_str:>6}  {model_short:<20}  {cost_color}${d['cost']:.4f}{_RESET}"
        )
    return lines


def _total_saved(tools: dict[str, dict]) -> float:
    total_in   = sum(t["in"]   for t in tools.values())
    total_out  = sum(t["out"]  for t in tools.values())
    total_cost = sum(t["cost"] for t in tools.values())
    baseline   = _host_baseline(total_in, total_out)
    # Unclamped (AUD-06): other surfaces consume this, so clamping here would
    # launder the loss before any caller could see it.
    return baseline - total_cost


def _net_session_line(free_rows: list[dict], paid_rows: list[dict]) -> str | None:
    """A prominent, HONEST net-savings line (#6).

    Net = baseline (what all routed work would have cost on the Opus host) −
    actual paid-API spend across ALL tiers. Reported UNCLAMPED: when wasteful
    paid routing (e.g. a $0.10 draft) makes the session a net loss, it says so
    in red instead of hiding it behind a notional free-tier "saved" figure.
    """
    rows = list(free_rows) + list(paid_rows)
    if not rows:
        return None
    total_in  = sum(int(r.get("input_tokens")  or 0) for r in rows)
    total_out = sum(int(r.get("output_tokens") or 0) for r in rows)
    baseline  = _host_baseline(total_in, total_out)
    actual    = sum(float(r.get("cost_usd") or 0.0) for r in paid_rows)
    net       = baseline - actual
    if net >= 0:
        return (
            f"  {_BOLD}Net saved{_RESET}      {_C_GREEN}${net:.4f}{_RESET}  "
            f"{_C_MUTED}(${baseline:.4f} baseline − ${actual:.4f} paid){_RESET}"
        )
    return (
        f"  {_BOLD}{_C_ORANGE}⚠ NET LOSS{_RESET}     {_C_ORANGE}-${abs(net):.4f}{_RESET}  "
        f"{_C_MUTED}(${actual:.4f} paid exceeds ${baseline:.4f} baseline — wasteful paid routing){_RESET}"
    )


def _format_free_section(free_rows: list[dict], paid_rows: list[dict] | None = None) -> list[str]:
    """Format free-model (Ollama) session savings.

    D3: a savings figure is only ever derived from **real** token counts. When a
    free provider reports no token volume we cannot compute a defensible
    baseline, so we show ``—`` and claim ``$0`` rather than inventing tokens from
    unrelated paid-call averages. (Codex is excluded from ``free_rows`` upstream —
    D2 — and reported by ``_format_codex_section`` from ``codex_usage``; the
    ``paid_rows`` parameter is retained only for call-site compatibility.)
    """
    if not free_rows:
        return []

    # Aggregate by provider
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
        known = (in_t + out_t) > 0
        saved = _host_baseline(in_t, out_t) if known else 0.0
        total_saved += saved
        if known:
            in_k  = f"{in_t  // 1000}k" if in_t  >= 1000 else str(in_t)
            out_k = f"{out_t // 1000}k" if out_t >= 1000 else str(out_t)
            tok_disp   = f"{in_k}↑ {out_k}↓"
            saved_disp = f"{_C_GREEN}${saved:.4f}{_RESET}"
        else:
            # Unknown token volume — no fabrication, no claimed savings.
            tok_disp   = f"{_C_MUTED}—↑ —↓{_RESET}"
            saved_disp = f"{_C_MUTED}${saved:.4f}{_RESET}"
        body.append(
            f"    {_C_LABEL}{provider:<10}{_RESET}  {d['calls']:>3}×  "
            f"{tok_disp}  {saved_disp}"
        )

    # Label based on actual providers present
    providers_present = list(by_provider.keys())
    if providers_present == ["ollama"]:
        label = "Local (Ollama)"
    elif providers_present == ["codex"]:
        label = "Prepaid (Codex)"
    else:
        label = "Local / prepaid"
    saved_color = _C_GREEN if total_saved > 0 else _C_LABEL
    lines = [
        f"    {_C_WHITE}{total_calls}{_RESET} calls  ·  "
        # D9: gross of routing overhead (matches the Routing panel; distinct from
        # the Codex/Gemini "realized" figure). "saved{_RESET} vs Claude host" is
        # kept contiguous (the DASH-2 mislabel guard relies on it).
        f"{saved_color}${total_saved:.4f} gross saved{_RESET} vs Claude host  "
        # D5: this-session window (the {label} is a provider descriptor, not a window)
        f"{_C_MUTED}this session · {label}{_RESET}"
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
        # WP-07: carry the denominator. This rate is over routing decisions we
        # RECORDED; unobserved_n is the traffic that never reached the table.
        cov = {"observed_n": 0, "unobserved_n": 0}
        try:
            from llm_router.coverage import snapshot as _cov_snapshot

            _s = _cov_snapshot()
            cov = {"observed_n": _s.observed_n, "unobserved_n": _s.unobserved_n}
        except Exception:  # noqa: BLE001
            pass
        return {
            "total": total, "on_target": on_target,
            "efficiency_pct": efficiency_pct, **cov,
        }
    except Exception:
        # WP-13 note: this returns the same {} as the no-rows branch above, so a
        # broken query is indistinguishable from a quiet day. Left as-is here
        # deliberately — it is one instance of the ~810-site fail-open triage
        # that WP-13 sequences late, on purpose, so the whole class is fixed
        # with one policy rather than piecemeal.
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
_DIM = "\033[90m"  # Use bright-black instead of dim (dim vanishes on white bg)
_RESET = "\033[0m"

# Semantic color palette — standard ANSI that adapts to terminal theme.
# All colors chosen to be readable on BOTH white and black backgrounds.
# Key rules:
#   - never use \033[2m (dim) alone — invisible on white bg
#   - never use \033[90m for labels/data — too faint on white bg
#   - use _C_LABEL (default fg) for secondary text that must be readable
#   - use _C_MUTED (\033[90m) ONLY for truly optional annotations (live, ~est, sub)
_C_CYAN    = "\033[36m"       # Teal — works on both
_C_GREEN   = "\033[32m"       # Green — works on both
_C_YELLOW  = "\033[33m"       # Yellow/brown — works on both
_C_ORANGE  = "\033[33;1m"     # Bold yellow = orange on most terminals
_C_RED     = "\033[31m"       # Red — works on both
_C_MAGENTA = "\033[35m"       # Magenta — works on both
_C_WHITE   = "\033[1m"        # Bold (inherits fg) — always visible
_C_LABEL   = ""               # Default foreground — always readable on any bg
_C_MUTED   = "\033[90m"       # Bright black — ONLY for optional annotations
_C_GRAY    = ""               # Alias: default fg (was \033[90m, too faint on white)
_C_DARK    = "\033[90m"       # Dividers and bar unfilled segments only

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
    """Query routing decision breakdown by classification method.

    v10.1.4: cutoff unified to start-of-day so this panel matches the
    SAVINGS panel's "today" scope. Prior behaviour filtered to the current
    session, causing the ROUTING and SAVINGS counts to measure different
    windows (session vs day) without any label saying so. `session_start`
    arg kept for back-compat but no longer used.
    """
    if not os.path.exists(DB_PATH):
        return []
    try:
        import json as _json
        import datetime as _dt
        tracking_path = os.path.join(STATE_DIR, "model_tracking.jsonl")
        if not os.path.exists(tracking_path):
            return []

        methods: dict[str, dict] = {}
        # Start-of-day in local time, as a unix timestamp.
        _today = _dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        cutoff = _today.timestamp()

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


def _query_session_metrics(session_start: float) -> dict:
    """Single-pass query for burn rate, fallback rate, p95 latency, routing effectiveness.

    Returns:
        session_cost_usd, burn_rate_per_hr, fallback_count, escalation_count,
        fallback_pct, escalation_pct, p95_latency (dict by tier in seconds),
        routing_effectiveness_pct, session_cost_ratio, session_calls_ratio
    """
    if not os.path.exists(DB_PATH):
        return {}
    try:
        session_iso = _session_start_iso(session_start)
        conn = sqlite3.connect(DB_PATH)

        rows = conn.execute(
            """
            SELECT complexity, final_provider, latency_ms, cost_usd,
                   was_downshifted, timestamp
            FROM routing_decisions
            WHERE timestamp >= ? AND (is_real = 1 OR is_real IS NULL)
            """,
            (session_iso,),
        ).fetchall()

        # 14-day daily costs for "session vs typical" denominator
        daily = conn.execute(
            """
            SELECT date(timestamp, 'localtime') as day,
                   SUM(cost_usd) as day_cost,
                   COUNT(*) as day_calls
            FROM routing_decisions
            WHERE date(timestamp, 'localtime') >= date('now', '-15 days')
              AND (is_real = 1 OR is_real IS NULL)
            GROUP BY day
            ORDER BY day
            """
        ).fetchall()

        conn.close()

        if not rows:
            return {}

        from datetime import datetime as _dt, timezone as _tz
        session_start_dt = _dt.fromtimestamp(session_start, tz=_tz.utc)
        now_dt = _dt.now(tz=_tz.utc)
        session_hours = max(0.017, (now_dt - session_start_dt).total_seconds() / 3600)

        _CHEAP = {"ollama", "codex", "gemini_cli"}
        _PREMIUM = {"anthropic", "openai", "deepseek"}

        total = len(rows)
        session_cost = sum(r[3] or 0.0 for r in rows)
        burn_rate = session_cost / session_hours

        fallbacks = sum(1 for r in rows if r[4] == 1)
        # escalation: simple/moderate task routed to premium API provider
        escalations = sum(
            1 for r in rows
            if (r[0] or "moderate") in ("simple", "moderate") and (r[1] or "") in _PREMIUM
        )

        # p95 latency per tier (ms → seconds)
        tier_lat: dict[str, list[float]] = {
            "simple": [], "moderate": [], "complex": [], "deep_reasoning": []
        }
        for r in rows:
            tier = r[0] if r[0] in tier_lat else "moderate"
            if r[2] and r[2] > 0:
                tier_lat[tier].append(r[2])
        p95_by_tier: dict[str, float] = {}
        for tier, lats in tier_lat.items():
            if lats:
                sl = sorted(lats)
                p95_by_tier[tier] = sl[min(len(sl) - 1, int(len(sl) * 0.95))] / 1000.0

        # Routing effectiveness: % handled by cheap / subscription providers
        cheap_count = sum(1 for r in rows if (r[1] or "") in _CHEAP | {"subscription", "gemini"})
        effectiveness_pct = cheap_count / total * 100

        # Session vs typical: compare session cost/calls to 14-day excluding today
        today_str = now_dt.astimezone().strftime("%Y-%m-%d")
        hist = [(dc, dca) for day, dc, dca in daily if day != today_str]
        cost_ratio = calls_ratio = None
        if hist:
            avg_cost = sum(c for c, _ in hist) / len(hist)
            avg_calls = sum(c for _, c in hist) / len(hist)
            if avg_cost > 0:
                cost_ratio = session_cost / avg_cost
            if avg_calls > 0:
                calls_ratio = total / avg_calls

        return {
            "session_cost_usd": session_cost,
            "session_hours": session_hours,
            "burn_rate_per_hr": burn_rate,
            "fallback_count": fallbacks,
            "escalation_count": escalations,
            "total_decisions": total,
            "fallback_pct": fallbacks / total * 100 if total else 0.0,
            "escalation_pct": escalations / total * 100 if total else 0.0,
            "p95_latency": p95_by_tier,
            "routing_effectiveness_pct": effectiveness_pct,
            "session_cost_ratio": cost_ratio,
            "session_calls_ratio": calls_ratio,
        }
    except Exception:
        return {}


def _query_daily_cache_trend() -> list[float]:
    """Return up to 14 days of daily cache hit rates as % [oldest→newest]."""
    if not os.path.exists(DB_PATH):
        return []
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            """
            SELECT date(timestamp, 'localtime') as day,
                   COUNT(*) as total,
                   SUM(CASE WHEN cache_hit = 1 THEN 1 ELSE 0 END) as hits
            FROM usage
            WHERE date(timestamp, 'localtime') >= date('now', '-14 days')
              AND cache_hit IS NOT NULL
            GROUP BY day
            ORDER BY day
            """
        ).fetchall()
        conn.close()
        return [(r[2] or 0) / max(r[1], 1) * 100 for r in rows]
    except Exception:
        return []


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


def _query_daily_14d() -> list[tuple[str, int, int, float, int]]:
    """Return last 14 days of daily usage: [(date_label, calls, tokens, saved, tokens_saved), ...].

    v10.1.6: delegates to ``llm_router.dashboard_data.query_daily``. The
    UNION across ``usage`` + v9.3 per-platform tables + ``savings_stats``
    lives in the data module so any future schema addition only requires
    updating that module — not every consumer surface.
    """
    if not os.path.exists(DB_PATH):
        return []
    try:
        from llm_router.dashboard_data import query_daily
        rows = query_daily(14, db_path=DB_PATH)
        return [(r.day, r.calls, r.tokens, r.saved_usd, r.tokens_saved) for r in rows]
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
    # D6: this count is the classification-method mix from the classifier log
    # (model_tracking.jsonl), a different store from the routing_decisions table
    # that feeds the fallback-rate below. Attribute it explicitly ("classified"
    # + source + window) so the two counts are not misread as one denominator.
    lines = [
        f"  {_BOLD}Routing{_RESET}  {_C_GREEN}●{_RESET} "
        f"{_C_WHITE}{total_hits}{_RESET} classified · "
        f"{pct_color}{zero_pct}% zero-cost{_RESET}  "
        f"{_C_MUTED}today · classifier log{_RESET}"
    ]
    # Find max method name length for alignment
    max_name = max(len(d["method"]) for d in data)
    for d in data:
        pct = (d["hits"] / total_hits) * 100
        symbol = d.get("symbol", "❓")
        name = d["method"]
        lines.append(
            f"    {symbol} {_C_LABEL}{name:<{max_name}}{_RESET}"
            f"  {_C_WHITE}{d['hits']:>3}{_RESET}"
            f"  {pct:>3.0f}%"
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
        f"    {_C_GREEN}{_BOLD}{saved_hero}{_RESET}  lifetime"
        f"    {_C_WHITE}{today_s}{_RESET}  today",
        "",
    ]

    # Period grid — vertical for readability
    for label, calls, _ti, _to, saved in periods:
        s = f"${saved:.2f}" if saved >= 1.0 else f"${saved:.4f}"
        call_str = f"{calls:,}" if calls >= 1000 else str(calls)
        short_label = {"today": "today", "this week": "week", "this month": "month", "all time": "all"}.get(label, label)
        lines.append(
            f"    {short_label:<6}"
            f"  {_C_WHITE}{s:>8}{_RESET}"
            f"  {call_str:>6}"
        )

    # Yearly projection — prefer 14-day rolling average for stability
    from datetime import datetime as _dt
    days_this_month = max(1, _dt.now().day)
    data_14d = period_map.get("14 days", (0, 0, 0, 0.0))
    month_saved = month_d[3]
    weekly_data = period_map.get("this week", (0, 0, 0, 0.0))
    weekly_saved = weekly_data[3]
    today_saved = today_d[3]
    saved_14d = data_14d[3]
    tok_14d = data_14d[1] + data_14d[2]
    month_tok = month_d[1] + month_d[2]
    weekly_tok = weekly_data[1] + weekly_data[2]
    today_tok = today_d[1] + today_d[2]
    rate_usd = 0.0
    if saved_14d > 0:
        rate_usd, rate_tok, basis = saved_14d / 14, tok_14d / 14, "14-day avg"
    elif month_saved > 0:
        rate_usd, rate_tok, basis = month_saved / days_this_month, month_tok / days_this_month, "30-day avg"
    elif weekly_saved > 0:
        rate_usd, rate_tok, basis = weekly_saved / 7, weekly_tok / 7, "7-day avg"
    elif today_saved > 0:
        rate_usd, rate_tok, basis = today_saved, today_tok, "today"
    if rate_usd > 0:
        proj_mo = rate_usd * 30
        proj_yr = rate_usd * 365
        lines.append(
            f"    ≈ ${proj_mo:.2f}/mo · ${proj_yr:.0f}/yr · {_fmt_tok(int(rate_tok * 365))} tok/yr  {_C_MUTED}({basis}){_RESET}"
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
        # D6: this total is from the routing_decisions table — a different store
        # from the classifier-log "classified" count above. Label it "routed" so
        # the two denominators are not read as the same number.
        if fallbacks == 0:
            quality_parts.append(f"{_C_GREEN}0{_RESET} fallbacks ({efficiency['total']} routed)")
        else:
            quality_parts.append(f"{_C_ORANGE}{fallbacks}{_RESET}/{efficiency['total']} routed fallbacks")

    overhead = _query_classifier_overhead()
    if overhead and overhead['count'] > 0:
        ms = overhead['avg_ms']
        ms_color = _C_GREEN if ms < 50 else (_C_YELLOW if ms < 200 else _C_ORANGE)
        quality_parts.append(f"{ms_color}{ms:.0f}ms{_RESET} avg routing")

    cache_stats = _query_cache_hit_stats()
    if cache_stats:
        hr = cache_stats['hit_rate_pct']
        hr_color = _C_GREEN if hr >= 50 else _C_LABEL
        quality_parts.append(f"{hr_color}{hr:.0f}%{_RESET} cache hit")

    if quality_parts:
        lines.append(f"    {' · '.join(quality_parts)}")

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
    lines = ["    Model selection by complexity"]

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
            if provider in ("ollama", "codex", "gemini_cli"):
                free_calls += cnt
            model_str_parts.append(f"{model} ({cnt}×)")

        model_str = " · ".join(model_str_parts)
        c_color = _COMPLEXITY_COLORS.get(complexity, _C_LABEL)
        cost_tag = f"${cost_sum:.4f}" if cost_sum > 0 else f"{_C_GREEN}free{_RESET}"

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
            + (f" + {_C_MUTED}{filtered_test_calls} excluded{_RESET}" if filtered_test_calls > 0 else "")
            + f"  · avg ${avg_cost:.4f}/call"
        )

    return lines

def _format_provider_section(table: str, title: str, emoji: str) -> list[str]:
    """Generic renderer for a per-provider dashboard section.

    Used by _format_codex_section (codex_usage) and _format_gemini_section
    (gemini_usage). Stays invisible if the table has no rows for today.
    v9.3.1.
    """
    if not os.path.exists(DB_PATH):
        return []
    try:
        conn = sqlite3.connect(DB_PATH)
        if not conn.execute(
            f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}'"
        ).fetchone():
            conn.close()
            return []
        cursor = conn.execute(
            f"SELECT model, COUNT(*) as cnt, "
            f"       COALESCE(SUM(input_tokens + output_tokens "
            f"                    + cache_creation_input_tokens "
            f"                    + cache_read_input_tokens), 0) AS tokens, "
            f"       COALESCE(SUM(cost_saved_usd), 0) AS gross_saved, "
            f"       COALESCE(SUM(routing_overhead_usd), 0) AS overhead "
            f"FROM {table} "
            f"WHERE date(timestamp, 'localtime') = date('now', 'localtime') "
            f"GROUP BY model "
            f"ORDER BY cnt DESC"
        )
        rows = cursor.fetchall()
        conn.close()
    except Exception:
        return []
    if not rows:
        return []

    lines: list[str] = [f"  {_BOLD}{emoji} {title} (today){_RESET}"]
    total_calls = 0
    total_tokens = 0
    total_gross = 0.0
    total_overhead = 0.0
    for model, cnt, toks, gross, overhead in rows:
        total_calls += cnt
        total_tokens += toks
        total_gross += gross or 0.0
        total_overhead += overhead or 0.0
        gross_tag = f"+${gross:.4f}" if gross > 0 else (f"-${-gross:.4f}" if gross < 0 else "$0.0000")
        lines.append(f"    {model:<22} {cnt:>3}×  {toks:>6} tok  saved {gross_tag}")
    realized = total_gross - total_overhead
    lines.append(
        f"    {total_calls} routed · {total_tokens} tok · "
        f"gross ${total_gross:.4f} · overhead ${total_overhead:.4f} · "
        f"realized ${realized:.4f}"
    )
    return lines


def _format_codex_section() -> list[str]:
    """Render a compact Codex CLI session summary if codex_usage has rows.

    v9.3.0 — Parallel surface for Codex CLI sessions. Reads from codex_usage
    table populated by log_codex_usage. Always reads "today" window since the
    dashboard always shows today by default; bigger reports come from other tools.
    """
    if not os.path.exists(DB_PATH):
        return []
    try:
        conn = sqlite3.connect(DB_PATH)
        # Confirm the table exists before SELECTing
        if not conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='codex_usage'"
        ).fetchone():
            conn.close()
            return []
        cursor = conn.execute(
            "SELECT model, COUNT(*) as cnt, "
            "       COALESCE(SUM(input_tokens + output_tokens "
            "                    + cache_creation_input_tokens "
            "                    + cache_read_input_tokens), 0) AS tokens, "
            "       COALESCE(SUM(cost_saved_usd), 0) AS gross_saved, "
            "       COALESCE(SUM(routing_overhead_usd), 0) AS overhead "
            "FROM codex_usage "
            "WHERE date(timestamp, 'localtime') = date('now', 'localtime') "
            "GROUP BY model "
            "ORDER BY cnt DESC"
        )
        rows = cursor.fetchall()
        conn.close()
    except Exception:
        return []
    if not rows:
        return []

    lines: list[str] = []
    lines.append(f"  {_BOLD}🔷 Codex CLI (today){_RESET}")
    total_calls = 0
    total_tokens = 0
    total_gross = 0.0
    total_overhead = 0.0
    for model, cnt, toks, gross, overhead in rows:
        total_calls += cnt
        total_tokens += toks
        total_gross += gross or 0.0
        total_overhead += overhead or 0.0
        gross_tag = f"+${gross:.4f}" if gross > 0 else (f"-${-gross:.4f}" if gross < 0 else "$0.0000")
        lines.append(f"    {model:<14} {cnt:>3}×  {toks:>6} tok  saved {gross_tag}")
    realized = total_gross - total_overhead
    summary = (
        f"    {total_calls} routed · {total_tokens} tok · "
        f"gross ${total_gross:.4f} · overhead ${total_overhead:.4f} · "
        f"realized ${realized:.4f}"
    )
    lines.append(summary)
    return lines


def _format(tools: dict[str, dict], cc_rows: list[dict], free_rows: list[dict],
            paid_rows: list[dict],
            start: dict | None, current: dict | None, is_live: bool,
            cumulative: list[tuple[str, int, int, int, float]] | None = None,
            session_start: float | None = None) -> str:
    div = f"{'─' * (WIDTH - 4)}"
    lines = ["", f"  {_C_CYAN}{_BOLD}⚡ LLM Router{_RESET}  session summary", f"  {div}"]

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
        # Honest net FIRST — baseline − actual paid, unclamped (#6). The
        # per-tier sections below show notional/gross figures; this is the
        # bottom line, and it goes red when paid routing made it a net loss.
        _net_line = _net_session_line(free_rows, paid_rows)
        if _net_line:
            lines.append(_net_line)
            # D1 (DASH-1b): the Net line above is the single canonical session
            # bottom-line (host baseline − actual paid). Everything below — the
            # per-tier Routing/Free panels, and the separately-scoped Codex/Gemini
            # (today) and lifetime panels — are scope-labeled BREAKDOWNS computed on
            # their own basis/window. They are deliberately NOT a single running
            # total; say so, so a reader never sums across scopes.
            lines.append(
                f"  {_C_MUTED}Net is the session bottom line; the panels below are "
                f"per-tier / per-scope breakdowns — not additive.{_RESET}"
            )
            lines.append("")
        lines += session_lines

    # v9.3.0 — Codex CLI parallel section. Only renders if codex_usage has
    # rows for today (otherwise stays invisible — Claude Code-only users see
    # no change).
    codex_lines = _format_codex_section()
    if codex_lines:
        lines.append("")
        lines += codex_lines

    # v9.3.1 — Gemini CLI parallel section. Same visibility rule.
    gemini_lines = _format_provider_section("gemini_usage", "Gemini CLI", "🔶")
    if gemini_lines:
        lines.append("")
        lines += gemini_lines

    if session_start is not None:
        routing_lines = _format_routing_logic(session_start)
        if routing_lines:
            lines.append("")
            lines += routing_lines

    # Enhanced 14-day sparkline + models section (replaces old cumulative savings)
    try:
        from llm_router.hooks.dashboard_enhanced import (
            render_enhanced_sparkline,
            query_last_prompt_model,
        )
        daily_14d = _query_daily_14d()
        if daily_14d:
            lines.append("")
            lines.append(f"  {'─' * (WIDTH - 4)}")
            sparkline_block = render_enhanced_sparkline(daily_14d, max_height=8)
            if sparkline_block:
                lines.append("")
                lines += sparkline_block.split("\n")

        # Last routed model
        last_model = query_last_prompt_model(db_path=DB_PATH)
        if last_model:
            lines.append("")
            lines.append(f"  {_BOLD}Last Routed Model{_RESET}  {last_model}")
    except Exception:
        # Fallback: use old cumulative section if enhanced dashboard fails
        if cumulative:
            cum_lines = _format_cumulative_section(cumulative)
            if cum_lines:
                lines.append("")
                lines.append(f"  {'─' * (WIDTH - 4)}")
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
            base = ((in_tok or 0) * HOST_INPUT_PER_M
                    + (out_tok or 0) * HOST_OUTPUT_PER_M) / 1_000_000
            if provider in _FREE_PROVIDERS:
                saved += base
            elif provider != "subscription":
                # Unclamped (AUD-06): a provider that overspent must subtract
                # from the total, not contribute zero. Clamping per-row let a
                # loss-making provider hide inside a profitable aggregate.
                saved += base - (cost or 0.0)
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
        "session_start": session_start,
        "db_path": DB_PATH,
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

def _flush_session_spend_from_mcp() -> None:
    """Signal MCP server to flush in-memory session spend to disk.

    SAVINGS fix: The MCP server holds SessionSpend in memory and updates
    session_spend.json in real-time. But if the last routed call happens
    just before session-end, there can be a brief window where the file
    is stale. This function requests a flush to ensure the file reflects
    all calls made in this session.

    Implementation: Create a flag file; wait briefly for MCP to react;
    then read the freshly-flushed file.
    """
    try:
        flush_flag = os.path.join(STATE_DIR, "session_spend_flush_request.txt")
        with open(flush_flag, "w") as f:
            f.write(str(time.time()))
        time.sleep(0.2)  # Brief delay for MCP server to react
        # Remove flag (cleanup)
        try:
            os.remove(flush_flag)
        except OSError:
            pass
    except Exception:
        pass  # Graceful failure — session-end always continues


def _read_session_spend() -> dict | None:
    """Read the real-time session spend file if it exists.

    SAVINGS fix: Call _flush_session_spend_from_mcp() first to ensure
    the file contains the latest in-memory state from MCP server.
    """
    _flush_session_spend_from_mcp()  # Ensure file is up-to-date
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


# ── CHZ-STOP-01: output verbosity for the Stop hook ───────────────────────────
# THIS IS A `Stop` HOOK, AND `Stop` FIRES AFTER EVERY AGENT RESPONSE — not once
# when a session ends, which is what the filename suggests and what the full
# boxed summary was designed for. So the heaviest output this project produces
# was printing after every single turn, with no way to turn it down short of
# unregistering the hook, which loses the information entirely.
#
# Modes:
#   full       the boxed summary, unchanged — for anyone who wants it every turn
#   condensed  one line, only when something actually happened   (DEFAULT)
#   disabled   nothing; read it on demand via `llm_router summary`
#
# WHY `condensed` IS THE DEFAULT, deliberately and not because it was suggested:
# a default should match the frequency of the event that triggers it. At
# session-end cadence the full block is proportionate; at per-turn cadence it is
# not, and the mismatch is the defect rather than the block's size. Condensed
# keeps the signal (spend, savings, routes) at a volume per-turn output can carry.
# `full` remains one env var away and is unchanged for anyone who preferred it.
# The rendered box is ANSI-coloured; strip it before matching labels, or
# every regex here silently fails against escape codes.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

_STOP_HOOK_ENV = "LLM_ROUTER_STOP_HOOK"
_STOP_MODES = ("full", "condensed", "disabled")


def _stop_hook_mode() -> str:
    """Resolve the output mode. Unknown values fall back to the default.

    Deliberately does NOT error on a typo: this runs after every turn, and a
    hook that fails closed on a misspelled env var would break the session it is
    only meant to summarise.
    """
    raw = os.environ.get(_STOP_HOOK_ENV, "").strip().lower()
    return raw if raw in _STOP_MODES else "condensed"


def _condense(summary: str) -> str:
    """Reduce the boxed summary to one line, or "" if nothing happened.

    Reports today's savings, lifetime savings, and remaining quota — the three
    numbers worth seeing every turn.

    MATCHED AGAINST THE REAL RENDER, NOT A GUESS. The first version searched for
    `$<amount>` FOLLOWED BY a label, because that is how the fixture in
    tests/test_stop_hook_verbosity.py was written — by hand, from memory. The
    actual box puts the label first (`lifetime $2299.39`), so the regex matched
    nothing and the line printed `682 routed` and no money at all, every turn,
    while its tests passed.

    That is the exact failure the old docstring warned about — "green against
    synthetic fixtures while doing nothing in production" — and writing the
    warning did not prevent it, because the fixture was still invented. The
    fixture is now a captured excerpt of real output.

    Figures are EXTRACTED, never recomputed, so condensed and full cannot
    disagree about the same session.
    """
    plain = _ANSI_RE.sub("", summary)

    def _money(label: str) -> str | None:
        # Real render: "lifetime $2299.39" / "today    $159.74" — label, then money.
        m = re.search(label + r"\s+(\$[0-9][0-9,]*\.[0-9]{2})", plain, re.I)
        return m.group(1) if m else None

    def _pct(label: str) -> int | None:
        # Real render: "5h ━━────────  16%" — a progress bar, then percent USED.
        m = re.search(label + r"[^\n%]*?(\d{1,3})%", plain, re.I)
        return int(m.group(1)) if m else None

    today = _money("today")
    lifetime = _money("lifetime")
    # GH#53: "routed" means EXECUTED, so this may only match counts that come
    # from the routing_decisions store. The old fallback matched a bare
    # "N calls", which the classifier-log line could supply the moment its
    # wording changed — relabelling hints as executions in a compact badge
    # where the reader has no figures to sanity-check against. "classified" is
    # excluded explicitly so the two can never collapse into one number.
    routes = re.search(r"(\d[\d,]*)\s+decisions?\b", plain, re.I) or \
             re.search(r"(\d[\d,]*)\s+routes?\b", plain, re.I) or \
             re.search(r"(\d[\d,]*)\s+(?!classified)calls?\b", plain, re.I)

    # CONSUMED, matching the status line. This reported REMAINING for one
    # revision, which was correct arithmetic and a bad decision: the status line
    # shows consumed, so the same quantity appeared as 39% in one surface and
    # 61% in the other, and the only way to tell them apart was reading the
    # label. The first person to see both asked whether the numbers were real.
    # Two surfaces agreeing beats either one being individually more useful.
    used_5h, used_wk = _pct("5h"), _pct("weekly")

    bits: list[str] = []
    if routes:
        # Sourced from the decisions/routes count above, never from "classified".
        bits.append(f"{routes.group(1)} routed")
    if today:
        bits.append(f"today {today}")
    if lifetime:
        bits.append(f"lifetime {lifetime}")
    if used_5h is not None or used_wk is not None:
        used = []
        if used_5h is not None:
            used.append(f"5h {used_5h}%")
        if used_wk is not None:
            used.append(f"wk {used_wk}%")
        bits.append("quota used " + "/".join(used))

    if not bits:
        return ""
    return "⚡ llm_router · " + " · ".join(bits) + "  ·  `llm-router summary` for detail"


def main() -> None:
    try:
        _hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        _hook_input = {}

    # Session Context Accumulator: archive (delete) this session's durable
    # JSONL event store now that the session is ending. Fail-open, single
    # best-effort delete — never blocks the summary below. Resolution order:
    # the real session_id from this hook's stdin payload, else env vars,
    # else the pointer file written by session-start.py.
    try:
        from llm_router import session_store as _session_store
        _explicit_sid = _hook_input.get("session_id") if isinstance(_hook_input, dict) else None
        _sid = _session_store.resolve_session_id(_explicit_sid)
        if _sid:
            _session_store.archive_session(_sid)
    except Exception:
        pass

    session_start               = _read_session_start()
    paid_rows, cc_rows, free_rows = _query_session_data(session_start)
    tools                       = _aggregate(paid_rows) if paid_rows else {}
    start, current, is_live     = _get_cc_usage()
    _sync_import_savings_log()          # flush JSONL before cumulative query
    cumulative                  = _query_cumulative_savings()
    _build_and_save_learned_profile()   # v6.1: build profile from corrections



    # Try SessionSummaryDashboard (Rich) renderer; fall back to legacy ANSI
    final_summary_output = ""

    if HAS_RICH_DASHBOARD:
        try:
            report_data = _collect_report_data(
                session_start, paid_rows, cc_rows, free_rows, tools,
                start, current, is_live, cumulative,
            )

            # Prepare data for SessionSummaryDashboard
            # Use canonical "method" key (e.g. "heuristic", "ollama") not the human "reason"
            # string — the renderer's _METHOD_SYMBOLS lookup requires canonical IDs.
            dashboard_decisions = [
                {"method": d["method"], "count": d["hits"]}
                for d in report_data.get("routing_logic", [])
            ]

            dashboard_savings = {}
            for label, calls, total_tokens, _, saved_usd in cumulative:
                if label == "today":
                    dashboard_savings["today"] = saved_usd
                    dashboard_savings["today_tokens"] = total_tokens
                elif label == "this week":
                    dashboard_savings["week"] = saved_usd
                    dashboard_savings["week_tokens"] = total_tokens
                elif label == "14 days":
                    dashboard_savings["14d"] = saved_usd
                    dashboard_savings["14d_tokens"] = total_tokens
                elif label == "this month":
                    dashboard_savings["month"] = saved_usd
                    dashboard_savings["month_tokens"] = total_tokens
                elif label == "all time":
                    dashboard_savings["lifetime"] = saved_usd
                    dashboard_savings["lifetime_tokens"] = total_tokens

            # Redirect to StringIO so Rich doesn't pollute stdout.
            # Console(record=True) without file= defaults to sys.stdout AND records;
            # that mix would corrupt the JSON envelope Claude Code reads from stdout.
            _rich_buf = io.StringIO()
            console = Console(record=True, force_terminal=True, color_system="truecolor", file=_rich_buf)
            dashboard = SessionSummaryDashboard(console=console)

            # Gather 14-day cost data from report
            daily_14d_data = report_data.get("daily_14d", [])
            daily_costs = [d[3] for d in daily_14d_data] if daily_14d_data else []

            # NOTE: this used to synthesise a 7-day history when the query
            # returned nothing, by scaling today's figure by invented ratios
            # (0.3, 0.35, 0.4, ...). That is fabricated data presented as a
            # measurement, and it is the one thing a savings report must never
            # do — a chart of made-up numbers is worse than an empty chart,
            # because an empty chart is honest about what is known. When there
            # is no daily data, show none.

            total_saved = sum(daily_costs) if daily_costs else 0.0

            # Gather 14-day model breakdown directly from routing_decisions.final_model.
            # This is the authoritative source — previous code fell through to routing
            # method names (heuristic, build-fast-path) because it never queried here.
            # COVERAGE, NOT JUST NUMBERS (audit doc 27).
            #
            # `routing_decisions` is written ONLY by llm_route and llm_auto. The whole
            # llm(task=…) family calls route_and_call() without `classification_data`,
            # and router.py guards the write with `if classification_data:` — so the
            # dominant traffic never appears here and nothing records the omission.
            #
            # This panel therefore describes ONE TOOL's routing, and was rendered as
            # "MODELS 14-day mix" as though it described everything. Worse, when no
            # llm_route call has happened inside the window the newest row can be weeks
            # old and the percentages still render as current — which is exactly what was
            # observed: 643 rows into `usage` in 24h against 0 into routing_decisions.
            #
            # So the panel now carries what it covers and how fresh it is. The fix is to
            # NAME the difference, not to widen the query — adding the missing traffic
            # would move every historical percentage silently.
            model_breakdown: dict[str, float] = {}
            model_breakdown_note = ""
            try:
                if os.path.exists(DB_PATH):
                    _mb_conn = sqlite3.connect(DB_PATH)
                    _mb_rows = _mb_conn.execute(
                        "SELECT final_model, COUNT(*) AS cnt "
                        "FROM routing_decisions "
                        "WHERE final_model IS NOT NULL AND final_model != '' "
                        "  AND date(timestamp) >= date('now', '-14 days') "
                        "GROUP BY final_model "
                        "ORDER BY cnt DESC "
                        "LIMIT 8"
                    ).fetchall()
                    _mb_newest = _mb_conn.execute(
                        "SELECT MAX(timestamp) FROM routing_decisions"
                    ).fetchone()[0]
                    _mb_conn.close()
                    _mb_total = sum(r[1] for r in _mb_rows)
                    if _mb_total > 0:
                        for _model, _cnt in _mb_rows:
                            model_breakdown[_model] = (_cnt / _mb_total) * 100
                        model_breakdown_note = "classified routes only"
                    elif _mb_newest:
                        # Rows exist but NONE inside the window. Say so rather than
                        # rendering nothing, which reads as "no routing happened".
                        model_breakdown_note = f"no classified routes since {_mb_newest[:10]}"
            except Exception:
                pass

            # Gather quota data from Claude subscription.
            # Both *_pct values are stored as 0-100 (not 0-1) — do NOT multiply by 100.
            claude_quota_pct = current.get("weekly_pct", 0.0) if current else 0.0
            claude_session_pct = current.get("session_pct", 0.0) if current else 0.0
            claude_session_resets_at = current.get("session_resets_at", "") if current else ""
            gemini_quota_pct = 0.0  # Placeholder for future Gemini integration
            claude_remaining = current.get("session_resets_at", "Unknown") if current else "Unknown"

            # If the session reset time is unknown, fall back to showing weekly
            # savings. Pull the "this week" bucket and label it truthfully.
            # Previously this summed the "all time" row but printed "saved this
            # week" — the mislabel that made the SessionEnd figure disagree with
            # the llm_savings weekly bucket by the lifetime/weekly ratio
            # (RETROSPECTIVE B-6). Value and label must name the same window.
            if not claude_remaining or claude_remaining == "Unknown":
                weekly_saved = sum(d[4] for d in cumulative if d[0] == "this week")
                if weekly_saved > 0:
                    claude_remaining = f"~{weekly_saved:.2f} USD saved this week"

            gemini_remaining = "Unknown"

            # Build daily_calls / daily_tokens from the 14-day data already computed above.
            # daily_14d_data rows are (date_str, calls, tokens, cost_usd, tokens_saved).
            daily_calls_list = [d[1] for d in daily_14d_data] if daily_14d_data else []
            daily_tokens_list = [d[2] for d in daily_14d_data] if daily_14d_data else []
            daily_tokens_saved_list = [d[4] for d in daily_14d_data] if daily_14d_data else []

            # Gather session-level metrics: burn rate, fallback %, p95 latency, etc.
            session_metrics = _query_session_metrics(session_start)
            daily_cache_trend = _query_daily_cache_trend()

            # Build session_models from tools_data so the MODELS panel shows "this session".
            # Format: [{"model": str, "calls": int, "tokens": int, "cost": float, "saved": float}]
            # The panel is titled "MODELS this session", so it aggregates EVERY model
            # invoked this session — paid, subscription and free/local alike. It was
            # previously built from `report_data["tools"]`, which is `_aggregate(paid_rows)`:
            # that silently excluded `_FREE_PROVIDERS` (ollama, codex, gemini_cli), so a
            # session routed mostly to a local model showed a panel that did not contain
            # it. A "free" cost column already exists precisely to render those rows.
            all_session_rows = paid_rows + cc_rows + free_rows
            session_models_list: list[dict] = []
            if all_session_rows:
                model_agg: dict[str, dict] = {}
                for data in _aggregate(all_session_rows).values():
                    if not isinstance(data, dict):
                        continue
                    for model, totals in data.get("model_totals", {}).items():
                        agg = model_agg.setdefault(
                            model, {"calls": 0, "tokens": 0, "cost": 0.0}
                        )
                        # Real per-model sums — NOT tool_total * call_count, which
                        # inflated tokens and cost by the tool's row count.
                        agg["calls"]  += totals["calls"]
                        agg["tokens"] += totals["in"] + totals["out"]
                        agg["cost"]   += totals["cost"]
                for model, agg in sorted(model_agg.items(), key=lambda x: -x[1]["calls"]):
                    session_models_list.append({
                        "model": model,
                        "calls": agg["calls"],
                        "tokens": agg["tokens"],
                        "cost": agg["cost"],
                        "saved": 0.0,
                    })

            dashboard.print_dashboard(
                timestamp=f"Session · {datetime.now(timezone.utc).isoformat()}",
                decisions=dashboard_decisions,
                savings=dashboard_savings,
                daily_costs=daily_costs if daily_costs else None,
                total_saved=total_saved,
                model_breakdown=model_breakdown if model_breakdown else None,
                model_breakdown_note=model_breakdown_note or None,
                session_models=session_models_list if session_models_list else None,
                claude_quota_pct=claude_quota_pct,
                claude_session_pct=claude_session_pct,
                claude_session_resets_at=claude_session_resets_at,
                gemini_quota_pct=gemini_quota_pct,
                claude_remaining=claude_remaining,
                gemini_remaining=gemini_remaining,
                daily_calls=daily_calls_list,
                daily_tokens=daily_tokens_list,
                daily_tokens_saved=daily_tokens_saved_list,
                # New session-level metrics
                burn_rate_per_hr=session_metrics.get("burn_rate_per_hr", 0.0),
                session_cost_usd=session_metrics.get("session_cost_usd", 0.0),
                fallback_pct=session_metrics.get("fallback_pct", 0.0),
                escalation_pct=session_metrics.get("escalation_pct", 0.0),
                fallback_count=session_metrics.get("fallback_count", 0),
                escalation_count=session_metrics.get("escalation_count", 0),
                p95_latency=session_metrics.get("p95_latency", {}),
                routing_effectiveness_pct=session_metrics.get("routing_effectiveness_pct", 0.0),
                session_cost_ratio=session_metrics.get("session_cost_ratio"),
                session_calls_ratio=session_metrics.get("session_calls_ratio"),
                daily_cache_trend=daily_cache_trend if daily_cache_trend else None,
            )
            colored_output = console.export_text(clear=False, styles=True)
            # Save ANSI version to disk — Claude Code UI can't render terminal
            # colors, but the user can view it with: cat ~/.llm-router/last_summary.ansi
            import re as _re
            try:
                import pathlib
                _llm_router_dir = pathlib.Path.home() / ".llm-router"
                _llm_router_dir.mkdir(parents=True, exist_ok=True)
                (_llm_router_dir / "last_summary.ansi").write_text(colored_output, encoding="utf-8")
            except Exception:
                pass
            # systemMessage gets plain text (ANSI codes stripped) for Claude Code UI.
            final_summary_output = _re.sub(r"\x1b\[[0-9;]*[mGKHF]", "", colored_output)
            # Append color hint so user knows how to view the colored version.
            final_summary_output = final_summary_output.rstrip() + (
                "\n\n🎨  Full colored summary: cat ~/.llm-router/last_summary.ansi  (or: llm-router summary)\n"
            )
        except Exception as e:
            # 🥷 Backslash-Security: using vibe-coding rules for Logging & Error Handling
            print(f"Error rendering SessionSummaryDashboard: {e}", file=sys.stderr)
            # Fall back to legacy ANSI formatting
            final_summary_output = _format(tools, cc_rows, free_rows, paid_rows, start, current, is_live, cumulative, session_start)
    else:
        # Rich dashboard not available, use legacy ANSI formatting
        final_summary_output = _format(tools, cc_rows, free_rows, paid_rows, start, current, is_live, cumulative, session_start)

    # Append session spend + real savings panel (v8.8.0)
    spend = _read_session_spend()
    if spend and spend.get("call_count", 0) > 0:
        total = spend.get("total_usd", 0.0)
        calls = spend.get("call_count", 0)
        tokens_reclaimed = spend.get("tokens_reclaimed", 0)
        ext_min = spend.get("extension_minutes", 0.0)

        # Build savings panel. ONE consistent story: the headline % and the tier
        # table below both derive from the SAME per-tier rollups (Sonnet baseline).
        # The old "Opus would cost / Actually spent / Net preserved" trio was
        # removed — it compared the Opus baseline of the *reclaimed* calls against
        # *total* spend (which includes non-reclaimed paid calls), a mixed-scope
        # figure that contradicted the tier table (e.g. "Net preserved $0.01" next
        # to "Saved $0.05") and could green-wash a session that overspent.
        lines = []
        per_model = spend.get("per_model", {}) or {}
        rollups = []
        try:
            from llm_router.tiers import render_tier_table, summarize_tiers, total_savings
            if per_model:
                rollups = summarize_tiers(per_model)
        except Exception:
            rollups = []

        if rollups:
            _actual, _baseline, _saved = total_savings(rollups)
            pct = (_saved / _baseline * 100) if _baseline > 0 else 0
            bar_len = 20
            filled = int(pct / 100 * bar_len)
            bar = _C_GREEN + "━" * filled + "\033[90m" + "─" * (bar_len - filled) + _RESET
            lines.append(f"  Routing saved    {bar} {pct:.0f}% of baseline (${_saved:.4f})")
            if tokens_reclaimed > 0:
                tok_k = tokens_reclaimed / 1000
                lines.append(f"  {tok_k:.0f}K tokens reclaimed" + (f" · +{ext_min:.0f}min runway" if ext_min >= 1 else ""))
        else:
            lines.append(f"  Session spend: ${total:.4f} across {calls} call(s)")

        if spend.get("anomaly_flag"):
            lines.insert(0, f"  {_C_RED}⚠  ANOMALY: spend rate exceeded threshold{_RESET}")

        # Detailed per-tier breakdown (free local / free subscription / paid API)
        # — the single source of truth for the savings figures above.
        try:
            if rollups:
                tier_lines = render_tier_table(rollups).split("\n")
                lines.append("")
                for tl in tier_lines:
                    lines.append("  " + tl)
        except Exception:
            # Defensive — never let a render bug nuke the session-end summary.
            pass

        spend_block = "\n".join(lines)
        final_summary_output = final_summary_output.rstrip("  " + "═" * (WIDTH - 2)) + "\n" + spend_block + "\n" + "  " + "═" * (WIDTH - 2)

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
                    final_summary_output = final_summary_output.rstrip("  " + "═" * (WIDTH - 2)) + "\n【TRENDS】\n" + trend_output + "\n" + "  " + "═" * (WIDTH - 2)
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
                final_summary_output = final_summary_output.rstrip("  " + "═" * (WIDTH - 2)) + config_note + "\n" + "  " + "═" * (WIDTH - 2)
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
                final_summary_output = final_summary_output.rstrip("  " + "═" * (WIDTH - 2)) + eval_note + "\n" + "  " + "═" * (WIDTH - 2)
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
                final_summary_output = final_summary_output.rstrip("  " + "═" * (WIDTH - 2)) + quota_timeline + "\n" + "  " + "═" * (WIDTH - 2)
    except Exception:
        pass  # Graceful failure — never break session-end

    # ── Add routing efficiency report (v10.2.0) ──────────────────────────────────
    # Shows model usage, token distribution, and detects wasteful routing patterns.
    try:
        from llm_router.hooks.lineage_integration import format_routing_section

        routing_section = format_routing_section()
        if routing_section:
            final_summary_output = final_summary_output.rstrip("  " + "═" * (WIDTH - 2)) + routing_section + "  " + "═" * (WIDTH - 2)
    except Exception:
        pass  # Graceful failure — never break session-end

    # CHZ-STOP-01: honour the verbosity mode before emitting.
    _mode = _stop_hook_mode()
    if _mode == "disabled":
        pass  # no output at all; `llm_router summary` on demand
    elif _mode == "condensed":
        _line = _condense(final_summary_output)
        if _line:
            print(json.dumps({"systemMessage": _line}))
    else:
        print(json.dumps({"systemMessage": final_summary_output}))

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
