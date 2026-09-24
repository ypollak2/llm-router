#!/usr/bin/env python3
# llm_router-hook-version: 1
"""Report cumulative router savings after a Codex turn, without ending its session."""
from __future__ import annotations

import asyncio
from contextlib import redirect_stdout
import json
import os
import sys


def _summary() -> str:
    from llm_router.config import get_config
    from llm_router.cost import import_savings_log, savings_log_path
    from llm_router.dashboard_data import query_window

    # Share the existing ledger and atomic importer with the other reporting
    # surfaces. Never maintain a second counter or archive the ongoing session.
    if savings_log_path().exists():
        asyncio.run(import_savings_log())
    db = get_config().llm_router_db_path
    today = query_window("today", db_path=db)
    lifetime = query_window("lifetime", db_path=db)

    def money(value: float) -> str:
        return f"~${value:,.2f}" if abs(value) >= 1 else f"~${value:.4f}"

    # Unverified money (MCP/gateway/agentic, incl. this host's pending_savings)
    # stays out of both figures and is labelled beside them (savings.py).
    from llm_router.savings import unverified_note
    note = unverified_note(lifetime.unverified_saved_usd, lifetime.unverified_calls)
    return (
        f"⚡ llm-router · saved today {money(today.saved_usd)}"
        f" · lifetime {money(lifetime.saved_usd)}"
        " · estimated, all hosts"
        + (f" · lifetime {note}" if note else "")
    )


def main() -> None:
    if os.environ.get("LLM_ROUTER_STOP_HOOK", "").strip().lower() == "disabled":
        return
    try:
        # Imports and ledger diagnostics must not corrupt Codex's JSON stdout.
        with redirect_stdout(sys.stderr):
            message = _summary()
    except Exception:
        message = "⚡ llm-router · savings unavailable · run `llm-router summary`"
    print(json.dumps({"systemMessage": message}))


if __name__ == "__main__":
    main()
