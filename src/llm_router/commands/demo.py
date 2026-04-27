"""Demo command — show routing decisions."""

from __future__ import annotations

import os
import sqlite3
import sys


# ── ANSI helpers (respect NO_COLOR / non-tty) ─────────────────────────────────

def _color_enabled() -> bool:
    return sys.stdout.isatty() and not os.getenv("NO_COLOR")


def _bold(s: str) -> str:
    return f"\033[1m{s}\033[0m" if _color_enabled() else s


def _green(s: str) -> str:
    return f"\033[32m{s}\033[0m" if _color_enabled() else s


def _yellow(s: str) -> str:
    return f"\033[33m{s}\033[0m" if _color_enabled() else s


def _red(s: str) -> str:
    return f"\033[31m{s}\033[0m" if _color_enabled() else s


def _dim(s: str) -> str:
    return f"\033[2m{s}\033[0m" if _color_enabled() else s


def _visual_len(s: str) -> int:
    """Return visible character count, stripping ANSI escape codes."""
    import re
    return len(re.sub(r'\033\[[0-9;]*m', '', s))


def _pad(s: str, width: int) -> str:
    """Left-justify s to visual width (handles ANSI-colored strings correctly)."""
    return s + " " * max(0, width - _visual_len(s))


# ── Command entry point ────────────────────────────────────────────────────────

def cmd_demo(args: list[str]) -> int:
    """Entry point for demo command."""
    _run_demo()
    return 0


# ── Helpers ────────────────────────────────────────────────────────────────────

def _load_real_routing_history(db_path: str, limit: int = 8) -> list[tuple]:
    """Return the last *limit* real routing decisions from usage.db.

    Returns list of (prompt_snippet, task_type, complexity, model, cost_str).
    Empty list if DB missing or table has no external calls.
    """
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT prompt, task_type, complexity, model, cost_usd "
            "FROM usage WHERE success=1 AND provider!='subscription' "
            "AND prompt IS NOT NULL "
            "ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
        conn.close()
    except Exception:
        return []

    result = []
    for r in rows:
        prompt = (r["prompt"] or "").strip()[:44]
        if len(r["prompt"] or "") > 44:
            prompt = prompt[:43] + "…"
        task   = (r["task_type"] or "query")[:8]
        compl  = (r["complexity"] or "moderate")[:12]
        model  = (r["model"] or "?").split("/")[-1][:18]
        cost   = f"${r['cost_usd']:.5f}" if (r["cost_usd"] or 0) < 0.001 else f"${r['cost_usd']:.4f}"
        result.append((f'"{prompt}"', task, compl, model, cost))
    return result


# ── Implementation ─────────────────────────────────────────────────────────────

def _run_demo() -> None:
    """Show routing decisions — real history if available, examples otherwise."""

    db_path = os.path.expanduser("~/.llm-router/usage.db")
    real_rows = _load_real_routing_history(db_path)
    using_real = bool(real_rows)

    # Fallback static examples — 3 focused cases showing cost savings
    cc_mode = os.getenv("LLM_ROUTER_CLAUDE_SUBSCRIPTION", "").lower() in ("true", "1", "yes")
    has_perplexity = bool(os.getenv("PERPLEXITY_API_KEY"))
    has_openai     = bool(os.getenv("OPENAI_API_KEY"))
    has_gemini     = bool(os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"))

    # 3 core examples: simple → moderate → complex cost story
    EXAMPLE_CASES = [
        ('"what does os.path.join do?"',          "query",    "simple",     "Claude Haiku",    "$0.00001"),
        ('"why is my async code slow?"',           "analyze",  "moderate",   "Claude Sonnet",   "$0.003"),
        ('"implement a Redis-backed rate limiter"',"code",     "complex",    "Claude Opus",     "$0.015"),
    ]

    cases = real_rows if using_real else EXAMPLE_CASES
    col_w = [44, 8, 12, 18, 9]
    sep = "─" * (sum(col_w) + len(col_w) * 2 + 2)

    title = "your last routing decisions" if using_real else "cost-optimized routing examples"
    print(f"\n{_bold('llm-router demo')}  — {title}\n")

    if not using_real:
        config_parts = []
        if cc_mode:
            config_parts.append("Claude Code subscription")
        if has_perplexity:
            config_parts.append("Perplexity")
        if has_openai:
            config_parts.append("OpenAI")
        if has_gemini:
            config_parts.append("Gemini")
        if not config_parts:
            config_parts.append("no external APIs configured")
        print(f"  Active config: {', '.join(config_parts)}")
        print(f"  {_dim('(no routing history yet — showing examples)')}\n")
    else:
        print(f"  {_dim('Source: ~/.llm-router/usage.db  (your actual routing decisions)')}\n")

    print(f"  {'Prompt':<{col_w[0]}}  {'Task':<{col_w[1]}}  {'Complexity':<{col_w[2]}}  {'Model':<{col_w[3]}}  {'Cost'}")
    print("  " + sep)

    total_routed = 0.0
    total_opus   = 0.0
    for prompt, task, complexity, model, cost_str in cases:
        if complexity == "simple":
            compl_label = _green(complexity)
        elif complexity in ("moderate", "—"):
            compl_label = _yellow(complexity)
        elif complexity in ("complex", "deep_reason", "deep_reasoning"):
            compl_label = _red(complexity)
        else:
            compl_label = complexity

        try:
            cost_val = float(cost_str.lstrip("$"))
            cost_label = _green(cost_str) if cost_val < 0.002 else (
                _yellow(cost_str) if cost_val < 0.01 else _red(cost_str))
        except ValueError:
            cost_label = cost_str

        prompt_disp = prompt if len(prompt) <= col_w[0] else prompt[:col_w[0] - 1] + "…"
        print(
            f"  {_pad(prompt_disp, col_w[0])}"
            f"  {_pad(task, col_w[1])}"
            f"  {_pad(compl_label, col_w[2])}"
            f"  {_pad(model, col_w[3])}"
            f"  {cost_label}"
        )
        try:
            total_routed += float(cost_str.lstrip("$"))
            total_opus   += 0.015
        except ValueError:
            pass

    print("  " + sep)

    if total_opus > 0:
        savings_pct = 100 * (1 - total_routed / total_opus)
        savings_amount = total_opus - total_routed

        print(f"\n  {_bold('Cost Comparison:')}")
        print(f"    {_red('Always-Opus:')} ${total_opus:.4f} per batch")
        print(f"    {_green('Smart Routing:')} ${total_routed:.5f} per batch")
        print(f"\n  {_bold('Savings:')}  {_green(f'${savings_amount:.4f}')} ({_green(f'{savings_pct:.0f}%')} cheaper)")

        if savings_pct > 70:
            print(f"  {_yellow('→')} {_green('Excellent savings')} — routing paid for itself immediately")
        elif savings_pct > 50:
            print(f"  {_yellow('→')} {_green('Good savings')} — paying for advanced features at budget prices")

    if not using_real:
        print(f"\n  {_yellow('Next steps:')}")
        print(f"    {_yellow('→')} Run {_bold('llm-router install')} to enable automatic routing")
        if not cc_mode:
            print(f"    {_yellow('→')} Set {_bold('LLM_ROUTER_CLAUDE_SUBSCRIPTION=true')} to use subscription models")
    else:
        print(f"\n  {_yellow('Your routing history:')}")
    print(f"  {_yellow('→')} Check savings: {_bold('llm-router gain')}")
    print(f"  {_yellow('→')} View dashboard: {_bold('llm-router dashboard')}\n")
