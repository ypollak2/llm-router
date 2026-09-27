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
    from llm_router.cost import (
        import_routing_quality_ledger, import_savings_log, savings_log_path,
    )
    from llm_router.dashboard_data import summary

    # Share the existing ledger and atomic importer with the other reporting
    # surfaces. Never maintain a second counter or archive the ongoing session.
    if savings_log_path().exists():
        asyncio.run(import_savings_log())
    # Same flush for the North Star ledger (routing_quality.jsonl) — MCP/gateway
    # calls write there and were otherwise invisible to every savings_stats
    # reader, including this summary's own unverified figure below.
    asyncio.run(import_routing_quality_ledger())
    db = get_config().llm_router_db_path

    # THE canonical dashboard_data.summary() — the same function `llm-router
    # status`, `savings-report`, `gain`, and the Claude Code Stop hook all
    # call (PR #173). This used to read `query_window(...).saved_usd` and
    # print it as plain "lifetime {money}", but `saved_usd` is the
    # VERIFIED-only figure (PR6): a database with $112.84 of unverified
    # estimated savings and $0.00 verified printed "lifetime ~$0.00" here
    # while `llm-router status` on the SAME database showed "+$112.84
    # unverified (n=9,229)". Both halves are shown now, each labelled, so
    # neither can be read as the other.
    today = summary("today", db_path=db)
    lifetime = summary("lifetime", db_path=db)

    def period(label: str, s) -> str:
        # `:,.2f` throughout, matching the Claude Code Stop hook's
        # `_condense()` — one money format across both surfaces so a screenshot
        # from either reads the same way.
        bit = f"{label}: verified ${s.realized_usd:,.2f}"
        if s.unverified_usd:
            bit += f" · est +${s.unverified_usd:,.2f} (n={s.unverified_n:,})"
        return bit

    return f"⚡ llm-router · {period('today', today)} · {period('lifetime', lifetime)}"


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
