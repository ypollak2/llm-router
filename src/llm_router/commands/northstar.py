"""`llm-router northstar` — the North Star metric (NS1): routed-and-used share.

See ``llm_router.northstar`` for the counting rules and outcome signals; this
is only the CLI presentation.
"""

from __future__ import annotations

import argparse
import json
import sys

MIN_SAMPLE_NOTE = "too few to tell"


def _fmt_pct(x: float | None) -> str:
    return f"{x * 100:.1f}%" if x is not None else "n/a"


def cmd_northstar(args: list[str]) -> int:
    from llm_router import northstar as ns

    ap = argparse.ArgumentParser(prog="llm-router northstar", add_help=True)
    ap.add_argument("--session", default=None, help="report a single session id")
    ap.add_argument("--days", type=int, default=30, help="window in days (default 30)")
    ap.add_argument("--json", action="store_true", help="print the raw report() dict as JSON")
    parsed = ap.parse_args(args)

    days = None if parsed.session else parsed.days
    data = ns.report(days=days, session_id=parsed.session)

    if parsed.json:
        print(json.dumps(data, indent=2, sort_keys=False))
        return 0

    print(f"North Star — routed-and-used share (window: "
          f"{'single session' if parsed.session else f'{parsed.days}d'})\n")

    for row in data["sessions"]:
        n = row["units"]
        if n < ns.MIN_UNITS:
            share_s = f"{MIN_SAMPLE_NOTE} (n={n})"
        else:
            share_s = f"{_fmt_pct(row['share'])} used  (n={n})"
        attempted_share = (row["attempted"] / n) if n else None
        print(f"  {row['session_id'][:16]:16s}  {share_s:28s}  "
              f"attempted={_fmt_pct(attempted_share):>7s}  unknown={row['unknown']}")

    agg = data["aggregate"]
    print()
    if agg["too_few"] or agg["n_sessions"] == 0:
        print(f"aggregate: {MIN_SAMPLE_NOTE} (n_sessions={agg['n_sessions']})")
    else:
        print(f"aggregate over {agg['n_sessions']} session(s): "
              f"median={_fmt_pct(agg['median'])}  p25={_fmt_pct(agg['p25'])}  "
              f"max={_fmt_pct(agg['max'])}")

    print("\nby kind:")
    for kind in ns.ALL_KINDS:
        bk = data["by_kind"][kind]
        if bk["units"] == 0:
            continue
        print(f"  {kind:18s} units={bk['units']:<6d} attempted={bk['attempted']:<6d} "
              f"used={bk['used']:<6d} redo={bk['redo']:<6d} unknown={bk['unknown']}")
    return 0


if __name__ == "__main__":
    sys.exit(cmd_northstar(sys.argv[1:]))
