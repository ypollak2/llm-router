"""llm-router learn — fold new examples into the L3 kNN index, behind the update gate.

Command: llm-router learn [--dry-run] [--replay PATH] [--json] | --purge-user-examples

Reads ``examples.jsonl`` (forced prefixes, human-validated redos, "always local" rules),
builds a candidate kNN index and re-scores it against the held-out replay set
(``--replay`` or ``LLM_ROUTER_LEARNING_REPLAY``). The index is replaced only when exact-match
and under-route each worsen by at most 2 points; otherwise it is left as it was. Exit 0 for
accepted or nothing new, 1 for rejected. ``--dry-run`` gates and reports without installing.
``--purge-user-examples`` rebuilds the index without the user-derived embeddings earlier runs
added (``.prev`` is replaced by a copy of the purged index, so no user embedding survives in it);
delete ``examples.jsonl`` as well. A second ``learn`` waits up to 60 s for the first, then exits 2.
"""
from __future__ import annotations

import argparse
import json
import sys


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="llm-router learn", description=__doc__.splitlines()[0],
                                 epilog="--purge-user-examples also replaces complexity_knn.json.prev with a copy "
                                        "of the purged index (the old .prev held the user embeddings).")
    ap.add_argument("--dry-run", action="store_true", help="gate the update, install nothing")
    ap.add_argument("--replay", default=None, help="held-out replay JSONL (default: LLM_ROUTER_LEARNING_REPLAY)")
    ap.add_argument("--purge-user-examples", action="store_true",
                    help="remove the user-derived embeddings earlier runs added to the index")
    ap.add_argument("--json", action="store_true", help="print the verdict as JSON")
    args = ap.parse_args(argv)
    from llm_router import classifier_learning as cl

    try:
        v = cl.purge_user_examples() if args.purge_user_examples else cl.apply(dry_run=args.dry_run, replay=args.replay)
    except cl.LockTimeout as exc:
        print(f"learn: {exc}", file=sys.stderr)
        return 2
    if args.json:
        def m(x):
            return None if x is None else {"n": x.n, "exact": x.exact, "under_route": x.under_route}
        print(json.dumps({"status": v.status, "reason": v.reason, "added": v.added,
                          "base": m(v.base), "candidate": m(v.candidate), "detail": v.detail}))
    else:
        print(f"learn: {v.status} ({v.reason}); {v.added} example(s)" + (" [dry run]" if args.dry_run else ""))
        if v.base and v.candidate:
            print(f"  replay n={v.base.n}: exact {v.base.exact:.1%} -> {v.candidate.exact:.1%}, "
                  f"under-route {v.base.under_route:.1%} -> {v.candidate.under_route:.1%}")
    return 1 if v.status == cl.REJECTED else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
