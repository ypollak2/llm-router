#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# lint: ra-leakage-exempt evaluates candidate designs on dev; the escalation model is fit on external data only
"""A cost-first router: cheapest model by default, escalate on a *stable* signal.

Three tracks measured together, because optimising one of them alone is how we ended up with
the current submission -- a constant that scores respectably on Arena and is not a router:

* **Arena** -- accuracy against cost, the headline.
* **Opt.Sel** -- did we pick the *cheapest correct* model. Our constant scores 6.06 here,
  because gemini-flash-lite is almost never the cheapest option that works. Always picking the
  cheapest model scores 75+ with no prediction at all.
* **Robustness** -- does the router choose the same model for a paraphrase of the same
  question. Ours scores 30.00: the shipped bag-of-words classifier flips on **294 of 420**
  paired prompts. Hybrid Router flips 14, Divyam 7. That is a broken classifier, not a design
  trade-off, and it is free to fix -- the robustness file needs no generations, only choices.

The escalation signal is therefore chosen for *stability* as well as accuracy. Features that
survive paraphrase (length band, script, structural markers) flip rarely by construction;
hashed word counts do not, which is exactly what the 294 flips measure.

Everything fit here is fit on the external corpus. sub_10 dev is read only to score frozen
candidate designs.

Usage::

    python scripts/routerarena/cost_first_router.py
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

RA = Path("/private/tmp/claude-501/-Users-yaliandrona/7417d79c-0f09-4af6-ad37-3b363dd4b0d3/"
          "scratchpad/RouterArena")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


replay = _load("replay", REPO / "scripts" / "routerarena" / "replay.py")

CHEAP = "qwen/qwen3-235b-a22b-2507"          # cheapest in the measured pool
STRONG = "google/gemini-3.1-flash-lite-preview"

_CODE = re.compile(r"```|\bdef\s+\w+\s*\(|\bclass\s+\w+\s*[:(]|\breturn\b")
_NONLATIN = re.compile(r"[^\x00-\xFF]{3,}")
_MATH = re.compile(r"\d+\s*[+\-*/^]\s*\d+|\\frac|\\sqrt|\bsolve\b|\bcompute\b", re.I)


def stable_features(prompt: str) -> dict:
    """Signals that survive paraphrase.

    Deliberately coarse. A paraphrase keeps a prompt's length band, its script, and whether it
    contains code or equations; it does not keep the exact word set, which is why the shipped
    bag-of-words classifier flipped on 70% of paraphrases.
    """
    n = len(prompt)
    return {
        "len_band": 0 if n < 300 else 1 if n < 800 else 2 if n < 2000 else 3,
        "code": bool(_CODE.search(prompt)),
        "nonlatin": bool(_NONLATIN.search(prompt)),
        "math": bool(_MATH.search(prompt)),
    }


def make_policy(rule: str):
    """Return a function prompt -> model name."""
    def pick(prompt: str) -> str:
        f = stable_features(prompt)
        if rule == "constant_cheap":
            return CHEAP
        if rule == "long_escalates":
            return STRONG if f["len_band"] >= 2 else CHEAP
        if rule == "long_or_code":
            return STRONG if (f["len_band"] >= 2 or f["code"]) else CHEAP
        if rule == "code_or_math":
            return STRONG if (f["code"] or f["math"]) else CHEAP
        if rule == "very_long":
            return STRONG if f["len_band"] >= 3 else CHEAP
        raise ValueError(rule)
    return pick


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    args = ap.parse_args()

    matrix = replay.load_matrix()
    split = replay.load_split(matrix)
    dev = {k: v for k, v in matrix.items() if split[k] == "dev"}

    # Paraphrase pairs, for the robustness track. No generations needed -- only choices.
    rob = json.loads((RA / "dataset" / "router_robustness.json").read_text())
    rob_prompt = {r["global index"]: r["prompt_formatted"] for r in rob}
    main_prompt = {gi: rec["prompt"] for gi, rec in matrix.items()}
    paired = [gi for gi in rob_prompt if gi in main_prompt]

    print(f"dev {len(dev)} queries | {len(paired)} paraphrase pairs for robustness\n")
    print(f"{'design':<34}{'arena':>8}{'OptSel':>8}{'OptCost':>9}{'flips':>7}{'robust':>8}")

    rows = []
    for label, rule in (("always cheapest (qwen3-235b)", "constant_cheap"),
                        ("escalate: very long only", "very_long"),
                        ("escalate: long", "long_escalates"),
                        ("escalate: long or code", "long_or_code"),
                        ("escalate: code or math", "code_or_math")):
        pick = make_policy(rule)
        picks = {}
        for gi, rec in dev.items():
            m = pick(rec["prompt"])
            picks[gi] = m if m in rec["models"] else CHEAP
        r = replay.score(dev, lambda gi, _rec: picks[gi])
        flips = sum(1 for gi in paired if pick(rob_prompt[gi]) != pick(main_prompt[gi]))
        robust = 100 * (1 - flips / len(paired))
        esc = sum(1 for gi in dev if picks[gi] != CHEAP) / len(dev) * 100
        rows.append((label, r, flips, robust, esc))
        print(f"{label:<34}{r['arena_score'] * 100:>8.2f}{r['opt_sel'] * 100:>8.2f}"
              f"{r['opt_cost'] * 100:>9.2f}{flips:>7}{robust:>8.2f}")

    ref = replay.score(dev, replay.policy_always(STRONG))
    print(f"\n{'current submission (constant)':<34}{ref['arena_score'] * 100:>8.2f}"
          f"{ref['opt_sel'] * 100:>8.2f}{ref['opt_cost'] * 100:>9.2f}{0:>7}{100.0:>8.2f}")
    print(f"{'shipped router (bag-of-words)':<34}{'71.26':>8}{'18.01':>8}{'20.46':>9}"
          f"{294:>7}{30.00:>8}   <-- what is live today")
    print("\nescalation rates: " + ", ".join(f"{l.split(':')[-1].strip()} {e:.0f}%"
                                             for l, _r, _f, _rb, e in rows if e > 0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
