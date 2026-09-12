#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Run one model over every full-split prompt, for measurement rather than submission.

Built for candidates that cannot be submitted -- a model absent from RouterArena's price table
is graded as failed inference no matter how well it answers -- but whose accuracy is still worth
knowing before writing one off. Free preview endpoints are the usual case, and they throttle
hard, so this trades throughput for completion: few workers, exponential backoff to a minute,
and every result flushed on write so an interrupted run resumes exactly where it stopped.

Reads prompts only. No answer key, no cost model, no routing.

Usage::

    OPENROUTER_API_KEY=... python scripts/routerarena/run_single_model.py \
        --model stealth/ox-alpha --out /tmp/ox_full.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", required=True)
    ap.add_argument("--prompts", default="/tmp/full_prompts.json")
    ap.add_argument("--key", default="main")
    ap.add_argument("--out", required=True)
    ap.add_argument("--workers", type=int, default=6,
                    help="low by default: free endpoints rate-limit aggressively")
    ap.add_argument("--max-tokens", type=int, default=4096)
    args = ap.parse_args()

    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        raise SystemExit("OPENROUTER_API_KEY is not set")

    prompts = json.loads(Path(args.prompts).read_text())[args.key]
    out_path = Path(args.out)
    done: set[str] = set()
    if out_path.exists():
        for line in out_path.open():
            try:
                done.add(json.loads(line)["gi"])
            except Exception:
                pass
    todo = [(gi, prompts[gi]) for gi in prompts if gi not in done]
    print(f"{len(todo)} calls ({len(done)} already done), {args.workers} workers", flush=True)

    lock = threading.Lock()
    n = [0]
    throttled = [0]
    t0 = time.time()

    def call(item):
        gi, prompt = item
        delay = 3.0
        for attempt in range(6):
            try:
                req = urllib.request.Request(
                    ENDPOINT,
                    data=json.dumps({
                        "model": args.model,
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": args.max_tokens,
                    }).encode(),
                    headers={"Authorization": f"Bearer {api_key}",
                             "Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=300) as resp:
                    d = json.loads(resp.read())
                u = d.get("usage") or {}
                return {
                    "gi": gi, "model": args.model,
                    "text": d["choices"][0]["message"]["content"] or "",
                    "usage": {"input_tokens": u.get("prompt_tokens", 0),
                              "output_tokens": u.get("completion_tokens", 0)},
                }
            except Exception as exc:  # noqa: BLE001
                if "429" in str(exc):
                    with lock:
                        throttled[0] += 1
                if attempt == 5:
                    return {"gi": gi, "model": args.model, "text": "", "usage": {},
                            "error": f"{type(exc).__name__}: {exc}"[:140]}
                time.sleep(delay)
                delay = min(delay * 1.8, 60.0)
        raise RuntimeError("unreachable")

    with out_path.open("a") as fh, ThreadPoolExecutor(max_workers=args.workers) as pool:
        for res in pool.map(call, todo):
            with lock:
                n[0] += 1
                fh.write(json.dumps(res) + "\n")
                fh.flush()
                if n[0] % 250 == 0:
                    rate = n[0] / max(time.time() - t0, 1)
                    eta = (len(todo) - n[0]) / max(rate, 1e-6) / 60
                    print(f"  {n[0]}/{len(todo)}  {rate:.2f}/s  429s={throttled[0]}  "
                          f"eta {eta:.0f}m", flush=True)

    rows = [json.loads(x) for x in out_path.open()]
    errors = sum(1 for r in rows if r.get("error"))
    empty = sum(1 for r in rows if not r.get("error") and not (r.get("text") or "").strip())
    print(f"\nDONE {len(rows)} rows | errors {errors} | empty {empty} | "
          f"429 retries {throttled[0]}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
