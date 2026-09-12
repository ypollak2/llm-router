#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""G1.1 — the model screening list: what we are allowed to route to and have not yet measured.

Our Arena score *is* the best constant, and constants are the one thing that transfers between
our corpus and RouterArena (Spearman +0.800, against +0.561 for policies). So the highest-value
unexplored lever is simply which models are in the pool -- and we have measured 8 of 91.

Two hard constraints define the list, and both matter:

1. **RouterArena must be able to price it.** The harness computes cost from its own
   ``model_cost.json``; a model missing from that table has no cost, and an entry that cannot
   be costed is treated as failed inference and scored *wrong* (their issue #135). Routing to
   an unpriced model is therefore not a cheap gamble, it is a guaranteed zero.
2. **OpenRouter must actually serve it.** The price table carries names that are not OpenRouter
   ids (`gpt-5-nano`, `glm-4-air-250414`, `agnes-2.0-flash`), so each candidate is resolved
   against the live catalogue rather than assumed.

Anything failing either test is excluded *here*, with a reason, rather than silently scoring
badly in a sweep later.

Usage::

    python scripts/routerarena/build_screen_list.py
    python scripts/routerarena/build_screen_list.py --max-output-price 3.0
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PRICES = REPO / "data" / "benchmarks" / "model_cost.json"
OUT = REPO / "data" / "policy" / "screen_list.json"
CATALOGUE_CACHE = Path("/tmp/or_models.json")

# Already measured across cheap_tier.jsonl and trackf.jsonl.
# Kept in the screen on purpose: measuring the incumbent inside the same run, on the same
# items under the same conditions, turns the comparison into a controlled one. Otherwise a
# candidate's margin over it would be confounded with whatever differed between two sweeps.
INCUMBENT = "google/gemini-3.1-flash-lite-preview"

MEASURED = {
    "google/gemini-3.1-flash-lite-preview", "qwen/qwen3-235b-a22b-2507",
    "deepseek/deepseek-v4-flash", "qwen/qwen3-30b-a3b-instruct-2507",
    "mistralai/ministral-8b-2512", "openai/gpt-oss-120b", "qwen/qwen3.5-9b",
    "qwen/qwen3.5-flash-02-23",
}


def catalogue() -> dict[str, dict]:
    """Live OpenRouter models, by id."""
    if not CATALOGUE_CACHE.exists():
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/models",
            headers={"Authorization": f"Bearer {os.environ.get('OPENROUTER_API_KEY', '')}"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            CATALOGUE_CACHE.write_bytes(resp.read())
    return {m["id"]: m for m in json.loads(CATALOGUE_CACHE.read_text())["data"]}


def resolve(name: str, live: dict[str, dict]) -> str | None:
    """Map a RouterArena price-table name onto a live OpenRouter id, or None.

    Exact match first, then a vendor-prefixed guess, then a unique suffix match. A *unique*
    suffix match only -- if two live ids end the same way there is no way to tell which one the
    price row refers to, and guessing would mean pricing one model and calling another.
    """
    if name in live:
        return name
    slug = name.split("/")[-1]
    exact_suffix = [i for i in live if i.split("/")[-1] == slug]
    if len(exact_suffix) == 1:
        return exact_suffix[0]
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max-output-price", type=float, default=3.0,
                    help="$/M output tokens; 3.0 keeps the mid-tier in scope for G5")
    args = ap.parse_args()

    prices = json.loads(PRICES.read_text())
    live = catalogue()

    keep, skipped = [], {"already_measured": [], "too_expensive": [], "not_on_openrouter": []}
    for name, p in prices.items():
        out_price = p.get("output_token_price_per_million")
        in_price = p.get("input_token_price_per_million")
        if out_price is None or in_price is None:
            skipped["not_on_openrouter"].append(f"{name} (no price)")
            continue
        if name in MEASURED:
            skipped["already_measured"].append(name)
            continue
        if out_price > args.max_output_price:
            skipped["too_expensive"].append(f"{name} (${out_price}/M)")
            continue
        resolved = resolve(name, live)
        if resolved is None:
            skipped["not_on_openrouter"].append(name)
            continue
        # The price table lists some models without a vendor prefix, so the MEASURED check
        # above misses them until the id is resolved. Check again on the resolved id --
        # except for the incumbent, which is kept deliberately (see below).
        if resolved in MEASURED and resolved != INCUMBENT:
            skipped["already_measured"].append(f"{name} -> {resolved}")
            continue
        keep.append({
            "price_table_name": name,
            "openrouter_id": resolved,
            "input_price_per_m": in_price,
            "output_price_per_m": out_price,
        })

    # Two price-table rows can resolve to the same live id; screening it twice wastes budget.
    seen, deduped = set(), []
    for c in keep:
        if c["openrouter_id"] in seen:
            continue
        seen.add(c["openrouter_id"])
        deduped.append(c)

    for c in deduped:
        c["role"] = "incumbent control" if c["openrouter_id"] == INCUMBENT else "candidate"
    deduped.sort(key=lambda c: c["output_price_per_m"])
    print(f"{'openrouter id':<48}{'in $/M':>9}{'out $/M':>10}   price-table name")
    for c in deduped:
        alias = "" if c["openrouter_id"] == c["price_table_name"] else c["price_table_name"]
        tag = "  <-- INCUMBENT CONTROL" if c["role"] != "candidate" else ""
        print(f"{c['openrouter_id']:<48}{c['input_price_per_m']:>9}"
              f"{c['output_price_per_m']:>10}   {alias}{tag}")

    print(f"\n{len(deduped)} screenable candidates "
          f"(priced by RouterArena AND served by OpenRouter, output <= ${args.max_output_price}/M)")
    for reason, names in skipped.items():
        print(f"  skipped, {reason}: {len(names)}")
        if reason == "not_on_openrouter" and names:
            print(f"    {', '.join(sorted(names)[:8])}{' ...' if len(names) > 8 else ''}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(
        {"candidates": deduped, "skipped": skipped,
         "max_output_price": args.max_output_price,
         "note": "A model absent from RouterArena's model_cost.json cannot be costed by their "
                 "harness and is scored as failed inference, so it is excluded here rather "
                 "than discovered later."},
        indent=1))
    print(f"\nwrote {OUT}")
    n_new = sum(1 for c in deduped if c["role"] == "candidate")
    print(f"  of which genuinely unmeasured: {n_new}")
    gate = len(deduped) >= 25
    print(f"GATE (>=25 candidates): {'PASS' if gate else 'FAIL'} ({len(deduped)})")
    return 0 if gate else 1


if __name__ == "__main__":
    raise SystemExit(main())
