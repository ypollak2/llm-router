#!/usr/bin/env python3
# llm-router-hook-version: 3
"""UserPromptSubmit hook — enhanced savings + routing status bar.

Displays a two-mode status line:
  compact (~80 chars):  📊 CC 45%s·28%w | 🦙✔ ⚙️✔ ☁️✔ | 💰 D:$1.42 W:$9.88 | 🛡️ enforce | 14.2x
  full    (~140 chars): 📊 CC 45%s·28%w·61%♪ | Ollama✔ Codex✔ APIs✔ | 💰 D:$1.42 W:$9.88 M:$41.15 (vs Sonnet:$58) | enforce🛡️ | eff 14.2x

Time buckets: today, this week (Mon), this calendar month, all-time.
Provider health: read from ~/.llm-router/health.json (written by background checks).
Enforcement mode: read from LLM_ROUTER_ENFORCE env var.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

# ── Paths ──────────────────────────────────────────────────────────────────
STATE_DIR = os.path.expanduser("~/.llm-router")
USAGE_JSON = os.path.join(STATE_DIR, "usage.json")
USAGE_DB = os.path.join(STATE_DIR, "usage.db")
HEALTH_JSON = os.path.join(STATE_DIR, "health.json")
SESSION_START_FILE = os.path.join(STATE_DIR, "session_start.txt")
PROMPT_COUNT_FILE = os.path.join(STATE_DIR, "prompt_count.txt")

# ── Config ─────────────────────────────────────────────────────────────────
STATUS_EVERY = os.environ.get("LLM_ROUTER_STATUS_EVERY", "0")
STATUS_MODE = os.environ.get("LLM_ROUTER_STATUS_MODE", "compact")  # compact | full
ENFORCE_MODE = os.environ.get("LLM_ROUTER_ENFORCE", "hard").lower()

# Baseline cost for "what would Sonnet have cost?" comparison
SONNET_INPUT_PER_M = 3.0
SONNET_OUTPUT_PER_M = 15.0

# ── ANSI colours ───────────────────────────────────────────────────────────
G = "\033[92m"   # green  — savings, provider OK, enforce
Y = "\033[93m"   # yellow — suggest mode, warn
R = "\033[91m"   # red    — error, provider down
C = "\033[96m"   # cyan   — shadow mode, active model
B = "\033[94m"   # blue   — efficiency multiplier (the "wow" number)
DIM = "\033[90m" # grey   — labels, separators
RST = "\033[0m"  # reset

SEP = f"{DIM} │ {RST}"

_FREE_PROVIDERS = {"ollama", "codex"}


# ── Claude subscription credits ────────────────────────────────────────────

def _read_claude_credits() -> tuple[float | None, float | None, float | None, bool]:
    """Return (session_pct, weekly_pct, sonnet_pct, is_stale)."""
    try:
        with open(USAGE_JSON) as f:
            data = json.load(f)
        stale = (time.time() - data.get("updated_at", 0)) > 1800
        return (
            data.get("session_pct"),
            data.get("weekly_pct"),
            data.get("sonnet_pct"),
            stale,
        )
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None, None, None, True


# ── Provider health ────────────────────────────────────────────────────────

def _read_provider_health() -> dict[str, str] | None:
    """Return {ollama, codex, apis} status, or None if health tracking not active.

    Returns None when health.json is missing or stale (>5 min) — callers
    suppress the health segment entirely in that case. The segment becomes
    visible automatically once the background health checker writes this file.
    """
    try:
        with open(HEALTH_JSON) as f:
            data = json.load(f)
        if time.time() - data.get("updated_at", 0) > 300:
            return None  # stale — checker may have stopped
        return {
            "ollama": data.get("ollama", "unknown"),
            "codex": data.get("codex", "unknown"),
            "apis": data.get("apis", "unknown"),
        }
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None  # not yet active


def _health_icon(status: str, label: str = "", compact: bool = True) -> str:
    """Return coloured icon + optional label for a provider status."""
    if status == "ok":
        icon, col = "✔", G
    elif status == "warn":
        icon, col = "⚠", Y
    elif status == "down":
        icon, col = "✖", R
    else:
        icon, col = "?", DIM

    if compact:
        return f"{col}{icon}{RST}"
    return f"{label}{col}{icon}{RST}"


# ── Savings queries ────────────────────────────────────────────────────────

def _time_bucket_starts() -> dict[str, str]:
    """Return SQL-ready datetime strings for today, week, month."""
    now = datetime.now(tz=timezone.utc)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    # Monday of this week
    week_start = today.replace(day=today.day - today.weekday())
    # First of this month
    month_start = today.replace(day=1)
    return {
        "today": today.strftime("%Y-%m-%d %H:%M:%S"),
        "week": week_start.strftime("%Y-%m-%d %H:%M:%S"),
        "month": month_start.strftime("%Y-%m-%d %H:%M:%S"),
    }


def _savings_for_period(conn: sqlite3.Connection, since: str) -> tuple[float, float]:
    """Return (saved_usd, baseline_usd) for calls since `since`."""
    rows = conn.execute(
        "SELECT provider, input_tokens, output_tokens, cost_usd FROM usage "
        "WHERE timestamp >= ? AND success = 1",
        (since,),
    ).fetchall()

    actual = baseline = 0.0
    for provider, in_tok, out_tok, cost in rows:
        in_tok = in_tok or 0
        out_tok = out_tok or 0
        cost = cost or 0.0
        sonnet_cost = (in_tok * SONNET_INPUT_PER_M + out_tok * SONNET_OUTPUT_PER_M) / 1_000_000
        if provider == "subscription":
            continue  # no token data for subscription calls
        elif provider in _FREE_PROVIDERS:
            baseline += sonnet_cost
            # actual cost is $0 for free providers
        else:
            actual += cost
            baseline += sonnet_cost

    return max(0.0, baseline - actual), baseline


def _read_savings() -> dict[str, tuple[float, float]]:
    """Return {today, week, month, session} → (saved_usd, baseline_usd)."""
    result: dict[str, tuple[float, float]] = {
        "today": (0.0, 0.0),
        "week": (0.0, 0.0),
        "month": (0.0, 0.0),
        "session": (0.0, 0.0),
    }
    try:
        buckets = _time_bucket_starts()
        conn = sqlite3.connect(USAGE_DB, timeout=2)
        for key, since in buckets.items():
            result[key] = _savings_for_period(conn, since)

        # Session savings (since session start file)
        try:
            start_ts = float(open(SESSION_START_FILE).read().strip())
            start_str = datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            result["session"] = _savings_for_period(conn, start_str)
        except (OSError, ValueError):
            pass

        conn.close()
    except Exception:
        pass
    return result


def _read_session_calls() -> tuple[int, int, int]:
    """Return (sub_calls, free_calls, paid_calls) for this session."""
    try:
        start = float(open(SESSION_START_FILE).read().strip())
        start_str = datetime.fromtimestamp(start, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        conn = sqlite3.connect(USAGE_DB, timeout=2)
        rows = conn.execute(
            "SELECT provider FROM usage WHERE timestamp >= ? AND success = 1",
            (start_str,),
        ).fetchall()
        conn.close()
        sub = free = paid = 0
        for (provider,) in rows:
            if provider == "subscription":
                sub += 1
            elif provider in _FREE_PROVIDERS:
                free += 1
            else:
                paid += 1
        return sub, free, paid
    except Exception:
        return 0, 0, 0


# ── Enforcement mode badge ─────────────────────────────────────────────────

def _enforce_badge(compact: bool = True) -> str:
    mode = ENFORCE_MODE
    if mode in ("hard", "on"):
        col, icon, label = G, "🛡️", "enforce"
    elif mode == "soft":
        col, icon, label = Y, "💡", "suggest"
    elif mode == "off":
        col, icon, label = C, "👻", "shadow"
    else:
        col, icon, label = DIM, "?", mode

    if compact:
        return f"{col}{icon}{RST}"
    return f"{col}{icon} {label}{RST}"


# ── Efficiency multiplier ──────────────────────────────────────────────────

def _efficiency(saved: float, baseline: float) -> str:
    """Return coloured Nx efficiency string, or empty if no data."""
    if baseline < 0.001:
        return ""
    multiplier = baseline / max(baseline - saved, 0.0001)
    if multiplier >= 2.0:
        return f"{B}{multiplier:.1f}x{RST}"
    return f"{DIM}{multiplier:.1f}x{RST}"


# ── Format helpers ─────────────────────────────────────────────────────────

def _savings_str_compact(savings: dict[str, tuple[float, float]]) -> str:
    """D:$1.42 W:$9.88 — green when positive."""
    parts = []
    for label, key in [("D", "today"), ("W", "week")]:
        saved, _ = savings[key]
        col = G if saved >= 0.01 else DIM
        parts.append(f"{col}{label}:${saved:.2f}{RST}")
    return " ".join(parts)


def _savings_str_full(savings: dict[str, tuple[float, float]]) -> str:
    """D:$1.42 W:$9.88 M:$41.15 (vs Sonnet:$58) — with baseline comparison."""
    parts = []
    for label, key in [("D", "today"), ("W", "week"), ("M", "month")]:
        saved, _ = savings[key]
        col = G if saved >= 0.01 else DIM
        parts.append(f"{col}{label}:${saved:.2f}{RST}")

    month_saved, month_baseline = savings["month"]
    if month_baseline >= 0.01:
        parts.append(f"{DIM}(vs Sonnet:${month_baseline:.0f}){RST}")

    return " ".join(parts)


def _provider_health_compact(health: dict[str, str]) -> str:
    """🦙✔ ⚙️✔ ☁️✔"""
    return (
        f"🦙{_health_icon(health['ollama'])} "
        f"⚙️{_health_icon(health['codex'])} "
        f"☁️{_health_icon(health['apis'])}"
    )


def _provider_health_full(health: dict[str, str]) -> str:
    """Ollama✔ Codex✔ APIs✔"""
    return (
        f"{_health_icon(health['ollama'], 'Ollama', compact=False)} "
        f"{_health_icon(health['codex'], 'Codex', compact=False)} "
        f"{_health_icon(health['apis'], 'APIs', compact=False)}"
    )


# ── Main format ────────────────────────────────────────────────────────────

def _format_status() -> str:
    session_pct, weekly_pct, sonnet_pct, stale = _read_claude_credits()
    savings = _read_savings()
    health = _read_provider_health()
    sub, free, paid = _read_session_calls()
    compact = STATUS_MODE != "full"

    # ── Claude subscription segment ──
    if session_pct is not None:
        stale_mark = f" {Y}⚠{RST}" if stale else ""
        if compact:
            cc = f"CC {session_pct:.0f}%s·{weekly_pct:.0f}%w{stale_mark}"
        else:
            cc = f"CC {session_pct:.0f}%s·{weekly_pct:.0f}%w·{sonnet_pct:.0f}%♪{stale_mark}"
    else:
        cc = f"{DIM}CC — run llm_check_usage{RST}"

    # ── Provider health segment (only when health.json is active and fresh) ──
    health_seg = None
    if health is not None:
        health_seg = _provider_health_compact(health) if compact else _provider_health_full(health)

    # ── Savings segment ──
    savings_seg = (
        f"💰 {_savings_str_compact(savings)}"
        if compact
        else f"💰 {_savings_str_full(savings)}"
    )

    # ── Enforcement mode ──
    enforce_seg = _enforce_badge(compact=compact)

    # ── Efficiency multiplier ──
    month_saved, month_baseline = savings["month"]
    eff = _efficiency(month_saved, month_baseline)

    # ── Session call counts (full mode only) ──
    calls_seg = ""
    if not compact and (sub + free + paid) > 0:
        calls_seg = f"{DIM}sub:{sub} free:{free} paid:{paid}{RST}"

    # ── Assemble ──
    if compact:
        # 📊 CC 45%s·28%w │ [🦙✔ ⚙️✔ ☁️✔ │] 💰 D:$1.42 W:$9.88 │ 🛡️ │ 14.2x
        parts = ["📊 ", cc]
        if health_seg:
            parts += [SEP, health_seg]
        parts += [SEP, savings_seg, SEP, enforce_seg]
        if eff:
            parts += [SEP, eff]
    else:
        # 📊 CC 45%s·28%w·61%♪ │ [Ollama✔ Codex✔ APIs✔ │] 💰 D:$1.42 W:$9.88 M:$41.15 │ enforce🛡️ │ eff 14.2x
        parts = ["📊 ", cc]
        if health_seg:
            parts += [SEP, health_seg]
        parts += [SEP, savings_seg, SEP, enforce_seg]
        if eff:
            parts += [SEP, f"{DIM}eff {RST}{eff}"]
        if calls_seg:
            parts += [SEP, calls_seg]

    return "".join(parts)


# ── Throttle ───────────────────────────────────────────────────────────────

def _should_show() -> bool:
    if STATUS_EVERY == "session":
        return False

    try:
        every = int(STATUS_EVERY)
    except ValueError:
        every = 0

    if every <= 1:
        return True

    try:
        count = int(open(PROMPT_COUNT_FILE).read().strip()) + 1 if os.path.exists(PROMPT_COUNT_FILE) else 1
    except (ValueError, OSError):
        count = 1
    try:
        with open(PROMPT_COUNT_FILE, "w") as f:
            f.write(str(count))
    except OSError:
        pass
    return count % every == 0


# ── Entry point ────────────────────────────────────────────────────────────

def main() -> None:
    try:
        json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        pass

    if not _should_show():
        sys.exit(0)

    status = _format_status()
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "systemMessage": status,
        }
    }))


if __name__ == "__main__":
    main()
