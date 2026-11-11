#!/usr/bin/env python3
"""P14 key stability from a proxy ledger written with LLM_ROUTER_PROXY_LAYOUT_SHADOW=1.

Usage: python scripts/p14_key_stability.py LEDGER.jsonl

Prints COUNTS ONLY (no session ids, no row contents): per session the rows that carry ``layout_shadow`` and
their distinct keys; for consecutive rows of one session where ``n_messages`` grew, how many kept the key and
how many changed; for the changed ones, which component hash changed.
"""
from __future__ import annotations

import json
import sys
from collections import Counter

COMPONENTS = ("system", "tools", "first_human", "first_reply")


def analyse(lines) -> dict:
    sessions: dict = {}
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        shadow = row.get("layout_shadow") if isinstance(row, dict) else None
        if isinstance(shadow, dict):
            sessions.setdefault(row.get("session_id"), []).append(shadow)
    kept = changed = 0
    by_component: Counter = Counter()
    per_session = []
    for rows in sessions.values():
        per_session.append((len(rows), len({r.get("key") for r in rows})))
        for prev, cur in zip(rows, rows[1:]):
            grew = isinstance(prev.get("n_messages"), int) and isinstance(cur.get("n_messages"), int) \
                and cur["n_messages"] > prev["n_messages"]
            if not grew:
                continue
            if prev.get("key") == cur.get("key"):
                kept += 1
                continue
            changed += 1
            diff = [c for c in COMPONENTS if prev.get(c) != cur.get(c)]
            for c in diff:
                by_component[c] += 1
            if not diff:
                by_component["none_identified"] += 1
    return {"sessions": len(sessions), "rows": sum(n for n, _ in per_session), "per_session": sorted(per_session),
            "growing_pairs": kept + changed, "key_kept": kept, "key_changed": changed,
            "changed_component": {c: by_component.get(c, 0) for c in (*COMPONENTS, "none_identified")}}


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    with open(argv[1], encoding="utf-8") as fh:
        r = analyse(fh)
    print(f"sessions_with_layout_shadow: {r['sessions']}")
    print(f"rows_with_layout_shadow: {r['rows']}")
    for n, k in r["per_session"]:
        print(f"session: rows={n} distinct_keys={k}")
    print(f"consecutive_pairs_n_messages_grew: {r['growing_pairs']}")
    print(f"  key_kept: {r['key_kept']}")
    print(f"  key_changed: {r['key_changed']}")
    for c, n in r["changed_component"].items():
        print(f"  changed_by_{c}: {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
