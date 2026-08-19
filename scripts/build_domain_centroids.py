#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Stage 1: build the domain-centroid artifact from the hash-audited MMLU-Pro
corpus. Reuses the existing prototype + calibration primitives; the only reason
this isn't `build_semantic_centroids.py` is that domains are a free-form 14-label
taxonomy, not the enum-validated task_type/subject heads (so #130's production
classifier stays untouched).

Output: src/llm_router/data/domain_centroids.json — consumed by llm_router_domain_router.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from llm_router.semantic_centroids import (  # noqa: E402
    _kmeans, build_prototype, calibrate_floor, calibrate_temperature,
)
from llm_router.semantic_classify import _embed, _norm  # noqa: E402

_MODEL = "nomic-embed-text"


def _load(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def _embed_all(rows: list[dict]) -> list[tuple[list[float], str]]:
    out, n = [], len(rows)
    for i, r in enumerate(rows):
        v = _embed(r["prompt"], _MODEL)
        if v:
            out.append((_norm(v), r["domain"]))
        if (i + 1) % 300 == 0:
            print(f"  embedded {i+1}/{n}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", type=Path, default=Path("src/llm_router/data"))
    ap.add_argument("--k", type=int, default=2, help="prototypes per domain")
    ap.add_argument("--out", type=Path, default=Path("src/llm_router/data/domain_centroids.json"))
    args = ap.parse_args()

    train = _load(args.data_dir / "domain_train.jsonl")
    holdout = _load(args.data_dir / "domain_holdout.jsonl")
    print(f"embedding {len(train)} train + {len(holdout)} holdout via {_MODEL}...")

    tr = _embed_all(train)
    ho = _embed_all(holdout)
    if not tr:
        print("no embeddings — is Ollama up with nomic-embed-text?", file=sys.stderr)
        return 1
    dim = len(tr[0][0])

    grouped: dict[str, list[list[float]]] = defaultdict(list)
    for vec, dom in tr:
        grouped[dom].append(vec)
    protos = {dom: _kmeans(vs, args.k) for dom, vs in grouped.items() if vs}

    temperature = calibrate_temperature(ho, protos) if ho else 12.0
    floor = calibrate_floor(ho, protos, temperature) if ho else 0.4

    audit = {}
    ap_ = args.data_dir / "domain_train_audit.json"
    if ap_.is_file():
        audit = json.loads(ap_.read_text())

    artifact = {
        "version": "1",
        "embedding_model": _MODEL,
        "dim": dim,
        "temperature": round(temperature, 4),
        "confidence_floor": round(floor, 4),
        "domain": protos,
        "provenance": {
            "source": "MMLU-Pro test (TIGER-Lab/MMLU-Pro), hash-audited disjoint from RA eval",
            "label": "domain (public category); never RA answers/accuracy",
            "k": args.k, "n_train": len(tr), "n_holdout": len(ho),
            "audit": audit or {"note": "see domain_train_audit.json"},
        },
    }
    args.out.write_text(json.dumps(artifact), encoding="utf-8")
    print(f"artifact → {args.out}  (dim={dim}, T={artifact['temperature']}, "
          f"floor={artifact['confidence_floor']}, domains={len(protos)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
