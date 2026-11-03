"""``python -m llm_router.text_job_bind report [--days N] [--json]``: adoption and savings from the ledger."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from llm_router.text_job_bind import ledger


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m llm_router.text_job_bind")
    sub = ap.add_subparsers(dest="cmd", required=True)
    rp = sub.add_parser("report", help="adoption, bound rate, latency and estimated tokens avoided")
    rp.add_argument("--days", type=float, default=7.0, help="window in days (default 7)")
    rp.add_argument("--json", action="store_true", help="print the figures as JSON")
    rp.add_argument("--transcripts", nargs="*", metavar="PATH", default=None,
                    help="count git commit / gh pr create commands in Claude Code transcripts (default ~/.claude/projects) "
                         "for the adoption denominator")
    args = ap.parse_args(argv)
    since = time.time() - args.days * 86400
    rows = ledger.read_rows(since=since)
    scanned = None
    if args.transcripts is not None:
        roots = [Path(p) for p in args.transcripts] or [ledger.default_transcript_root()]
        scanned = ledger.scan_transcripts(roots, since)
    summary = ledger.summarize(rows, transcripts=scanned)
    summary["window_days"] = args.days
    print(json.dumps(summary, indent=2, sort_keys=True) if args.json else ledger.render(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
