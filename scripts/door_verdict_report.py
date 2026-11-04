#!/usr/bin/env python3
"""Coverage, latency, cold share, door mix and outcomes of the hook routing verdict (``door_verdicts.jsonl``).

    python scripts/door_verdict_report.py [--days 7] [--file PATH]

Read-only. Every share prints with its n and the window. Prompt text is never in the file.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


def main() -> int:
    from llm_router import door_verdict as dv
    from llm_router import paths

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=float, default=7.0)
    ap.add_argument("--file", type=Path, default=None)
    args = ap.parse_args()
    path = args.file or paths.state_path(dv.LEDGER)
    cutoff = time.time() - args.days * 86400
    rows = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict) and float(r.get("ts", 0)) >= cutoff:
                rows.append(r)
    except OSError as exc:
        print(f"cannot read {path}: {exc}", file=sys.stderr)
        return 1
    s = dv.summarize(rows)
    s["source"] = str(path)
    s["window_days"] = args.days
    print(json.dumps(s, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
