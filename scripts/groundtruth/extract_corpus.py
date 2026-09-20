#!/usr/bin/env python3
"""Extract a scrubbed, deduplicated corpus of real prompts, with a full funnel.

    python3 scripts/groundtruth/extract_corpus.py --out data/groundtruth/corpus.jsonl

Every input record ends in exactly one bucket — kept, or dropped with a named
reason — and the totals are written alongside the corpus. That is the point:
a survivor count on its own is unfalsifiable, whereas a funnel that adds up
can be checked.

Deduplication is two-stage, both on punctuation-stripped casefolded tokens:
  1. exact, on the token sequence
  2. near, on token-set Jaccard above --near-dup, to collapse the "same
     request typed twice" pairs that dominate agentic sessions

The near-exact stage keeps the FIRST occurrence and records how many it
absorbed, so a prompt repeated 15 times contributes one row and a visible
`duplicate_count` rather than 15 rows that quietly dominate a stratum.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from groundtruth import sources, triage  # noqa: E402
from groundtruth.scrub import ScrubReport, residual_risk, scrub  # noqa: E402

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s/.-]+")
_TRAILING = re.compile(r"[.,;:!?]+$")


def normalise(text: str) -> str:
    return _WS.sub(" ", text).strip().casefold()


def _stem(tok: str) -> str:
    """Strip one inflectional suffix when a substantial root remains.

    Deliberately crude. It exists so "which tests fail" and "which tests
    failed" collapse; it must not merge "string" and "str", which is why the
    remainder has to be at least four characters. Anything containing a path
    separator or a dot is left alone — `src/parser.py` is not a word.
    """
    if "/" in tok or "." in tok:
        return tok
    if tok.endswith("ss"):  # process, address, class — the s is not a plural
        return tok
    for suffix in ("ing", "ed", "es", "s"):
        if tok.endswith(suffix) and len(tok) - len(suffix) >= 4:
            return tok[: -len(suffix)]
    return tok


def tokens(text: str, *, stem: bool = True) -> tuple[str, ...]:
    """Tokens for comparison: casefolded, punctuation-stripped, lightly stemmed.

    Trailing punctuation is stripped per token rather than globally, so
    `src/parser.py` survives intact while `now.` becomes `now`. An earlier
    version compared raw whitespace splits and silently matched nothing —
    the near-duplicate stage reported zero hits and looked clean, which is
    exactly the failure mode where an empty check passes everything.
    """
    out = []
    for tok in _WS.sub(" ", _PUNCT.sub(" ", text)).strip().casefold().split():
        tok = _TRAILING.sub("", tok)
        if tok:
            out.append(_stem(tok) if stem else tok)
    return tuple(out)


def exact_key(text: str) -> str:
    return hashlib.sha1(" ".join(tokens(text)).encode()).hexdigest()[:16]


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


def load_denylist(path: Path | None) -> list[str]:
    if not path:
        return []
    if not path.exists():
        raise SystemExit(f"denylist not found: {path}")
    return [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="data/groundtruth/corpus.jsonl")
    ap.add_argument("--sources", default="llm-router-transcript,claude-code,captured",
                    help="comma-separated: " + ",".join(sources.READERS))
    ap.add_argument("--denylist", type=Path,
                    help="file of literal terms to redact (customer names etc), one per line")
    ap.add_argument("--salt", default=None,
                    help="scrubbing salt; omit for the default. Never recorded in the manifest.")
    ap.add_argument("--min-words", type=int, default=5)
    ap.add_argument("--keep-kinds", default="standalone,repo-bound",
                    help="triage verdicts to keep in the corpus")
    ap.add_argument("--near-dup", type=float, default=0.9,
                    help="Jaccard threshold for near-duplicate collapse (0 disables)")
    ap.add_argument("--dry-run", action="store_true", help="print the funnel, write nothing")
    args = ap.parse_args()

    salt = args.salt.encode() if args.salt else None
    denylist = load_denylist(args.denylist)
    keep_kinds = {k.strip() for k in args.keep_kinds.split(",") if k.strip()}

    funnel: Counter[str] = Counter()
    by_source: Counter[str] = Counter()
    scrub_total = ScrubReport()

    seen_exact: dict[str, int] = {}
    kept: list[dict] = []
    # Token sets of kept rows, bucketed by token count, so near-duplicate
    # search only compares prompts of comparable length.
    kept_sets: list[frozenset[str]] = []
    by_len: dict[int, list[int]] = {}

    for name in (s.strip() for s in args.sources.split(",")):
        reader = sources.READERS.get(name)
        if reader is None:
            raise SystemExit(f"unknown source {name!r}; known: {list(sources.READERS)}")
        for rec in reader():
            funnel["seen"] += 1
            by_source[f"seen:{name}"] += 1

            drop = sources.classify_drop(rec.text, rec.session_id, rec.workspace_is_sandbox)
            if drop:
                funnel[f"drop:{drop}"] += 1
                continue
            if len(rec.text.split()) < args.min_words:
                funnel[f"drop:{sources.DROP_TOO_SHORT}"] += 1
                continue

            key = exact_key(rec.text)
            if key in seen_exact:
                funnel["drop:duplicate-exact"] += 1
                kept[seen_exact[key]]["duplicate_count"] += 1
                continue

            toks = tokens(rec.text)
            tset = frozenset(toks)
            n = len(toks)
            hit = None
            # Jaccard >= threshold can only hold between prompts whose token
            # counts are within a factor of the threshold, so only those
            # buckets are searched.
            lo = int(n * args.near_dup) - 1
            hi = int(n / args.near_dup) + 1
            for length in range(max(1, lo), hi + 1):
                for idx in by_len.get(length, ()):
                    if jaccard(tset, kept_sets[idx]) >= args.near_dup:
                        hit = idx
                        break
                if hit is not None:
                    break
            if hit is not None:
                funnel["drop:duplicate-near"] += 1
                kept[hit]["duplicate_count"] += 1
                continue

            # Scrub BEFORE the record is retained anywhere.
            clean, rep = scrub(rec.text, denylist=denylist,
                               **({"salt": salt} if salt else {}))
            scrub_total.merge(rep)
            rec.text = clean
            rec.scrub_counts = dict(rep.counts)
            rec.residual_flags = residual_risk(clean)
            rec.finalise()

            verdict = triage.triage(clean)
            if verdict.kind not in keep_kinds:
                funnel[f"drop:triage-{verdict.kind}"] += 1
                continue

            row = rec.to_json()
            row["triage"] = verdict.kind
            row["triage_reasons"] = list(verdict.reasons)
            row["duplicate_count"] = 1
            row["needs_review"] = bool(rec.residual_flags)

            seen_exact[key] = len(kept)
            by_len.setdefault(n, []).append(len(kept))
            kept_sets.append(tset)
            kept.append(row)
            funnel["kept"] += 1
            by_source[f"kept:{name}"] += 1

    # ── Report ───────────────────────────────────────────────────────────────
    seen = funnel["seen"]
    dropped = sum(v for k, v in funnel.items() if k.startswith("drop:"))
    print(f"seen                     {seen}")
    for k, v in sorted(funnel.items()):
        if k.startswith("drop:"):
            print(f"  {k:38s} {v:6d}")
    print(f"kept                     {funnel['kept']}")
    print(f"funnel balances          {seen} == {dropped} + {funnel['kept']} "
          f"-> {seen == dropped + funnel['kept']}")
    print()
    for k, v in sorted(by_source.items()):
        print(f"  {k:38s} {v:6d}")
    print()
    print("triage of kept:", dict(Counter(r["triage"] for r in kept)))
    print("scrub hits    :", dict(sorted(scrub_total.counts.items())))
    print("needs review  :", sum(1 for r in kept if r["needs_review"]))

    if seen != dropped + funnel["kept"]:
        print("\nFUNNEL DOES NOT BALANCE — a record was dropped without a reason.",
              file=sys.stderr)
        return 2
    if not kept:
        print("\nNO RECORDS KEPT — refusing to write an empty corpus.", file=sys.stderr)
        return 3

    if args.dry_run:
        print("\n(dry run — nothing written)")
        return 0

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for row in kept:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    stats = out.with_suffix(".funnel.json")
    stats.write_text(json.dumps({
        "seen": seen,
        "kept": funnel["kept"],
        "dropped": dropped,
        "funnel": dict(sorted(funnel.items())),
        "by_source": dict(sorted(by_source.items())),
        "triage_of_kept": dict(Counter(r["triage"] for r in kept)),
        "scrub_hits": dict(sorted(scrub_total.counts.items())),
        "needs_review": sum(1 for r in kept if r["needs_review"]),
    }, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {out} ({funnel['kept']} rows)\nwrote {stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
