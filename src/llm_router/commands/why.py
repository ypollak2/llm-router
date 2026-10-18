"""llm-router why — why the router chose what it chose, for the last N decisions (P3.5-f).

Read-only: reads the decision ledger, the proxy ledger and usage.db; writes nothing.
"""
from __future__ import annotations

import argparse
import json
import sys

from llm_router import why


def main(args: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="llm-router why", description="Explain the last routing decisions")
    p.add_argument("--last", type=int, default=1, help="how many decisions (default 1)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    a = p.parse_args(args or [])
    recs, notes = why.last_decisions(a.last)
    if a.json:
        print(json.dumps({"decisions": recs, "notes": notes}, indent=2))
    elif not recs:
        print("No routing decisions recorded yet.")
    else:
        print("\n\n".join(why.render(r) for r in recs))
    for n in notes:
        print(f"note: {n}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
