#!/usr/bin/env python3
# llm_router-hook-version: 1
"""Report cumulative router savings after a Codex turn, without ending its session."""
from __future__ import annotations

import asyncio
from contextlib import redirect_stdout
import json
import os
import sys

# .env -> os.environ for this process (llm_router.env_loader). The real
# environment wins; without the package this is a no-op, as it always was.
try:
    from llm_router.env_loader import load_dotenv_files as _apply_dotenv
    _apply_dotenv()
except Exception:
    pass


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
    # unverified (n=9,229)".
    #
    # 2026-09-27: PR #178 fixed that by labelling both halves ("verified $X
    # · est +$Y"), which then showed "verified $0.00" on every machine where
    # no routed answer has ever been confirmed to replace a Claude turn —
    # still reading as "nothing saved" beside a real estimate. `compact()`
    # merges both into ONE always-labelled estimate instead — see
    # `Summary.compact()`'s docstring; the verified/unverified split is still
    # on the `Summary` object (`.headline()`), just not shown here any more.
    today = summary("today", db_path=db)
    lifetime = summary("lifetime", db_path=db)

    def period(label: str, s) -> str:
        # Summary.compact() — the ONE money-fragment implementation this
        # line, session-end.py's `_condense()`, and the statusline all call,
        # so a screenshot from any of them reads the same way.
        return f"{label} {s.compact()}"

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
