#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build the semantic_classify centroid artifact from a clean labeled corpus.

Enforces the Firewall-v2 ordering: **audit first, build only if clean.** A
corpus that overlaps RouterArena's eval prompts aborts the build — the artifact
can never be produced from contaminated data.

Usage:
    python scripts/build_semantic_centroids.py \
        --labeled data/semantic_train.jsonl \
        --holdout data/semantic_holdout.jsonl \
        --ra-hashes data/ra_eval_hashes.txt \
        --out src/llm_router/data/semantic_centroids.json \
        --audit-out ROUTERARENA_CONTAMINATION_AUDIT.json \
        --k 2

Corpus format (JSONL), one object per line:
    {"prompt": "...", "task_type": "code", "subject": "code"}     # subject optional

RA disjointness proof (either):
    --ra-hashes  path to a newline-delimited file of precomputed RA prompt hashes
                 (preferred for CI — no RA prompt text on disk).
    --ra-prompts path to a JSONL/txt of raw RA eval prompts (hashed here).
    (neither)    by-construction mode — valid ONLY for a purely self-generated
                 corpus; the audit report records the weaker claim.

Embedding backend is selected exactly as at inference time via
LLM_ROUTER_SEMANTIC_CLASSIFIER_BACKEND (ollama default | st). The artifact records
the model so runtime refuses to mix embedding spaces.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Make `llm_router` importable when run from the repo root without install.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from llm_router.contamination_audit import audit  # noqa: E402
from llm_router.semantic_centroids import LabeledPrompt, build_artifact  # noqa: E402


def _load_jsonl(path: Path) -> list[LabeledPrompt]:
    out: list[LabeledPrompt] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        out.append(
            LabeledPrompt(
                prompt=obj["prompt"],
                task_type=obj["task_type"],
                subject=obj.get("subject"),
            )
        )
    return out


def _load_ra_hashes(args) -> set[str] | None:
    if args.ra_hashes:
        return {
            h.strip()
            for h in Path(args.ra_hashes).read_text(encoding="utf-8").splitlines()
            if h.strip()
        }
    if args.ra_prompts:
        from llm_router.contamination_audit import hash_set

        raw = Path(args.ra_prompts).read_text(encoding="utf-8").splitlines()
        prompts = []
        for line in raw:
            line = line.strip()
            if not line:
                continue
            try:  # tolerate both JSONL {"prompt": ...} and bare text
                prompts.append(json.loads(line).get("prompt", line))
            except json.JSONDecodeError:
                prompts.append(line)
        return hash_set(prompts)
    return None


def _embed_fn():
    """Bind the same embedding path inference uses, resolving the model name."""
    import os

    from llm_router import semantic_classify as sc

    backend = os.getenv("LLM_ROUTER_SEMANTIC_CLASSIFIER_BACKEND", "ollama").strip().lower()
    model = (
        os.getenv("LLM_ROUTER_SEMANTIC_ST_MODEL", "all-MiniLM-L6-v2")
        if backend in ("st", "sentence-transformers")
        else sc._OLLAMA_EMBED_MODEL
    )

    def embed(text: str):
        return sc._embed(text, model)

    return embed, model


def main() -> int:
    ap = argparse.ArgumentParser(description="Build semantic_classify centroids.")
    ap.add_argument("--labeled", required=True, type=Path)
    ap.add_argument("--holdout", type=Path)
    ap.add_argument("--ra-hashes", dest="ra_hashes")
    ap.add_argument("--ra-prompts", dest="ra_prompts")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--audit-out", type=Path)
    ap.add_argument("--k", type=int, default=1)
    ap.add_argument(
        "--allow-by-construction",
        action="store_true",
        help="Permit building with no RA hashes (self-generated corpus only).",
    )
    args = ap.parse_args()

    labeled = _load_jsonl(args.labeled)
    holdout = _load_jsonl(args.holdout) if args.holdout else None
    print(f"loaded {len(labeled)} labeled prompts"
          f"{f', {len(holdout)} holdout' if holdout else ''}")

    # 1. AUDIT FIRST — build is forbidden on contaminated or unproven data.
    ra_hashes = _load_ra_hashes(args)
    all_prompts = [ex.prompt for ex in labeled] + [ex.prompt for ex in (holdout or [])]
    report = audit(all_prompts, ra_hashes=ra_hashes)
    print(f"audit: mode={report.mode} clean={report.clean} "
          f"overlap={report.overlap_count}/{report.unique_train_hashes}")

    if args.audit_out:
        args.audit_out.write_text(json.dumps(report.as_dict(), indent=2), encoding="utf-8")
        print(f"audit report → {args.audit_out}")

    if not report.clean:
        print(
            f"ABORT: {report.overlap_count} prompt(s) overlap RouterArena eval set. "
            "Contaminated corpus — refusing to build.",
            file=sys.stderr,
        )
        return 2
    if report.mode == "by_construction" and not args.allow_by_construction:
        print(
            "ABORT: no RA hashes supplied. Pass --ra-hashes/--ra-prompts to prove "
            "disjointness, or --allow-by-construction for a self-generated corpus.",
            file=sys.stderr,
        )
        return 3

    # 2. BUILD
    embed, model = _embed_fn()
    provenance = [f"labeled:{args.labeled.name}"]
    if args.ra_hashes or args.ra_prompts:
        provenance.append("ra-disjointness:audited")
    else:
        provenance.append("ra-disjointness:by-construction")

    artifact = build_artifact(
        labeled,
        embed,
        embedding_model=model,
        k=args.k,
        holdout=holdout,
        audit_report=report.as_dict(),
        provenance_sources=provenance,
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    print(
        f"artifact → {args.out}  (dim={artifact['dim']}, "
        f"T={artifact['temperature']}, floor={artifact['confidence_floor']}, "
        f"task_classes={len(artifact['task_type'])}, "
        f"subject_classes={len(artifact['subject'])})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
