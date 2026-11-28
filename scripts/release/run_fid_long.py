#!/usr/bin/env python3
"""fid_long_120 report (D-352, owner D-619/D-620): REPORT-ONLY long-session retention numbers.

Model-free and offline, like fid_syn_60 (run_p32.run_fid): per item, is the planted answer token
present in the FULL context and in the PACK context? PACK is built exactly as run_p32.run_fid
builds it (g1.pack_context: build_pack door="hook", extractive rolling summary, scratch HOME).
Windows: 65536 primary (the production cloud cap, PREREG-P32-AMEND2), 32768 descriptive (the
default num_ctx of the hook and agent doors).

  python scripts/release/run_fid_long.py --out release_evals/fid_long_<sha>.json
  python scripts/release/run_fid_long.py --indices 0,20,40,60,80,100 --out /tmp/x.json   (subset)

Writes counts only (per category: worse / better / both / neither). It never enters
eval_gate's decision, release_evals/PIN.json or baseline.json, and exits 0 on any retention
outcome. It aborts (exit 1, nothing written) if any item is not in pack mode at 65536: a "full"
item would pass by construction and make the number meaningless.

worse = FULL retains the token and PACK does not; better = the reverse.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
import fid_long_gen as gen  # noqa: E402
import run_p32  # noqa: E402

PRIMARY_WINDOW = 65536
DESCRIPTIVE_WINDOW = 32768
WINDOWS = (PRIMARY_WINDOW, DESCRIPTIVE_WINDOW)
LABEL = "REPORT-ONLY (D-619/D-620): never blocks the release gate"
BAR = 0.02  # the predicted BLOCK bar of the design, printed for reference only


class NotPackMode(RuntimeError):
    pass


def measure(indices: list[int] | None = None, root: Path = ROOT) -> dict:
    """Per-item rows at both windows; raises NotPackMode (nothing written by callers) if an item
    is not in pack mode at the primary window."""
    from llm_router import context_summary

    g1 = gen.load_g1(root)
    chosen = gen.items(root, indices)
    rows: list[dict] = []
    old_window = g1.WINDOW
    try:
        with g1.scratch_env() as scratch:
            tdir = scratch / "transcripts"
            tdir.mkdir()
            for it in chosen:
                path = g1.write_transcript(it, tdir)
                context_summary.maybe_update(it.item_id, path, model=None, free_pct=100.0)
                full = bool(run_p32.retained(it.token, g1.full_context(it)))
                row = {"id": it.item_id, "category": it.category, "full": full}
                for w in WINDOWS:
                    g1.WINDOW = w
                    pack, mode = g1.pack_context(it, path, run_p32.FID_PROVIDER)
                    if w == PRIMARY_WINDOW and mode != "pack":
                        raise NotPackMode(f"{it.item_id}: mode={mode!r} at {w}, expected 'pack'")
                    row[str(w)] = {"mode": mode, "pack": bool(run_p32.retained(it.token, pack))}
                rows.append(row)
    finally:
        g1.WINDOW = old_window
    return {"rows": rows, "set_hash": gen.set_hash(chosen)}


def counts(rows: list[dict], window: int) -> dict:
    """Per category and total: worse/better/both/neither (+ n and the pack-mode count)."""
    def blank():
        return {"n": 0, "worse": 0, "better": 0, "both": 0, "neither": 0, "pack_mode": 0}
    by_cat: dict[str, dict] = {}
    total = blank()
    for r in rows:
        w = r[str(window)]
        key = ("both" if r["full"] and w["pack"] else "worse" if r["full"] else
               "better" if w["pack"] else "neither")
        for bucket in (by_cat.setdefault(r["category"], blank()), total):
            bucket["n"] += 1
            bucket[key] += 1
            bucket["pack_mode"] += w["mode"] == "pack"
    total["worse_minus_better_over_n"] = (round((total["worse"] - total["better"]) / total["n"], 4)
                                          if total["n"] else None)
    return {"window": window, "by_category": by_cat, "total": total}


def report(indices: list[int] | None = None, root: Path = ROOT) -> dict:
    t0 = time.monotonic()
    m = measure(indices, root)
    return {
        "label": LABEL, "set": "fid_long_120", "kind": "retention (model-free, $0)",
        "seed": gen.SEED_LONG, "cycles": gen.CYCLES, "n_items": len(m["rows"]),
        "set_hash": m["set_hash"], "primary_window": PRIMARY_WINDOW,
        "descriptive_window": DESCRIPTIVE_WINDOW, "reference_bar": BAR,
        "windows": {str(w): counts(m["rows"], w) for w in WINDOWS},
        "wall_seconds": round(time.monotonic() - t0, 1),
    }


def format_lines(doc: dict) -> list[str]:
    lines = [f"fid_long_120 {doc['label']}  n={doc['n_items']} set_hash={doc['set_hash'][:16]} "
             f"wall={doc['wall_seconds']}s"]
    for w, d in doc["windows"].items():
        tag = "primary" if int(w) == doc["primary_window"] else "descriptive"
        t = d["total"]
        lines.append(f"  window {w} ({tag}): worse={t['worse']} better={t['better']} both={t['both']} "
                     f"neither={t['neither']} (worse-better)/n={t['worse_minus_better_over_n']}")
        for cat, c in d["by_category"].items():
            lines.append(f"    {cat}: n={c['n']} worse={c['worse']} better={c['better']} "
                         f"both={c['both']} neither={c['neither']}")
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="JSON path (counts only); release_evals/fid_long_<sha>.json")
    ap.add_argument("--indices", help="comma-separated item indices (subset smoke); default all 120")
    a = ap.parse_args(argv)
    indices = [int(x) for x in a.indices.split(",")] if a.indices else None
    try:
        doc = report(indices)
    except NotPackMode as e:
        print(f"fid_long_120 ABORTED, nothing written: {e}", file=sys.stderr)
        return 1
    from llm_router.state_files import open_private  # 0600, like every other state write

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open_private(out, "w") as fh:
        fh.write(json.dumps(doc, indent=1, sort_keys=True) + "\n")
    print("\n".join(format_lines(doc)))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
