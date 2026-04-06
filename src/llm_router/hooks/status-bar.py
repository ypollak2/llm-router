#!/usr/bin/env python3
# llm-router-hook-version: 2
"""UserPromptSubmit hook — compact credit + savings status bar.

Fires before every prompt. Displays a single visible line showing:
  - Claude Code subscription usage (session %, weekly %, sonnet %)
  - LLM Router session savings ($ saved, calls routed, % cheaper vs Sonnet)

Output is a systemMessage (visible in terminal), not contextForAgent.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

STATE_DIR = os.path.expanduser("~/.llm-router")
USAGE_JSON = os.path.join(STATE_DIR, "usage.json")
USAGE_DB = os.path.join(STATE_DIR, "usage.db")
SESSION_START_FILE = os.path.join(STATE_DIR, "session_start.txt")
PROMPT_COUNT_FILE = os.path.join(STATE_DIR, "prompt_count.txt")

# LLM_ROUTER_STATUS_EVERY controls how often the status bar fires:
#   0 or unset → every prompt (default)
#   N          → every N prompts (e.g. 5 = once every 5 prompts)
#   "session"  → disabled here; session-end hook handles session boundaries only
STATUS_EVERY = os.environ.get("LLM_ROUTER_STATUS_EVERY", "0")

SONNET_INPUT_PER_M = 3.0
SONNET_OUTPUT_PER_M = 15.0


def _read_claude_credits() -> tuple[float | None, float | None, float | None, bool]:
    """Return (session_pct, weekly_pct, sonnet_pct, is_stale) from usage.json."""
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
    except (FileNotFoundError, json.JSONDecodeError, KeyError, OSError):
        return None, None, None, True


_FREE_PROVIDERS = {"ollama", "codex"}


def _read_session_stats() -> tuple[int, int, int, float, int]:
    """Return (sub_calls, free_calls, paid_calls, dollars_saved, savings_pct).

    One SQLite pass: categorise each row as sub / free / paid, compute savings
    only for paid+free rows (subscription rows have no token data).
    """
    try:
        start = float(open(SESSION_START_FILE).read().strip())
        start_str = datetime.fromtimestamp(start, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        conn = sqlite3.connect(USAGE_DB)
        rows = conn.execute(
            "SELECT provider, input_tokens, output_tokens, cost_usd FROM usage "
            "WHERE timestamp >= ? AND success = 1",
            (start_str,),
        ).fetchall()
        conn.close()

        sub_calls = free_calls = paid_calls = 0
        actual = baseline = 0.0
        for provider, in_tok, out_tok, cost in rows:
            in_tok = in_tok or 0
            out_tok = out_tok or 0
            cost = cost or 0.0
            if provider == "subscription":
                sub_calls += 1
            elif provider in _FREE_PROVIDERS:
                free_calls += 1
                # Estimate baseline (free model avoided Sonnet cost)
                baseline += (in_tok * SONNET_INPUT_PER_M + out_tok * SONNET_OUTPUT_PER_M) / 1_000_000
            else:
                paid_calls += 1
                actual += cost
                baseline += (in_tok * SONNET_INPUT_PER_M + out_tok * SONNET_OUTPUT_PER_M) / 1_000_000

        saved = max(0.0, baseline - actual)
        pct = round(saved / baseline * 100) if baseline > 0 else 0
        return sub_calls, free_calls, paid_calls, saved, pct
    except Exception:
        return 0, 0, 0, 0.0, 0


def _format_status() -> str:
    session_pct, weekly_pct, sonnet_pct, stale = _read_claude_credits()
    sub_calls, free_calls, paid_calls, saved, pct = _read_session_stats()

    if session_pct is not None:
        stale_mark = " ⚠️" if stale else ""
        credit = f"CC {session_pct:.0f}%s · {weekly_pct:.0f}%w · {sonnet_pct:.0f}%♪{stale_mark}"
    else:
        credit = "CC — run llm_check_usage"

    total_calls = sub_calls + free_calls + paid_calls
    if total_calls > 0:
        calls_part = f"sub:{sub_calls} · free:{free_calls} · paid:{paid_calls}"
        savings_part = f"${saved:.3f} saved ({pct}%)" if saved >= 0.001 else "no savings yet"
        router = f"{calls_part}   │   {savings_part}"
    else:
        router = "no calls yet"

    return f"📊  {credit}   │   {router}"


def _should_show() -> bool:
    """Return True if the status bar should fire this prompt."""
    if STATUS_EVERY == "session":
        return False  # session-end hook handles both boundaries

    try:
        every = int(STATUS_EVERY)
    except ValueError:
        every = 0

    if every <= 1:
        return True  # every prompt

    # Increment persistent prompt counter, show when divisible by every
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
