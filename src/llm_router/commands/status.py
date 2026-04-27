"""Status command — display routing status, savings, and subscription pressure."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta


# ── Formatting utilities ────────────────────────────────────────────────────

def _color_enabled() -> bool:
    """Check if color output is enabled."""
    return sys.stdout.isatty() and not os.getenv("NO_COLOR")


def _bold(s: str) -> str:
    """Bold text."""
    return f"\033[1m{s}\033[0m" if _color_enabled() else s


def _green(s: str) -> str:
    """Green text."""
    return f"\033[32m{s}\033[0m" if _color_enabled() else s


def _red(s: str) -> str:
    """Red text."""
    return f"\033[31m{s}\033[0m" if _color_enabled() else s


def _yellow(s: str) -> str:
    """Yellow text."""
    return f"\033[33m{s}\033[0m" if _color_enabled() else s


def _dim(s: str) -> str:
    """Dim text."""
    return f"\033[2m{s}\033[0m" if _color_enabled() else s


def _warn(label: str) -> str:
    """Formatted warning message."""
    return f"  {_yellow('⚠')}  {label}"


# ── Helper functions for status queries ────────────────────────────────────

def _savings_bar(saved: float, cost: float, width: int = 28) -> str:
    """Return a colored ASCII bar showing saved vs spent fractions."""
    total = saved + cost
    if total <= 0:
        return "  " + "─" * width
    save_w = max(1, round(saved / total * width))
    cost_w = max(0, width - save_w)
    return "  " + _green("█" * save_w) + _yellow("░" * cost_w)


def _query_routing_period(db_path: str, since_iso: str) -> tuple[int, float, float]:
    """Return (calls, cost_usd, baseline_usd) for calls since *since_iso*."""
    SONNET_IN = 3.0  # $/M tokens
    SONNET_OUT = 15.0
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT input_tokens, output_tokens, cost_usd FROM usage "
            "WHERE timestamp >= ? AND success = 1 AND provider != 'subscription'",
            (since_iso,),
        ).fetchall()
        conn.close()
        calls, cost, baseline = 0, 0.0, 0.0
        for r in rows:
            calls += 1
            cost += r["cost_usd"] or 0.0
            baseline += (
                (r["input_tokens"] or 0) * SONNET_IN
                + (r["output_tokens"] or 0) * SONNET_OUT
            ) / 1_000_000
        return calls, cost, baseline
    except Exception:
        return 0, 0.0, 0.0


def _query_free_model_savings(db_path: str) -> list[dict]:
    """Return per-provider savings rows for zero-cost providers (Ollama, Codex).
    
    Each row: {provider, calls, in_tok, out_tok, cost_usd, baseline, saved, estimated}
    """
    SONNET_IN, SONNET_OUT = 3.0, 15.0
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        # Average tokens per call across paid providers (used to estimate Codex)
        avg_row = conn.execute(
            "SELECT AVG(input_tokens), AVG(output_tokens) FROM usage "
            "WHERE success=1 AND provider NOT IN ('subscription','ollama','codex') "
            "AND input_tokens > 0"
        ).fetchone()
        avg_in = float(avg_row[0] or 0)
        avg_out = float(avg_row[1] or 0)

        rows = conn.execute(
            "SELECT provider, COUNT(*) as calls, "
            "COALESCE(SUM(input_tokens),0) as in_tok, "
            "COALESCE(SUM(output_tokens),0) as out_tok, "
            "COALESCE(SUM(cost_usd),0) as cost_usd "
            "FROM usage WHERE success=1 AND provider IN ('ollama','codex') "
            "GROUP BY provider ORDER BY calls DESC"
        ).fetchall()
        conn.close()

        result = []
        for r in rows:
            in_t = r["in_tok"] or 0
            out_t = r["out_tok"] or 0
            calls = r["calls"]

            # If tokens not tracked (Codex), estimate from avg paid-provider tokens
            if in_t == 0 and out_t == 0:
                in_t = int(avg_in * calls)
                out_t = int(avg_out * calls)
                estimated = True
            else:
                estimated = False

            baseline = (in_t * SONNET_IN + out_t * SONNET_OUT) / 1_000_000
            saved = max(0.0, baseline - (r["cost_usd"] or 0.0))
            result.append(
                {
                    "provider": r["provider"],
                    "calls": calls,
                    "in_tok": in_t,
                    "out_tok": out_t,
                    "cost_usd": r["cost_usd"] or 0.0,
                    "baseline": baseline,
                    "saved": saved,
                    "estimated": estimated,
                }
            )
        return result
    except Exception:
        return []


# ── Main command entry point ───────────────────────────────────────────────

def cmd_status(args: list[str]) -> int:
    """Execute: llm-router status
    
    Display routing status, savings summary, subscription pressure, and top models.
    """
    state_dir = os.path.expanduser("~/.llm-router")
    usage_json = os.path.join(state_dir, "usage.json")
    db_path = os.path.join(state_dir, "usage.db")
    WIDTH = 62

    print("\n" + "─" * WIDTH)
    print(f"  {_bold('llm-router status')}")
    print("─" * WIDTH)

    # ── Subscription pressure ──────────────────────────────────────────
    pressure_data: dict = {}
    try:
        with open(usage_json) as f:
            pressure_data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    print(f"\n  {_bold('Claude Code subscription')}", end="")
    if pressure_data:
        age_s = time.time() - pressure_data.get("updated_at", 0)
        age_label = f"{int(age_s / 60)}m ago" if age_s < 3600 else "stale"
        print(f"  ({age_label})")

        def _bar(pct: float, label: str) -> None:
            filled = max(0, min(20, round(pct / 5)))
            bar = _green("█" * filled) + "░" * (20 - filled)
            color = _green if pct < 70 else (_yellow if pct < 90 else _red)
            print(f"    {label:<16} {bar}  {color(f'{pct:.1f}%')}")

        _bar(pressure_data.get("session_pct", 0.0), "session (5h)")
        _bar(pressure_data.get("weekly_pct", 0.0), "weekly")
        if pressure_data.get("sonnet_pct", 0.0) > 0:
            _bar(pressure_data["sonnet_pct"], "weekly sonnet")
    else:
        print()
        print(_warn("no data — run: llm_check_usage"))

    # ── Savings summary ────────────────────────────────────────────────
    print(f"\n  {_bold('Routing savings')}")

    if not os.path.exists(db_path):
        print("    no data yet — route some tasks first\n")
    else:
        now = datetime.now(timezone.utc)
        periods = [
            ("today", now.replace(hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")),
            ("7 days", (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")),
            ("30 days", (now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")),
            ("all time", "1970-01-01 00:00:00"),
        ]

        any_data = False
        for label, since_iso in periods:
            calls, cost, baseline = _query_routing_period(db_path, since_iso)
            if calls == 0:
                continue
            any_data = True
            saved = max(0.0, baseline - cost)
            pct = 100 * saved / baseline if baseline > 0 else 0.0
            bar = _savings_bar(saved, cost)
            print(
                f"    {_bold(label):<20}  {_green(f'${saved:.3f}')} saved  "
                f"({_green(f'{pct:.0f}%')} cheaper)"
            )
            print(f"  {bar}  {calls} calls")

        if not any_data:
            print("    no external routing yet — route some tasks first")

        # Top models used
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT model, COUNT(*) as n, COALESCE(SUM(cost_usd),0) as c "
                "FROM usage WHERE success=1 AND provider!='subscription' "
                "GROUP BY model ORDER BY n DESC LIMIT 4"
            ).fetchall()
            conn.close()
            if rows:
                print(f"\n  {_bold('Top models used')}")
                for r in rows:
                    short = r["model"].split("/")[-1] if "/" in r["model"] else r["model"]
                    short = short[:30] + "…" if len(short) > 30 else short
                    print(f"    {short:<32}  {r['n']:>4}×  ${r['c']:.4f}")
        except Exception:
            pass

        # Free model savings (Ollama + Codex)
        free_rows = _query_free_model_savings(db_path)
        if free_rows:
            total_free_saved = sum(r["saved"] for r in free_rows)
            total_free_calls = sum(r["calls"] for r in free_rows)
            print(f"\n  {_bold('Free-model savings')}  {_dim('(Ollama / Codex — $0 API cost)')}")
            for r in free_rows:
                est_tag = _dim(" ~est") if r["estimated"] else ""
                in_k = f"{r['in_tok'] // 1000}k" if r["in_tok"] >= 1000 else str(r["in_tok"])
                out_k = f"{r['out_tok'] // 1000}k" if r["out_tok"] >= 1000 else str(r["out_tok"])
                tok_str = f"{in_k}↑ {out_k}↓{est_tag}"
                saved_str = f"${r['saved']:.4f}"
                print(
                    f"    {r['provider']:<10}  {r['calls']:>4} calls  "
                    f"{tok_str:<14}  {_green(saved_str)} saved"
                )
            bar = _savings_bar(total_free_saved, 0.0)
            print(
                f"  {bar}  {total_free_calls} free calls  "
                f"{_green(f'${total_free_saved:.4f}')} total saved vs Sonnet"
            )

    print(f"\n  {_bold('Subcommands')}")
    print(f"    {_bold('llm-router update')}     — update hooks to latest version")
    print(f"    {_bold('llm-router doctor')}     — full health check")
    print(f"    {_bold('llm-router dashboard')}  — web dashboard (localhost:7337)")
    print("─" * WIDTH + "\n")

    return 0
