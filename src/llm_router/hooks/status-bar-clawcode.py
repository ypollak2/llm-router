#!/usr/bin/env python3
# llm_router-hook-version: 1
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

def _router_home():
    """Router state dir, resolved per call so LLM_ROUTER_HOME is honoured.

    M-04: this was a module constant bound at import, so a hook launched with
    LLM_ROUTER_HOME set still wrote to the operator's real home directory.

    Imports locally: hooks are standalone scripts with varied import headers and
    several do not import Path or os at module scope.
    """
    import os as _os
    from pathlib import Path as _P

    base = _os.environ.get("LLM_ROUTER_HOME", "").strip()
    return _P(base).expanduser() if base else _P.home() / ".llm-router"


def _state_dir():
    return str(_router_home())
def _usage_db():
    return os.path.join(_state_dir(), "usage.db")
def _session_start_file():
    return os.path.join(_state_dir(), "session_start.txt")
def _prompt_count_file():
    return os.path.join(_state_dir(), "prompt_count.txt")

STATUS_EVERY = os.environ.get("LLM_ROUTER_STATUS_EVERY", "0")

# WP-03: was 15.0/75.0 — the retired Opus 3 tier, a 3x overstatement on the
# claw-code status line. Two separate scalars, which is the shape the pricing
# lint cannot see; see the longer note in status-bar.py.
try:
    from llm_router import pricing as _pricing

    _host_price = _pricing.price_for("opus")
except ImportError:  # pragma: no cover — copied to ~/.claude/hooks/, runs standalone
    _host_price = None

HOST_PRICE_KNOWN = _host_price is not None
HOST_INPUT_PER_M = _host_price.input if _host_price else 0.0
HOST_OUTPUT_PER_M = _host_price.output if _host_price else 0.0

_FREE_PROVIDERS = {"ollama", "codex", "gemini_cli"}


def _read_session_stats() -> tuple[int, int, int, float, int]:
    """Return (sub_calls, free_calls, paid_calls, dollars_saved, savings_pct)."""
    try:
        start = float(open(_session_start_file()).read().strip())
        start_str = datetime.fromtimestamp(start, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        conn = sqlite3.connect(_usage_db())
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
                baseline += (in_tok * HOST_INPUT_PER_M + out_tok * HOST_OUTPUT_PER_M) / 1_000_000
            else:
                paid_calls += 1
                actual += cost
                baseline += (in_tok * HOST_INPUT_PER_M + out_tok * HOST_OUTPUT_PER_M) / 1_000_000

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
        count = int(open(_prompt_count_file()).read().strip()) + 1 if os.path.exists(_prompt_count_file()) else 1
    except (ValueError, OSError):
        count = 1
    try:
        with open(_prompt_count_file(), "w") as f:
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
