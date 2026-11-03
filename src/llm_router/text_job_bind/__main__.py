"""``python -m llm_router.text_job_bind report [--days N] [--json]``: adoption and savings from the ledger."""

from __future__ import annotations

import argparse
import json
import time

from llm_router.text_job_bind import ledger


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m llm_router.text_job_bind")
    sub = ap.add_subparsers(dest="cmd", required=True)
    rp = sub.add_parser("report", help="adoption, bound rate, latency and estimated tokens avoided")
    rp.add_argument("--days", type=float, default=7.0, help="window in days (default 7)")
    rp.add_argument("--json", action="store_true", help="print the figures as JSON")
    args = ap.parse_args(argv)
    rows = ledger.read_rows(since=time.time() - args.days * 86400)
    summary = ledger.summarize(rows)
    summary["window_days"] = args.days
    print(json.dumps(summary, indent=2, sort_keys=True) if args.json else ledger.render(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
