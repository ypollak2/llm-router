#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Execute a frozen divert policy over the full split and emit a prediction file.

This file reads RouterArena data and fits nothing. Every parameter it uses -- the task-type
classifier, the divert set, the gap threshold -- is read from the policy JSON that
``fit_divert_map.py`` produced from external evidence. That split is deliberate: a single file
that both fit and evaluated would be indistinguishable from tuning on the benchmark, however
carefully it was written.

The router decides from the prompt alone, before seeing any output, and each query's answer is
the one that model already produced in its own full-split pass. So the score below is what the
router would have earned had it run live -- no re-inference, no cherry-picking, no cost.

Usage::

    python scripts/routerarena/apply_divert_router.py \\
        --policy data/policy/divert_policy.json \\
        --base-gen /tmp/generated4.jsonl --alt-gen /tmp/ox_full.jsonl \\
        --name llm-router-divert
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import re
import zlib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
WORD = re.compile(r"[a-z0-9_]+")

# Price keys as RouterArena's model_cost.json spells them, not as the provider does.
PRICE_NAME = {
    "google/gemini-3-flash-preview": "gemini-3-flash-preview",
    "stealth/ox-alpha": "stealth/ox-alpha",
}
PROVIDER = {"google/gemini-3-flash-preview": "google", "stealth/ox-alpha": "openrouter"}


class FrozenNB:
    def __init__(self, blob: dict) -> None:
        self.classes = blob["classes"]
        self.logprior = blob["logprior"]
        self.total = blob["total"]
        self.counts = {c: {int(k): v for k, v in d.items()} for c, d in blob["counts"].items()}
        self.nbuckets = blob["nbuckets"]

    def featurize(self, text: str) -> list[int]:
        return [zlib.crc32(t.encode()) % self.nbuckets
                for t in WORD.findall(text.lower())[:400]]

    def predict(self, text: str) -> str:
        feats = self.featurize(text)
        best, best_score = self.classes[0], -1e18
        for c in self.classes:
            cnt, tot = self.counts[c], self.total[c] + self.nbuckets
            s = self.logprior[c]
            for f in feats:
                s += math.log((cnt.get(f, 0) + 1) / tot)
            if s > best_score:
                best, best_score = c, s
        return best


def load_gen(path: str) -> dict[str, dict]:
    out = {}
    for line in Path(path).open():
        r = json.loads(line)
        if not r.get("error"):
            out[r["gi"]] = r
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--policy", default="data/policy/divert_policy.json")
    ap.add_argument("--prompts", default="/tmp/full_prompts.json")
    ap.add_argument("--base-gen", required=True)
    ap.add_argument("--alt-gen", required=True)
    ap.add_argument("--name", default="llm-router-divert")
    ap.add_argument("--harness", default=str(Path.home() / ".llm-router/harness/RouterArena"))
    ap.add_argument("--price-alt-as", default=None,
                    help="label alt-model rows with this priced name so the harness will "
                         "grade them at all. For local measurement only: an unpriced model is "
                         "skipped entirely and scored as failed inference, which measures the "
                         "price table rather than the router. Accuracy is computed from the "
                         "answer text and is unaffected; the COST and Arena figures such a run "
                         "prints are meaningless and must not be reported as a router score.")
    args = ap.parse_args()

    policy = json.loads(Path(args.policy).read_text())
    nb = FrozenNB(policy["classifier"])
    divert = set(policy["divert_clusters"])
    base, alt = policy["base_model"], policy["alt_model"]
    if args.price_alt_as:
        PRICE_NAME[alt] = args.price_alt_as
        print(f"MEASUREMENT MODE: {alt} rows labelled '{args.price_alt_as}'. "
              f"Read ACCURACY only -- cost and Arena from this run are not real.")
    print(f"policy: divert {sorted(divert) or '(none)'} -> {alt}, else {base}")

    P = json.load(open(args.prompts))
    base_gen, alt_gen = load_gen(args.base_gen), load_gen(args.alt_gen)

    main_rows, picks = [], collections.Counter()
    fallbacks = 0
    for gi, prompt in P["main"].items():
        cluster = nb.predict(prompt)
        want = alt if cluster in divert else base
        gen = (alt_gen if want is alt else base_gen).get(gi)
        # A router cannot route to a model that produced nothing; falling back to the other
        # model is what a live router would do, and pretending otherwise would flatter the score.
        if gen is None or not (gen.get("text") or "").strip():
            other = base if want == alt else alt
            alt_g = (alt_gen if other == alt else base_gen).get(gi)
            if alt_g is not None and (alt_g.get("text") or "").strip():
                want, gen = other, alt_g
                fallbacks += 1
        if gen is None:
            continue
        picks[want] += 1
        u = gen.get("usage") or {}
        ot = u.get("output_tokens", 0)
        txt = gen.get("text") or ""
        ok = bool(txt.strip()) and ot > 0
        main_rows.append({
            "global index": gi, "prompt": prompt, "prediction": PRICE_NAME[want],
            "generated_result": {
                "generated_answer": txt, "success": ok,
                "token_usage": {"input_tokens": u.get("input_tokens", 0), "output_tokens": ot,
                                "total_tokens": u.get("input_tokens", 0) + ot},
                "provider": PROVIDER[want], "error": None if ok else "empty",
                "model_version": PRICE_NAME[want]},
            "cost": None, "accuracy": None, "for_optimality": False})

    total = sum(picks.values())
    print(f"rows {len(main_rows)} | fallbacks {fallbacks}")
    for m, n in picks.most_common():
        print(f"  {m:<42}{n:>6}  {n / max(total, 1) * 100:5.1f}%")

    H = Path(args.harness)
    (H / "router_inference" / "predictions").mkdir(parents=True, exist_ok=True)
    (H / "router_inference" / "config").mkdir(parents=True, exist_ok=True)
    json.dump(main_rows, (H / "router_inference" / "predictions" / f"{args.name}.json").open("w"))
    cfg = {"pipeline_params": {"router_name": args.name, "router_cls_name": "DivertRouter",
                               "models": sorted({PRICE_NAME[base], PRICE_NAME[alt]}),
                               "description": policy["provenance"]},
           "router": args.name, "router_name": args.name,
           "description": "task-type divert router, fit on external evidence only"}
    json.dump(cfg, (H / "router_inference" / "config" / f"{args.name}.json").open("w"), indent=1)
    print(f"\nwrote {args.name} to {H}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
