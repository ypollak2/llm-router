#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Generate the RouterArena prediction file: call each query's routed model, record the answer.

The router has already decided *which* model handles each query, offline and deterministically.
This step only executes those decisions and records what came back, because RouterArena's
submission format wants ``generated_result`` populated alongside the model choice.

Two things are load-bearing here and both are lessons from earlier runs in this project:

* **Resumable by construction.** Every result is appended and flushed immediately, and a
  restart skips whatever is already on disk. A sweep that dies at 80% and has to start over
  costs the money twice.
* **A hard budget ceiling that aborts rather than warns.** By the time a human reads a warning
  the money is already spent. Both pool models are terse (63 and ~30 output tokens uncapped),
  so the expected bill is well under a dollar -- the ceiling exists for the case where that
  assumption is wrong.

Usage::

    OPENROUTER_API_KEY=... python scripts/routerarena/run_inference_full.py \
        --picks /tmp/picks_main.json --prompts /tmp/full_prompts.json --out /tmp/generated.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# RouterArena's own published prices, so our cost accounting matches how we will be scored.
PRICE = {
    "google/gemini-3.1-flash-lite-preview": (0.1, 0.4),
    "qwen/qwen3-235b-a22b-2507": (0.071, 0.1),
}
ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"


def call_one(gi: str, prompt: str, model: str, key: str, retries: int = 3) -> dict:
    """One completion, with backoff. A failed call is recorded, never silently dropped."""
    delay = 2.0
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                ENDPOINT,
                data=json.dumps({
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 4096,
                }).encode(),
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=240) as resp:
                d = json.loads(resp.read())
            text = d["choices"][0]["message"]["content"] or ""
            u = d.get("usage") or {}
            pin, pout = PRICE.get(model, (0.0, 0.0))
            cost = (u.get("prompt_tokens", 0) * pin + u.get("completion_tokens", 0) * pout) / 1e6
            return {
                "gi": gi, "model": model, "text": text,
                "usage": {"input_tokens": u.get("prompt_tokens", 0),
                          "output_tokens": u.get("completion_tokens", 0)},
                "cost": cost,
            }
        except Exception as exc:  # noqa: BLE001 - one bad call must not end the run
            if attempt == retries:
                return {"gi": gi, "model": model, "text": "", "usage": {}, "cost": 0.0,
                        "error": f"{type(exc).__name__}: {exc}"[:160]}
            time.sleep(delay)
            delay *= 2
    raise RuntimeError("unreachable")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--picks", default="/tmp/picks_main.json")
    ap.add_argument("--prompts", default="/tmp/full_prompts.json")
    ap.add_argument("--key", default="main", help="which prompt set: main or rob")
    ap.add_argument("--out", default="/tmp/generated.jsonl")
    ap.add_argument("--workers", type=int, default=24)
    ap.add_argument("--budget", type=float, default=5.0)
    args = ap.parse_args()

    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        raise SystemExit("OPENROUTER_API_KEY is not set")

    prompts = json.loads(Path(args.prompts).read_text())[args.key]
    picks = json.loads(Path(args.picks).read_text())

    out_path = Path(args.out)
    done: set[str] = set()
    if out_path.exists():
        for line in out_path.open():
            try:
                done.add(json.loads(line)["gi"])
            except Exception:
                pass

    todo = [(gi, prompts[gi], picks[gi]) for gi in picks if gi not in done and gi in prompts]
    print(f"{len(todo)} calls to make ({len(done)} already done), {args.workers} workers, "
          f"budget ceiling ${args.budget:.2f}\n", flush=True)

    lock = threading.Lock()
    spend = 0.0
    n = 0
    t0 = time.time()
    aborted = threading.Event()

    with out_path.open("a") as fh, ThreadPoolExecutor(max_workers=args.workers) as pool:
        def work(item):
            if aborted.is_set():
                return None
            return call_one(item[0], item[1], item[2], api_key)

        for res in pool.map(work, todo):
            if res is None:
                continue
            with lock:
                spend += res.get("cost", 0.0)
                n += 1
                fh.write(json.dumps(res) + "\n")
                fh.flush()
                if spend > args.budget and not aborted.is_set():
                    aborted.set()
                    print(f"\n!! BUDGET CEILING ${args.budget:.2f} REACHED at ${spend:.4f} "
                          f"after {n} calls -- aborting", flush=True)
                if n % 500 == 0:
                    rate = n / max(time.time() - t0, 1)
                    eta = (len(todo) - n) / max(rate, 1e-6) / 60
                    print(f"  {n}/{len(todo)}  ${spend:.4f}  {rate:.1f}/s  eta {eta:.0f}m",
                          flush=True)

    errors = 0
    empty = 0
    for line in out_path.open():
        r = json.loads(line)
        if r.get("error"):
            errors += 1
        elif not (r.get("text") or "").strip():
            empty += 1
    print(f"\nDONE {n} calls  ${spend:.4f}  errors {errors}  empty {empty}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
