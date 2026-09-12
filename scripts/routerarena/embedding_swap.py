#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# lint: ra-leakage-exempt tests a representation hypothesis on dev; fits nothing that ships
"""Swap the representation only: hashed bag-of-words -> frozen pretrained embeddings.

Every classifier this project has built was a bag-of-words model fitted from scratch on our own
corpus, so it could only ever learn *our* surface vocabulary. That leaves one hypothesis
untested: maybe the transfer failure is **representational** rather than distributional -- a
pretrained embedding carries semantic structure learned from a far broader distribution than
our 27 datasets, and might encode a notion of question difficulty that survives the move to
RouterArena's mix where lexical cues do not.

If true, the fix is a better representation and a fine-tuned encoder is worth building. If
false, richer models inherit the same bias and the whole training programme is dead. RouteLLM
reports pretrained routers going near-random out-of-distribution, so the prior is poor -- but
that is a reason to spend an afternoon falsifying it, not to skip the test.

Nothing here ships. It changes one variable against numbers we already trust:

* **NMI(cluster ; best model)** -- 0.078 with bag-of-words (E1.3)
* **policy-rank correlation vs dev** -- 0.561, against a 0.887 reliability ceiling (F5)

Embeddings come from a local Ollama model, so this costs nothing and needs no network.

Usage::

    python scripts/routerarena/embedding_swap.py --limit 2000
"""

from __future__ import annotations

import argparse
import collections
import importlib.util
import json
import math
import statistics
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

OLLAMA = "http://localhost:11434"
EMBED_MODEL = "nomic-embed-text:latest"
CACHE = REPO / "data" / "policy" / "embed_cache.jsonl"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


replay = _load("replay", REPO / "scripts" / "routerarena" / "replay.py")
tg = _load("transfer_gate", REPO / "scripts" / "routerarena" / "transfer_gate.py")


def embed_many(texts: list[str]) -> dict[str, list[float]]:
    """Embed with a local model, cached on disk so a re-run is free."""
    cache: dict[str, list[float]] = {}
    if CACHE.exists():
        for line in CACHE.open():
            r = json.loads(line)
            cache[r["k"]] = r["v"]

    import hashlib
    from concurrent.futures import ThreadPoolExecutor

    def key(t: str) -> str:
        return hashlib.sha256(t.encode()).hexdigest()[:16]

    todo = [t for t in texts if key(t) not in cache]
    if todo:
        print(f"  embedding {len(todo)} new texts ({len(texts) - len(todo)} cached)...")

        def one(t: str):
            body = json.dumps({"model": EMBED_MODEL, "prompt": t[:4000]}).encode()
            req = urllib.request.Request(f"{OLLAMA}/api/embeddings", data=body,
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    return t, json.loads(resp.read()).get("embedding")
            except Exception:
                return t, None

        CACHE.parent.mkdir(parents=True, exist_ok=True)
        with CACHE.open("a") as fh, ThreadPoolExecutor(max_workers=8) as ex:
            for i, (t, v) in enumerate(ex.map(one, todo), 1):
                if v:
                    cache[key(t)] = v
                    fh.write(json.dumps({"k": key(t), "v": v}) + "\n")
                if i % 500 == 0:
                    print(f"    {i}/{len(todo)}")
    return {key(t): cache[key(t)] for t in texts if key(t) in cache}


def norm(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=2000,
                    help="external items to use; embedding is the slow part")
    ap.add_argument("--clusters", type=int, default=13)
    args = ap.parse_args()

    import hashlib

    def key(t: str) -> str:
        return hashlib.sha256(t.encode()).hexdigest()[:16]

    # ---- data ---------------------------------------------------------------------------
    ext_prompt, ext_cluster = {}, {}
    for path in sorted((REPO / "data" / "corpus").glob("*.jsonl")):
        if path.name.startswith("_"):
            continue
        for line in path.open():
            it = json.loads(line)
            h = hashlib.sha256(it["prompt"].encode()).hexdigest()[:16]
            ext_prompt[h] = it["prompt"]
            ext_cluster[h] = it["cluster"]

    correct: dict[str, dict[str, float]] = collections.defaultdict(dict)
    cost: dict[str, dict[str, float]] = collections.defaultdict(dict)
    for line in (REPO / "data" / "outcomes" / "cheap_tier.jsonl").open():
        r = json.loads(line)
        if not r.get("error"):
            correct[r["prompt_hash"]][r["model"]] = r["correct"]
            cost[r["prompt_hash"]][r["model"]] = r["cost_usd"]

    models = sorted({m for c in correct.values() for m in c})
    items = [h for h in sorted(correct) if len(correct[h]) == len(models) and h in ext_prompt]
    items = items[: args.limit]

    matrix = replay.load_matrix()
    split = replay.load_split(matrix)
    dev = {k: v for k, v in matrix.items() if split[k] == "dev"}
    print(f"external items: {len(items)} | dev: {len(dev)} | models: {len(models)}\n")

    texts = [ext_prompt[h] for h in items] + [r["prompt"] for r in dev.values()]
    emb = embed_many(texts)
    print(f"  embedded {len(emb)} texts, dim {len(next(iter(emb.values())))}\n")

    def best_model(cells: dict) -> str:
        ok = {m: v for m, v in cells.items() if (v[0] if isinstance(v, tuple) else v) >= 1.0}
        if isinstance(next(iter(cells.values())), tuple):
            pool = ok or cells
            return min(pool, key=lambda m: pool[m][1])
        pool = ok or cells
        return min(pool, key=lambda m: cost[list(cost)[0]].get(m, 0))

    # ---- 1. NMI of an embedding clustering vs the winning model on dev ------------------
    # Build cluster centroids from the external corpus's own labels, then assign dev queries
    # by nearest centroid -- the exact structure the shipped classifier uses, with only the
    # feature space changed.
    cent: dict[str, list[float]] = {}
    by_cluster: dict[str, list[list[float]]] = collections.defaultdict(list)
    for h in items:
        if key(ext_prompt[h]) in emb:
            by_cluster[ext_cluster[h]].append(emb[key(ext_prompt[h])])
    for cl, vecs in by_cluster.items():
        d = len(vecs[0])
        cent[cl] = norm([sum(v[i] for v in vecs) / len(vecs) for i in range(d)])

    assigned, winners = [], []
    for gi, rec in dev.items():
        k = key(rec["prompt"])
        if k not in emb:
            continue
        v = norm(emb[k])
        assigned.append(max(cent, key=lambda c: dot(v, cent[c])))
        ok = {m: val for m, val in rec["models"].items() if val[0] >= 1.0}
        pool = ok or rec["models"]
        winners.append(min(pool, key=lambda m: pool[m][1]))

    mi, nmi = tg_mi(assigned, winners)
    print("1. does an EMBEDDING clustering know which model wins? (dev)")
    print(f"   MI = {mi:.4f} bits, normalised {nmi:.3f}")
    print(f"   bag-of-words baseline (E1.3): MI 0.1129 bits, normalised 0.078")

    # ---- 2. policy-rank correlation, embeddings vs dev ---------------------------------
    # Rank the constants by external Arena, as before -- the representation does not change
    # constants, so this re-confirms the reference point rather than testing it.
    print("\n2. policy-rank correlation vs dev")
    dev_arena = {m: replay.score(dev, replay.policy_always(m))["arena_score"] * 100
                 for m in models}
    ext_arena = {}
    for m in models:
        a = statistics.mean(correct[h][m] for h in items)
        c = statistics.mean(cost[h][m] for h in items) * 1000
        ext_arena[m] = replay.arena_score(a, c) * 100
    rho_const = tg.spearman([ext_arena[m] for m in models], [dev_arena[m] for m in models])
    print(f"   constants: rho = {rho_const:+.3f}  (unchanged by representation, as expected)")

    print("\nVERDICT")
    if nmi >= 0.20:
        print(f"   Embeddings carry materially more signal than bag-of-words "
              f"({nmi:.3f} vs 0.078).")
        print("   The transfer failure is at least partly REPRESENTATIONAL. Fine-tuning an")
        print("   encoder is worth building -- this is the green light.")
    elif nmi >= 0.12:
        print(f"   Modest lift ({nmi:.3f} vs 0.078) -- real but far short of what closing a")
        print("   2.5-point Arena gap would need. Weak signal, not a green light.")
    else:
        print(f"   No material lift ({nmi:.3f} vs 0.078). A pretrained semantic representation")
        print("   knows no more about which model wins than hashed word counts do.")
        print("   'It's representational, not distributional' is FALSIFIED. A fine-tuned")
        print("   encoder would inherit the same bias; the training programme is dead.")
    return 0


def tg_mi(xs: list[str], ys: list[str]) -> tuple[float, float]:
    n = len(xs)
    joint = collections.Counter(zip(xs, ys))
    px, py = collections.Counter(xs), collections.Counter(ys)
    mi = sum((c / n) * math.log2((c / n) / ((px[x] / n) * (py[y] / n)))
             for (x, y), c in joint.items())
    hy = -sum((c / n) * math.log2(c / n) for c in py.values())
    return mi, (mi / hy if hy else 0.0)


if __name__ == "__main__":
    raise SystemExit(main())
