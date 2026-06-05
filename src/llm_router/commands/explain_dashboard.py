"""Explain what each panel of the session-end dashboard counts.

Use case: the dashboard reads from several tables (`usage`, `claude_usage`,
`codex_usage`, `gemini_usage`, `savings_stats`) and a JSONL file
(`model_tracking.jsonl`). Each panel mixes a different subset on a different
time window, which makes "why is today's count 18 when my test produced 77?"
hard to answer without inspecting the SQL.

This command prints, per panel:
  * the source table(s) and the file/line of the query
  * the time window (today / this session / 14 days / lifetime)
  * row counts per source for that window
  * aggregate columns the panel displays (calls, tokens, savings)

Run: ``llm-router explain-dashboard``
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

STATE_DIR = Path.home() / ".llm-router"
DB_PATH = STATE_DIR / "usage.db"
TRACKING_PATH = STATE_DIR / "model_tracking.jsonl"


_PANEL_W = 64


def _line(char: str = "─") -> str:
    return char * _PANEL_W


def _panel_header(title: str) -> list[str]:
    return [
        f"╭─ {title} {'─' * (_PANEL_W - len(title) - 4)}╮",
    ]


def _panel_footer() -> str:
    return f"╰{_line()}╯"


def _kv(label: str, value: str) -> str:
    pad = _PANEL_W - len(label) - len(value) - 2
    pad = max(1, pad)
    return f"│ {label}{' ' * pad}{value} │"


def _note(text: str) -> str:
    pad = _PANEL_W - len(text) - 2
    pad = max(0, pad)
    return f"│ {text}{' ' * pad} │"


def _blank() -> str:
    return f"│{' ' * _PANEL_W}│"


def _today_start_unix() -> float:
    return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _today_sql_window() -> str:
    return "date(timestamp,'localtime')=date('now','localtime')"


def _routing_panel_block() -> list[str]:
    """Diagnostic for the 'ROUTING N decisions' panel."""
    out = _panel_header("ROUTING panel")
    out.append(_note("Source:  ~/.llm-router/model_tracking.jsonl"))
    out.append(_note("Code:    session-end.py:_query_routing_logic"))
    out.append(_note("Window:  today (start-of-day local)"))
    out.append(_blank())

    if not TRACKING_PATH.exists():
        out.append(_note("model_tracking.jsonl missing — no routing data."))
        out.append(_panel_footer())
        return out

    cutoff = _today_start_unix()
    today_methods: dict[str, int] = {}
    today_total = 0
    lifetime_total = 0
    with TRACKING_PATH.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            lifetime_total += 1
            ts = r.get("timestamp", 0)
            if ts < cutoff:
                continue
            method = r.get("classification_method", "unknown")
            today_methods[method] = today_methods.get(method, 0) + 1
            today_total += 1

    out.append(_kv("Today rows matched:", str(today_total)))
    for method, count in sorted(today_methods.items(), key=lambda x: -x[1]):
        out.append(_kv(f"  · {method}", str(count)))
    out.append(_blank())
    out.append(_kv("Lifetime entries (file total):", str(lifetime_total)))
    out.append(_note("If tests produced N routings but today shows < N,"))
    out.append(_note("the test's entries are likely from a prior day."))
    out.append(_panel_footer())
    return out


def _table_count_and_sum(
    conn: sqlite3.Connection,
    table: str,
    sum_cols: list[str],
    where: str,
) -> tuple[int, list[float]] | None:
    """Return (count, [sums]) for the given table+window, or None if absent."""
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if not exists:
        return None
    sums_sql = ", ".join(f"COALESCE(SUM({c}),0)" for c in sum_cols) if sum_cols else ""
    sql = f"SELECT COUNT(*){', ' + sums_sql if sums_sql else ''} FROM {table} WHERE {where}"
    row = conn.execute(sql).fetchone()
    if not row:
        return 0, [0.0] * len(sum_cols)
    return int(row[0]), [float(x) for x in row[1:]]


def _savings_panel_block() -> list[str]:
    """Diagnostic for the 'SAVINGS' panel (today row specifically)."""
    out = _panel_header("SAVINGS panel · today row")
    out.append(_note("Sources: usage + claude_usage + codex_usage"))
    out.append(_note("         + gemini_usage + savings_stats"))
    out.append(_note("Code:    session-end.py:_query_cumulative_savings"))
    out.append(_note("Window:  today (start-of-day local)"))
    out.append(_blank())

    if not DB_PATH.exists():
        out.append(_note("usage.db missing — no savings data."))
        out.append(_panel_footer())
        return out

    where = _today_sql_window()
    conn = sqlite3.connect(str(DB_PATH))
    try:
        # usage table — sums input_tokens + output_tokens + cost_usd
        usage_result = _table_count_and_sum(
            conn,
            "usage",
            ["input_tokens + output_tokens", "cost_usd"],
            f"success=1 AND {where}",
        )
        if usage_result is not None:
            usage_calls, usage_sums = usage_result
            usage_tokens, usage_cost = int(usage_sums[0]), usage_sums[1]
            out.append(_note("usage  (success=1):"))
            out.append(_kv("  rows:", str(usage_calls)))
            out.append(_kv("  tokens (in+out):", f"{usage_tokens:,}"))
            out.append(_kv("  cost_usd:", f"${usage_cost:.4f}"))
            out.append(_blank())
        else:
            usage_calls = 0
            usage_tokens = 0

        # claude_usage / codex_usage / gemini_usage — same shape
        plat_calls = 0
        plat_tokens = 0
        plat_saved = 0.0
        for table in ("claude_usage", "codex_usage", "gemini_usage"):
            r = _table_count_and_sum(
                conn, table, ["tokens_used", "cost_saved_usd"], where
            )
            if r is None:
                continue
            c, sums = r
            tokens = int(sums[0])
            saved = sums[1]
            plat_calls += c
            plat_tokens += tokens
            plat_saved += saved
            if c > 0:
                out.append(_note(f"{table}:"))
                out.append(_kv("  rows:", str(c)))
                out.append(_kv("  tokens_used:", f"{tokens:,}"))
                out.append(_kv("  cost_saved_usd:", f"${saved:.4f}"))
                out.append(_blank())

        # savings_stats — no token columns
        ss_result = _table_count_and_sum(
            conn, "savings_stats", ["estimated_claude_cost_saved"], where
        )
        ss_calls = 0
        ss_saved = 0.0
        if ss_result is not None:
            ss_calls, sums = ss_result
            ss_saved = sums[0]
            if ss_calls > 0:
                out.append(_note("savings_stats (no token column):"))
                out.append(_kv("  rows:", str(ss_calls)))
                out.append(_kv("  estimated_saved:", f"${ss_saved:.4f}"))
                out.append(_blank())

        total_calls = usage_calls + plat_calls + ss_calls
        total_tokens = usage_tokens + plat_tokens  # savings_stats has none
        total_saved = (usage_cost if usage_result else 0) * 0  # cost not saving
        # Savings rollup mirrors session-end.py logic: free providers' baseline +
        # per-platform cost_saved_usd + savings_stats estimated_claude_cost_saved.
        # We display the per-platform + savings_stats sum (the parts that hit the
        # statusline and cyber_grid SAVINGS column for today).
        total_saved = plat_saved + ss_saved
        out.append(_note("TOTAL (what the SAVINGS today row should show):"))
        out.append(_kv("  calls:", str(total_calls)))
        out.append(_kv("  tokens:", f"{total_tokens:,}"))
        out.append(_kv("  saved:", f"${total_saved:.4f}"))
    finally:
        conn.close()

    out.append(_panel_footer())
    return out


def _activity_panel_block() -> list[str]:
    """Diagnostic for the '14-DAY ACTIVITY' panel."""
    out = _panel_header("14-DAY ACTIVITY panel")
    out.append(_note("Source:  usage table ONLY"))
    out.append(_note("Code:    session-end.py / cyber_grid.py 14-day chart"))
    out.append(_note("Window:  rolling 14 days, all sessions"))
    out.append(_blank())

    if not DB_PATH.exists():
        out.append(_note("usage.db missing."))
        out.append(_panel_footer())
        return out

    conn = sqlite3.connect(str(DB_PATH))
    try:
        has_usage = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='usage'"
        ).fetchone() is not None
        if has_usage:
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(input_tokens+output_tokens),0) "
                "FROM usage WHERE timestamp >= datetime('now','-14 days')"
            ).fetchone()
            calls, tokens = (int(row[0]), int(row[1])) if row else (0, 0)
            out.append(_kv("usage rows (14d):", str(calls)))
            out.append(_kv("tokens (in+out):", f"{tokens:,}"))
        else:
            out.append(_note("usage table not present yet — no 14d activity."))
        out.append(_blank())
        out.append(_note("Note: this panel doesn't include claude_usage /"))
        out.append(_note("codex_usage / gemini_usage / savings_stats — so it"))
        out.append(_note("may be lower than the SAVINGS panel's all column."))
    finally:
        conn.close()

    out.append(_panel_footer())
    return out


def _print_header() -> list[str]:
    return [
        "",
        "LLM Router · Dashboard Explainer",
        f"DB:       {DB_PATH}",
        f"Tracking: {TRACKING_PATH}",
        "",
        "Each panel pulls from different sources and uses a different window.",
        "This is intentional but easy to miss — the panels below tell you",
        "exactly what every number in the session-end dashboard represents.",
        "",
    ]


def _check_mode_canary() -> int:
    """Exit-non-zero canary for CI: detect v9.3-style schema drift.

    Returns 0 when ``dashboard_data.query_window`` and the legacy
    ``_table_count_and_sum`` view agree, ``1`` when any source has rows
    for the window but didn't contribute to the totals. Useful as a CI
    gate after schema changes — a new table that nobody reads will fail
    this check the first time anything writes to it.
    """
    try:
        from llm_router.dashboard_data import (
            audit_sources,
            query_window,
        )
    except Exception as e:
        print(f"explain-dashboard --check: cannot import dashboard_data ({e})")
        return 1

    failures: list[str] = []
    for window in ("today", "week", "lifetime"):
        try:
            audit = audit_sources(window)
            totals = query_window(window)
        except Exception as e:
            failures.append(f"{window}: query failure ({e})")
            continue
        # Each audit row should also appear in totals.by_source. If a
        # source has rows but no contribution, that's a drift bug.
        for a in audit:
            in_totals = a.table in totals.by_source
            if a.rows_for_window > 0 and not in_totals:
                failures.append(
                    f"{window}: source `{a.table}` has {a.rows_for_window} "
                    "rows but query_window dropped it"
                )

    if failures:
        print("explain-dashboard --check: FAILED")
        for f in failures:
            print(f"  · {f}")
        return 1
    print("explain-dashboard --check: OK")
    return 0


def cmd_explain_dashboard(args: list[str] | None = None) -> int:
    """Print per-panel diagnostics for the session-end dashboard.

    Args:
        args: ``--check`` runs the v9.3 drift canary (exit 0/1, no
            visualization). Otherwise prints the per-panel breakdown.

    Returns:
        0 on success, 1 if ``--check`` detected drift.
    """
    if args and "--check" in args:
        return _check_mode_canary()
    lines: list[str] = []
    lines += _print_header()
    lines += _routing_panel_block()
    lines.append("")
    lines += _savings_panel_block()
    lines.append("")
    lines += _activity_panel_block()
    lines.append("")
    print("\n".join(lines))
    return 0
