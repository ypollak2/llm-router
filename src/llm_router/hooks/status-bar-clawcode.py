#!/usr/bin/env python3
# llm-router-hook-version: 1
"""UserPromptSubmit hook (claw-code variant) — compact routing stats status bar.

Identical to status-bar.py but omits the Claude Code subscription usage prefix
(claw-code has no Anthropic OAuth subscription).

Output: 📊  sub:0 · free:N · paid:N   │   $X.XXX saved (Y%)
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

STATE_DIR = os.path.expanduser("~/.llm-router")
USAGE_DB = os.path.join(STATE_DIR, "usage.db")
SESSION_START_FILE = os.path.join(STATE_DIR, "session_start.txt")
PROMPT_COUNT_FILE = os.path.join(STATE_DIR, "prompt_count.txt")

STATUS_EVERY = os.environ.get("LLM_ROUTER_STATUS_EVERY", "0")

SONNET_INPUT_PER_M = 3.0
SONNET_OUTPUT_PER_M = 15.0

_FREE_PROVIDERS = {"ollama", "codex"}


def _read_session_stats() -> tuple[int, int, int, float, int]:
    """Return (sub_calls, free_calls, paid_calls, dollars_saved, savings_pct)."""
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
    sub_calls, free_calls, paid_calls, saved, pct = _read_session_stats()

    total_calls = sub_calls + free_calls + paid_calls
    if total_calls > 0:
        calls_part = f"sub:{sub_calls} · free:{free_calls} · paid:{paid_calls}"
        savings_part = f"${saved:.3f} saved ({pct}%)" if saved >= 0.001 else "no savings yet"
        router = f"{calls_part}   │   {savings_part}"
    else:
        router = "no calls yet"

    return f"📊  {router}"


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
