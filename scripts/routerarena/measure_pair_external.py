#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Measure two models on the external corpus under the *full-split* answer contract.

``label_outcomes.py`` measures the same corpus under tight per-cluster caps -- 8 tokens for a
multiple-choice answer -- which is correct for terse models and catastrophic for verbose ones.
A model that reasons in prose before answering scores near zero under an 8-token cap not
because it is wrong but because it is cut off mid-thought. That artifact is what made an early
probe report 24% for a model that actually scores 78%.

So this script measures under the contract the RouterArena submission actually uses: the
``\\boxed{}`` instruction and a generous token budget. The absolute accuracies are therefore
not comparable to ``trackf.jsonl``; they are comparable *to each other*, which is all a
divert map between two models needs.

Nothing here reads RouterArena. The outcomes this writes are the only evidence permitted to
choose a routing policy -- see ``scripts/lint_ra_leakage.py``.

Usage::

    OPENROUTER_API_KEY=... python scripts/routerarena/measure_pair_external.py \\
        --models google/gemini-3-flash-preview,stealth/ox-alpha \\
        --per-cluster 200 --out data/outcomes/pair_boxed.jsonl
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import random
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from label_outcomes import (  # noqa: E402
    CONTRACTS, DEFAULT_CONTRACT, GRADERS, resolve_grader, strip_thinking,
)

CORPUS = REPO / "data" / "corpus"
ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"

# The RouterArena zero-shot templates all end by demanding the answer inside \boxed{}, and the
# harness extractor looks for it. Mirroring that here is what makes these outcomes predictive
# of full-split behaviour rather than of our own prompt style.
BOXED = ("\n\nPut the final answer in \\boxed{{X}}, where X is the answer. "
         "Keep any explanation within 3 sentences.")
BOXED_CODE = ("\n\nReply with a single Python function in a fenced code block.")


def load_sample(per_cluster: int, seed: int) -> list[dict]:
    by_cluster = collections.defaultdict(list)
    for path in sorted(CORPUS.glob("*.jsonl")):
        if path.name.startswith("_"):
            continue
        for line in path.open():
            it = json.loads(line)
            by_cluster[it["cluster"]].append(it)
    rng = random.Random(seed)
    out = []
    for cluster, items in sorted(by_cluster.items()):
        rng.shuffle(items)
        out.extend(items[:per_cluster])
    rng.shuffle(out)
    return out


def unbox(text: str) -> str:
    """Prefer the boxed span; fall back to the whole reply so a model that ignores the
    instruction is graded on what it did say rather than scored zero for formatting."""
    t = strip_thinking(text or "")
    i = t.rfind("\\boxed{")
    if i == -1:
        return t
    j, depth = i + 7, 1
    while j < len(t) and depth:
        depth += (t[j] == "{") - (t[j] == "}")
        j += 1
    return t[i + 7:j - 1] if depth == 0 else t[i + 7:]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--models", required=True, help="comma-separated")
    ap.add_argument("--per-cluster", type=int, default=200)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--max-tokens", type=int, default=2048)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        raise SystemExit("OPENROUTER_API_KEY is not set")

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    items = load_sample(args.per_cluster, args.seed)
    print(f"{len(items)} items x {len(models)} models", flush=True)

    done = set()
    if args.out.exists():
        for line in args.out.open():
            try:
                r = json.loads(line)
                done.add((r["idx"], r["model"]))
            except Exception:
                pass
    jobs = [(i, it, m) for i, it in enumerate(items) for m in models if (i, m) not in done]
    print(f"{len(jobs)} calls ({len(done)} already done)", flush=True)

    lock = threading.Lock()
    n = [0]
    t0 = time.time()

    def run(job):
        i, item, model = job
        contract = CONTRACTS.get(item["cluster"], DEFAULT_CONTRACT)
        suffix = BOXED_CODE if contract.grader == "code_exec" else BOXED
        delay = 3.0
        for attempt in range(6):
            try:
                req = urllib.request.Request(
                    ENDPOINT,
                    data=json.dumps({
                        "model": model,
                        "messages": [{"role": "user", "content": item["prompt"] + suffix}],
                        "max_tokens": args.max_tokens,
                    }).encode(),
                    headers={"Authorization": f"Bearer {key}",
                             "Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=300) as resp:
                    d = json.loads(resp.read())
                text = d["choices"][0]["message"]["content"] or ""
                u = d.get("usage") or {}
                break
            except Exception as exc:  # noqa: BLE001
                if attempt == 5:
                    return {"idx": i, "model": model, "cluster": item["cluster"],
                            "source": item["source"], "correct": 0.0, "error": str(exc)[:120],
                            "in_tokens": 0, "out_tokens": 0}
                time.sleep(delay)
                delay = min(delay * 1.8, 60.0)

        grader = resolve_grader(contract.grader, item)
        answer = text if grader == "code_exec" else unbox(text)
        try:
            score = float(GRADERS[grader](answer, item["answer"], item))
        except Exception:
            score = 0.0
        return {"idx": i, "model": model, "cluster": item["cluster"], "source": item["source"],
                "correct": score, "grader": grader, "error": None,
                "in_tokens": u.get("prompt_tokens", 0), "out_tokens": u.get("completion_tokens", 0)}

    with args.out.open("a") as fh, ThreadPoolExecutor(max_workers=args.workers) as pool:
        for res in pool.map(run, jobs):
            with lock:
                n[0] += 1
                fh.write(json.dumps(res) + "\n")
                fh.flush()
                if n[0] % 200 == 0:
                    rate = n[0] / max(time.time() - t0, 1)
                    print(f"  {n[0]}/{len(jobs)}  {rate:.2f}/s  "
                          f"eta {(len(jobs) - n[0]) / max(rate, 1e-6) / 60:.0f}m", flush=True)

    rows = [json.loads(x) for x in args.out.open()]
    print()
    for m in models:
        mine = [r for r in rows if r["model"] == m]
        if not mine:
            continue
        err = sum(1 for r in mine if r.get("error"))
        acc = sum(r["correct"] for r in mine) / len(mine)
        tok = sum(r["out_tokens"] for r in mine) / max(len(mine), 1)
        print(f"{m:<42}{acc * 100:>7.2f}%  n={len(mine)}  errors={err}  mean_out={tok:.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
