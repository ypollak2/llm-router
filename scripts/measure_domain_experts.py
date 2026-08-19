#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Do the experts RIGHT: measure each candidate pool model per-domain on the
hash-audited MMLU-Pro data, then pick each domain's winner by REAL numbers —
exactly how vLLM builds its experts (not by the priors that lost last time).

Firewall-clean: measures models on MMLU-Pro (public benchmark), never on RA.
The clean subset drops every question that hash-collides with RA's eval set.
Grading is local MCQ letter-match (MMLU-Pro ships answer keys) — no LLM judge.

Output:
  * domain_expert_measurements.json — per (domain, model): accuracy, cost, arena
  * bench/routerarena/clean/domain_model_map.json — REBUILT from the measurements
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from llm_router.contamination_audit import hash_set, prompt_hash  # noqa: E402

_SC = Path("/private/tmp/claude-501/-Users-yaliandrona/"
           "9a763a39-220c-4130-b12d-aba5fa67cc86/scratchpad")
_EP = "https://openrouter.ai/api/v1/chat/completions"

# Candidate pool (OpenRouter ids) + RA pricing $/M (in, out) — same as the eval.
_PRICE = {
    "qwen/qwen3-235b-a22b-2507": (0.071, 0.10),
    "deepseek/deepseek-v4-flash": (0.14, 0.28),
    "deepseek/deepseek-v3.2": (0.28, 0.42),
    "qwen/qwen3-coder-30b-a3b-instruct": (0.07, 0.27),
    "deepseek/deepseek-r1": (0.28, 0.42),
}
_PER_DOMAIN = 25   # prompts per domain per model
_WORKERS = 12


def _load_env() -> None:
    p = Path.home() / ".llm-router" / ".env"
    if p.is_file():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                os.environ.setdefault(k, v)


def arena_score(cost_per_1k, acc, beta=0.1, c_max=200.0, c_min=0.0044):
    cost = max(c_min, min(cost_per_1k, c_max))
    ci = (math.log2(c_max) - math.log2(cost)) / (math.log2(c_max) - math.log2(c_min))
    return ((1 + beta) * acc * ci) / (beta * acc + ci) if (beta * acc + ci) else 0.0


def _call(model, prompt, key, max_tokens=1200):
    payload = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}],
                          "max_tokens": max_tokens, "temperature": 0}).encode()
    req = urllib.request.Request(_EP, data=payload, headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    last = "no-response"
    for _ in range(3):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                d = json.loads(r.read())
                msg = d["choices"][0]["message"].get("content") or ""
                u = d.get("usage", {})
                return msg, u.get("prompt_tokens", 0), u.get("completion_tokens", 0), None
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:80]
            if e.code in (400, 401, 403, 404):
                return "", 0, 0, f"HTTP{e.code}:{body}"
            last = f"HTTP{e.code}"
        except Exception as e:
            last = str(e)[:60]
    return "", 0, 0, last


_LETTER = re.compile(r"\b([A-J])\b")


def _grade_mcq(raw, gold_letter):
    m = re.search(r"answer\s*(?:is|:)?\s*\(?([A-J])\)?", raw, re.I)
    if m:
        return int(m.group(1).upper() == gold_letter.upper())
    letters = _LETTER.findall(raw.upper())
    return int(bool(letters) and letters[-1] == gold_letter.upper())


def _build_sample():
    import pyarrow.parquet as pq
    ra = pq.read_table(_SC / "full.parquet").to_pylist()
    ra_hashes = hash_set([str(r.get("Question", "")) for r in ra if r.get("Question")])
    mp = pq.read_table(_SC / "mmlu_pro_test.parquet").to_pylist()
    import random
    rng = random.Random(29)
    by_dom = defaultdict(list)
    rng.shuffle(mp)
    for r in mp:
        q = str(r["question"])
        if prompt_hash(q) in ra_hashes:
            continue
        dom = str(r["category"])
        if len(by_dom[dom]) >= _PER_DOMAIN:
            continue
        opts = list(r["options"]) if r["options"] is not None else []
        letters = "\n".join(f"{chr(65+i)}. {o}" for i, o in enumerate(opts))
        prompt = f"{q}\n{letters}\n\nAnswer with only the letter of the correct option."
        by_dom[dom].append({"prompt": prompt, "gold": str(r["answer"]), "domain": dom})
    return by_dom


def main() -> int:
    _load_env()
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        print("OPENROUTER_API_KEY missing in ~/.llm-router/.env", file=sys.stderr)
        return 1
    sample = _build_sample()
    total = sum(len(v) for v in sample.values())
    print(f"clean MMLU-Pro sample: {total} prompts across {len(sample)} domains x {len(_PRICE)} models")

    jobs = [(model, it) for model in _PRICE for dom in sample for it in sample[dom]]
    agg = defaultdict(lambda: {"correct": 0, "n": 0, "cost": 0.0, "err": 0})

    def work(job):
        model, it = job
        msg, pt, ct, err = _call(model, it["prompt"], key)
        pin, pout = _PRICE[model]
        cost = (pt * pin + ct * pout) / 1e6
        ok = 0 if err else _grade_mcq(msg, it["gold"])
        return model, it["domain"], ok, cost, bool(err)

    done = 0
    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        for f in as_completed([pool.submit(work, j) for j in jobs]):
            model, dom, ok, cost, err = f.result()
            a = agg[(model, dom)]
            a["correct"] += ok; a["n"] += 1; a["cost"] += cost; a["err"] += int(err)
            done += 1
            if done % 200 == 0:
                print(f"  {done}/{len(jobs)}")

    # Per-domain: pick the model maximizing arena_score(cost, acc) on that domain.
    measurements, new_map = {}, {}
    for dom in sorted(sample):
        rows = []
        for model in _PRICE:
            a = agg[(model, dom)]
            if a["n"] == 0:
                continue
            acc = a["correct"] / a["n"]
            cost_1k = a["cost"] / a["n"] * 1000
            rows.append({"model": model, "acc": round(acc, 3),
                         "cost_1k": round(cost_1k, 4),
                         "arena": round(arena_score(cost_1k, acc), 4),
                         "errors": a["err"]})
        rows.sort(key=lambda r: r["arena"], reverse=True)
        measurements[dom] = rows
        new_map[dom] = rows[0]["model"] if rows else "deepseek/deepseek-v3.2"

    Path("src/llm_router/data/domain_expert_measurements.json").write_text(
        json.dumps(measurements, indent=2))
    overall = defaultdict(lambda: {"correct": 0, "n": 0, "cost": 0.0})
    for (model, dom), a in agg.items():
        o = overall[model]
        o["correct"] += a["correct"]; o["n"] += a["n"]; o["cost"] += a["cost"]
    best_overall = max(overall, key=lambda m: arena_score(
        overall[m]["cost"] / overall[m]["n"] * 1000, overall[m]["correct"] / overall[m]["n"]))
    out = {
        "_provenance": "MEASURED per-domain on hash-audited MMLU-Pro (public benchmark; "
                       "0 overlap with RA). Each domain -> argmax arena_score(cost,acc). "
                       "No RouterArena data. Rebuilt by scripts/measure_domain_experts.py.",
        "_pool": list(_PRICE),
        "default": best_overall,
        "domain": new_map,
    }
    Path("bench/routerarena/clean/domain_model_map.json").write_text(json.dumps(out, indent=2))

    print("\n=== per-domain winner (measured) ===")
    for dom in sorted(new_map):
        top = measurements[dom][0]
        print(f"  {dom:20s} -> {new_map[dom]:38s} acc={top['acc']:.2f} arena={top['arena']:.3f}")
    print(f"\ndefault (best overall by arena): {best_overall}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
