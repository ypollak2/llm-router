#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Which candidate models have a cost we can actually predict — from our own data only.

E1.1 found the cost axis the Lagrangian compared was uncorrelated with reality (Spearman
rho = -0.048), because two models were priced 78-80x too low. That is not a pricing error:
RouterArena's published per-token prices are what we already used. It is a **token-count**
error. Our labelling disabled reasoning and capped output, so for models that emit reasoning
tokens we measured a floor, not an estimate, and then traded it off as though it were exact.

The repair has to come from external data, or it is fitting to the benchmark. It can: the
labelling harness recorded, per call, both the output length and the cap it was given, and a
model tells you it is unpredictable in one of two ways.

* **Over cap** — it exceeded the requested ceiling outright. Reasoning tokens bill as
  completion tokens but are not bounded by ``max_tokens``, so the ceiling silently fails to
  hold. `qwen3.5-flash` ran 100% over cap at 6,064 output tokens against a mean cap of 60.
* **Saturating the cap** — it finished exactly at the ceiling. The cap was binding, so its
  natural length is unknown and everything above the ceiling is unmeasured. `qwen3.5-9b` came
  in at a mean of 60.2 tokens against a mean cap of 60.2: a perfect 1.00 ratio, which is not a
  coincidence but a truncation.

In both cases our measured cost is a **lower bound**. A model whose cost is a lower bound
cannot be traded against one whose cost is measured -- ``accuracy - lambda * cost`` will always
prefer the one whose bill has not arrived yet. So the criterion is: **a model is a candidate
only if we have measured, not bounded, what it costs.**

This uses ``data/outcomes/*.jsonl`` -- our own runs on the audited external corpus. No
RouterArena data is read, and the resulting verdict can be reproduced from the repo alone.

Usage::

    python scripts/routerarena/cost_predictability.py
    python scripts/routerarena/cost_predictability.py --json
"""

from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OUTCOMES = REPO / "data" / "outcomes"

# A run that never hit its ceiling tells us the true length; one that sat on the ceiling does
# not. 0.75 leaves room for normal variation while still catching truncation, which shows up
# as a ratio pinned at or above 1.0.
SATURATION_LIMIT = 0.75
OVER_CAP_LIMIT = 0.02


def measure(paths: list[Path]) -> dict[str, dict]:
    """Per model: over-cap rate and mean output-length-to-cap ratio, worst case across runs.

    Worst case, not mean, because a single run in which the model ignored the cap proves the
    cap does not hold it -- a later run where the disable happened to work does not unprove it.
    """
    per_run: dict[str, dict[str, list]] = collections.defaultdict(
        lambda: collections.defaultdict(lambda: [0, 0, 0.0, 0.0])
    )
    for path in paths:
        for line in path.open():
            r = json.loads(line)
            if r.get("error") or not r.get("cap"):
                continue
            t = per_run[path.stem][r["model"]]
            t[0] += int(bool(r.get("over_cap")))
            t[1] += 1
            t[2] += r["out_tokens"]
            t[3] += r["cap"]

    out: dict[str, dict] = {}
    for run, models in per_run.items():
        for model, (over, n, out_tok, cap) in models.items():
            if n < 10:
                continue
            rec = {
                "over_cap_rate": over / n,
                "saturation": (out_tok / n) / (cap / n) if cap else 0.0,
                "mean_out_tokens": out_tok / n,
                "mean_cap": cap / n,
                "n": n,
                "run": run,
            }
            prev = out.get(model)
            if prev is None or (rec["over_cap_rate"], rec["saturation"]) > (
                prev["over_cap_rate"], prev["saturation"]
            ):
                out[model] = rec
    return out


def verdict(stats: dict[str, dict]) -> dict[str, dict]:
    for model, s in stats.items():
        reasons = []
        if s["over_cap_rate"] > OVER_CAP_LIMIT:
            reasons.append(
                f"exceeded the output cap on {s['over_cap_rate'] * 100:.0f}% of calls "
                f"({s['mean_out_tokens']:.0f} tokens against a {s['mean_cap']:.0f} cap)"
            )
        if s["saturation"] >= SATURATION_LIMIT:
            reasons.append(
                f"finished at the cap (out/cap = {s['saturation']:.2f}), so its true "
                "length is unmeasured"
            )
        s["predictable"] = not reasons
        s["reasons"] = reasons
    return stats


def candidate_models(
    paths: list[Path] | None = None, restrict_to: list[str] | None = None
) -> list[str]:
    """The pool a cost-aware fit is allowed to choose from.

    ``restrict_to`` keeps a local-only model (``qwen3.5:latest`` from the Ollama dry run) out
    of a pool that has to be servable through OpenRouter.
    """
    paths = paths or sorted(OUTCOMES.glob("*.jsonl"))
    keep = [m for m, s in verdict(measure(paths)).items() if s["predictable"]]
    if restrict_to is not None:
        keep = [m for m in keep if m in set(restrict_to)]
    return sorted(keep)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    stats = verdict(measure(sorted(OUTCOMES.glob("*.jsonl"))))
    if args.json:
        print(json.dumps(stats, indent=2, sort_keys=True))
        return 0

    print(f"{'model':<36}{'over cap':>10}{'out/cap':>10}{'worst run':>12}  verdict")
    for model in sorted(stats, key=lambda m: (not stats[m]["predictable"], m)):
        s = stats[model]
        mark = "candidate" if s["predictable"] else "EXCLUDED"
        print(f"{model.split('/')[-1]:<36}{s['over_cap_rate'] * 100:>9.1f}%"
              f"{s['saturation']:>10.2f}{s['run']:>12}  {mark}")
    print()
    for model in sorted(stats):
        for reason in stats[model]["reasons"]:
            print(f"  {model.split('/')[-1]}: {reason}")
    keep = [m for m, s in stats.items() if s["predictable"]]
    print(f"\n{len(keep)} of {len(stats)} models have a measured cost; the rest have a floor.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
