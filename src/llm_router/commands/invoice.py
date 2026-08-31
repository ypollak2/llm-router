"""llm_router invoice — finance-facing provider invoice reconciliation (#58).

    llm_router invoice report [--month YYYY-MM] [--format text|csv] [--provider P]

For each supported provider, pulls that month's billed total, tallies
llm_router's own recorded cost for the same period, and renders a
finance-readable reconciliation (per-provider rows + totals + a
"within 2%?" verdict). Providers whose billing export can't be reached
(missing API key, unsupported) are skipped with a note on stderr, so a
partial report is still produced rather than failing outright.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone

# Provider -> ingestor module attribute. Each exposes pull_monthly_invoice(period=...).
_PROVIDERS = ("anthropic", "openai", "gemini")


def _default_month() -> str:
    now = datetime.now(timezone.utc)
    year = now.year if now.month > 1 else now.year - 1
    mo = now.month - 1 if now.month > 1 else 12
    return f"{year:04d}-{mo:02d}"


def _usage_db_path() -> str:
    return os.environ.get("LLM_ROUTER_USAGE_PATH") or os.path.expanduser("~/.llm-router/usage.db")


def _llm_router_tally(provider: str, month: str) -> tuple[float, int]:
    """Return (total_usd, call_count) llm_router recorded for provider+month.

    Fail-soft: any DB error yields (0.0, 0) so the report still renders,
    signalling a degraded local lookup rather than crashing.
    """
    try:
        conn = sqlite3.connect(_usage_db_path())
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0), COUNT(*) FROM usage "
                "WHERE provider = ? AND strftime('%Y-%m', timestamp) = ?",
                (provider, month),
            ).fetchone()
            return (float(row[0]), int(row[1])) if row else (0.0, 0)
        finally:
            conn.close()
    except Exception:
        return (0.0, 0)


def _pull_invoice(provider: str, month: str):
    """Import the provider ingestor lazily and pull its monthly invoice.

    Returns an InvoiceReport, or None if the provider can't be reached
    (missing key / unsupported / IO error) — caller skips it.
    """
    try:
        mod = __import__(f"llm_router.invoice_reconciliation.{provider}", fromlist=["pull_monthly_invoice"])
        return mod.pull_monthly_invoice(period=month)
    except Exception as exc:  # noqa: BLE001 — best-effort, degrade gracefully
        print(f"[llm_router] skipping {provider}: {exc}", file=sys.stderr)
        return None


def cmd_invoice(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="llm-router invoice", description="Provider invoice reconciliation report",
    )
    sub = parser.add_subparsers(dest="command")
    p_report = sub.add_parser("report", help="finance-facing reconciliation report")
    p_report.add_argument("--month", default="", help="YYYY-MM (default: last complete month)")
    p_report.add_argument("--format", dest="fmt", choices=("text", "csv"), default="text")
    p_report.add_argument("--provider", default="", help="limit to one provider")
    args = parser.parse_args(argv)

    if args.command != "report":
        parser.print_help()
        return 2

    from llm_router.invoice_reconciliation import (
        build_reconciliation_report,
        compute_diff,
        format_report,
    )

    month = args.month or _default_month()
    providers = (args.provider,) if args.provider else _PROVIDERS

    diffs = []
    for provider in providers:
        invoice = _pull_invoice(provider, month)
        if invoice is None:
            continue
        total_usd, call_count = _llm_router_tally(provider, month)
        diffs.append(
            compute_diff(
                invoice=invoice, llm_router_total_usd=total_usd, llm_router_call_count=call_count,
            )
        )

    report = build_reconciliation_report(period=month, diffs=diffs)
    print(format_report(report, fmt=args.fmt))
    # Non-zero exit when out of tolerance so a scheduled job can alert on it.
    return 0 if report["within_2pct_aggregate"] else 1
