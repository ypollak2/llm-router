#!/usr/bin/env python3
"""P1.4-a live measurement: second call of each pair must read the cache.

Sends N pairs of Anthropic Messages API calls that share the stable prefix
built by `llm_router.cache_layout.build_layout` and differ only in the final
request. PASS for a pair when call 2 reports ``cache_read_input_tokens > 0``.
Gate: 10/10 (PLAN v16 P1.4-a). Each pair uses a unique prefix salt, so a hit on
call 2 can only come from call 1 of the same pair (isolated, no cross-pair
reuse). Needs ANTHROPIC_API_KEY. SPENDS REAL MONEY (~20 calls; call 1 writes the
prefix). `--dry-run` prints the request shapes and spends nothing.

    uv run python scripts/bench/cache_layout_live.py --pairs 10 --model claude-sonnet-4-5
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid

import httpx

from llm_router import cache_layout
from llm_router.proxy.cache_cost import context_tokens

os.environ.setdefault("LLM_ROUTER_SYNTHETIC", "1")  # benchmark traffic, not production spend

API = "https://api.anthropic.com/v1/messages"
FILLER = ("Project rule: keep functions small, test first, name evidence for every rule, "
          "never edit generated files, and state what was verified. ")


def build(pair_salt: str, request: str, *, filler_repeats: int):
    def block(tag: str) -> str:
        return f"[{tag} {pair_salt}] " + FILLER * filler_repeats

    recent = [{"role": "user", "content": f"earlier question {pair_salt}"},
              {"role": "assistant", "content": f"earlier answer {pair_salt}"}]
    return cache_layout.build_layout(request, instructions=block("instructions"),
                                     knowledge=block("knowledge"), recent=recent)


def call(client: httpx.Client, key: str, model: str, lay: cache_layout.CacheLayout) -> dict:
    r = client.post(API, headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                    json={"model": model, "max_tokens": 16, "system": lay.system,
                          "messages": lay.messages}, timeout=60)
    r.raise_for_status()
    return r.json().get("usage", {})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--pairs", type=int, default=10)
    ap.add_argument("--model", default="claude-sonnet-4-5")
    ap.add_argument("--filler-repeats", type=int, default=250,
                    help="~5k+ tokens of stable prefix, above every model's cache minimum")
    ap.add_argument("--gap-s", type=float, default=1.0, help="pause between call 1 and 2")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if a.dry_run:
        lay = build("dry", "request", filler_repeats=2)
        print(json.dumps({"system": lay.system, "messages": lay.messages,
                          "breakpoints": lay.breakpoints()}, indent=1))
        return 0
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        print("ANTHROPIC_API_KEY not set", file=sys.stderr)
        return 2

    run = uuid.uuid4().hex[:8]
    rows = []
    with httpx.Client() as client:
        for i in range(a.pairs):
            salt = f"{run}-{i}"
            u1 = call(client, key, a.model, build(salt, "first question", filler_repeats=a.filler_repeats))
            time.sleep(a.gap_s)
            u2 = call(client, key, a.model, build(salt, "a different second question", filler_repeats=a.filler_repeats))
            hit = int(u2.get("cache_read_input_tokens") or 0)
            rows.append({"pair": i, "write1": u1.get("cache_creation_input_tokens"),
                         "read2": hit, "ctx2": context_tokens(u2), "pass": hit > 0})
            print(json.dumps(rows[-1]), flush=True)
    passed = sum(r["pass"] for r in rows)
    print(f"P1.4-a: {passed}/{len(rows)} pairs with cache_read_input_tokens > 0 "
          f"(model={a.model}, run={run}, N={len(rows)})")
    return 0 if len(rows) and passed == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
