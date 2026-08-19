#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Stage 1 (Lever B): build a domain-labeled training corpus from MMLU-Pro,
hash-audited disjoint from RouterArena's eval questions.

Faithful to vLLM-semantic-router, which fine-tunes on MMLU-Pro's 14 domains —
here we use the same 14 categories to *label* a corpus for the embedding-prototype
classifier. Firewall v2: training on a benchmark train source is allowed **iff**
a SHA-256 audit proves 0 overlap with RA's eval questions.

Why this is clean even though RA samples from MMLU-Pro:
  * We drop every MMLU-Pro question that hash-collides with an RA question.
  * The label we learn is the **domain** (public, obvious from the text) — never
    the answer and never RA's accuracy. RA's secret (answer-correctness) never
    touches the classifier.

Inputs (already in scratch this session):
  * MMLU-Pro test parquet  (HF TIGER-Lab/MMLU-Pro)
  * RA gold parquet        (full.parquet) — used ONLY to build the RA question
    hash set for the disjointness audit.

Output: data/domain_train.jsonl + data/domain_holdout.jsonl  ({prompt, domain})
        + the audit report.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from llm_router.contamination_audit import audit, hash_set, prompt_hash  # noqa: E402

_SC = Path("/private/tmp/claude-501/-Users-yaliandrona/"
           "9a763a39-220c-4130-b12d-aba5fa67cc86/scratchpad")


def _format_prompt(question: str, options: list[str]) -> str:
    """Bare MCQ representation — question + lettered options. NO RA wrapper
    (matching an RA harness template would be the PR-#140 violation)."""
    opts = "\n".join(f"{chr(65 + i)}. {o}" for i, o in enumerate(options))
    return f"{question}\n{opts}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mmlu", type=Path, default=_SC / "mmlu_pro_test.parquet")
    ap.add_argument("--ra-gold", type=Path, default=_SC / "full.parquet")
    ap.add_argument("--out-dir", type=Path, default=Path("src/llm_router/data"))
    ap.add_argument("--holdout-ratio", type=float, default=0.15)
    ap.add_argument("--per-domain-cap", type=int, default=200,
                    help="cap examples per domain (balance + keep embed cost sane)")
    ap.add_argument("--seed", type=int, default=13)
    args = ap.parse_args()

    import pyarrow.parquet as pq

    # 1) RA question hash set (the disjointness reference) — RA data touched ONLY
    #    to prove non-overlap, never as a training label.
    ra = pq.read_table(args.ra_gold).to_pylist()
    ra_questions = [str(r.get("Question", "")) for r in ra if r.get("Question")]
    ra_hashes = hash_set(ra_questions)
    print(f"RA questions: {len(ra_questions)} ({len(ra_hashes)} unique hashes)")

    # 2) MMLU-Pro rows → drop any question that collides with RA.
    mp = pq.read_table(args.mmlu).to_pylist()
    kept: list[dict] = []
    dropped = 0
    for r in mp:
        q = str(r["question"])
        if prompt_hash(q) in ra_hashes:  # exact/normalized RA overlap → drop
            dropped += 1
            continue
        opts = list(r["options"]) if r["options"] is not None else []
        kept.append({
            "prompt": _format_prompt(q, opts),
            "domain": str(r["category"]),
            "_q": q,  # for the audit; stripped before write
        })
    print(f"MMLU-Pro: {len(mp)} rows → dropped {dropped} RA-overlapping → {len(kept)} clean")

    # 3) Audit report over the KEPT training questions (must be clean).
    rep = audit([k["_q"] for k in kept], ra_hashes=ra_hashes)
    print(f"audit: mode={rep.mode} clean={rep.clean} overlap={rep.overlap_count}")
    if not rep.clean:
        print("ABORT: kept set still overlaps RA — fix the drop step.", file=sys.stderr)
        return 2

    # 4) Balance per domain + split train/holdout.
    rng = random.Random(args.seed)
    by_dom: dict[str, list[dict]] = defaultdict(list)
    for k in kept:
        by_dom[k["domain"]].append(k)
    train, holdout = [], []
    for dom, rows in by_dom.items():
        rng.shuffle(rows)
        rows = rows[: args.per_domain_cap]
        n_h = max(1, int(len(rows) * args.holdout_ratio))
        holdout.extend(rows[:n_h])
        train.extend(rows[n_h:])
    rng.shuffle(train); rng.shuffle(holdout)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    tp = args.out_dir / "domain_train.jsonl"
    hp = args.out_dir / "domain_holdout.jsonl"
    for path, rows in ((tp, train), (hp, holdout)):
        path.write_text(
            "\n".join(json.dumps({"prompt": r["prompt"], "domain": r["domain"]}) for r in rows) + "\n",
            encoding="utf-8",
        )
    (args.out_dir / "domain_train_audit.json").write_text(json.dumps(rep.as_dict(), indent=2))

    print(f"train → {tp} ({len(train)})  holdout → {hp} ({len(holdout)})")
    print(f"train domain dist: {dict(Counter(r['domain'] for r in train))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
