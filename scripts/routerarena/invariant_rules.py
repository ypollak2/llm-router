#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# lint: ra-leakage-exempt evaluates a hand-written rule set on dev; fits nothing, writes no artifact
"""G4 — routing rules written from task semantics, with no fitted parameters.

Every routing attempt so far has failed the same way: a map fitted to our corpus's distribution
does not survive the trip to RouterArena's. E1.3 measured why -- the fitted cluster->model map
carried 0.078 normalised mutual information about which model wins, and a *perfect* such map,
fitted with hindsight, was worth only +1.51 points.

A rule with no fitted parameters cannot mis-transfer, because there is nothing in it that was
tuned to a distribution. That is the entire hypothesis. Its honest prior is poor and it is
included to be falsified cheaply rather than defended.

**The rules must be justifiable from a model card or from the task itself.** Not from any
measurement on our corpus -- the moment a rule is chosen because it scored well, it is a fitted
parameter wearing a rule's clothing, and it inherits every transfer problem we are trying to
escape. Each rule below therefore carries its documentary justification, and the detectors are
written from the task definition (a translation prompt contains non-Latin script or names a
language; a code prompt contains a fence or a signature), not from inspecting what worked.

Usage::

    python scripts/routerarena/invariant_rules.py
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


replay = _load("replay", REPO / "scripts" / "routerarena" / "replay.py")
tg = _load("transfer_gate", REPO / "scripts" / "routerarena" / "transfer_gate.py")

# The default. Chosen on external data in Track F, not here.
CONSTANT = "google/gemini-3.1-flash-lite-preview"

# Rules, each with the documentary basis that justifies it. Only models the sub_10 matrix
# actually measured can be evaluated, which constrains the roster to the original eight --
# a real limitation of this test, stated rather than worked around.
CODE_MODEL = "openai/gpt-oss-120b"          # card: trained for code and agentic tool use
MULTILINGUAL_MODEL = "qwen/qwen3-235b-a22b-2507"  # card: 119 languages and dialects

_FENCE = re.compile(r"```|\bdef\s+\w+\s*\(|\bclass\s+\w+|\breturn\b|\bfunction\s+\w+\s*\(")
_LANG_WORD = re.compile(
    r"\btranslat\w*\b|\binto (English|German|French|Russian|Chinese|Czech|Finnish)\b",
    re.I,
)
# Any run of characters outside Latin-1: Chinese, Cyrillic, Devanagari and so on. A prompt
# carrying them is a translation or multilingual task by construction, not by measurement.
_NON_LATIN = re.compile(r"[^\x00-\xFF]{3,}")


def route(prompt: str) -> str:
    """One model name, from the prompt alone, by rule."""
    if _FENCE.search(prompt):
        return CODE_MODEL
    if _LANG_WORD.search(prompt) or _NON_LATIN.search(prompt):
        return MULTILINGUAL_MODEL
    return CONSTANT


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    args = ap.parse_args()

    matrix = replay.load_matrix()
    split = replay.load_split(matrix)
    dev = {k: v for k, v in matrix.items() if split[k] == "dev"}

    picks, fired = {}, {"code": 0, "multilingual": 0, "constant": 0}
    for gi, rec in dev.items():
        m = route(rec["prompt"])
        if m not in rec["models"]:
            m = CONSTANT
        picks[gi] = m
        fired["code" if m == CODE_MODEL else
              "multilingual" if m == MULTILINGUAL_MODEL else "constant"] += 1

    n = len(dev)
    print(f"dev: {n} queries")
    for k, v in fired.items():
        print(f"  {k:<14}{v:>5} ({v / n * 100:.1f}%)")

    rules = replay.score(dev, lambda gi, _r: picks[gi])
    const = replay.score(dev, replay.policy_always(CONSTANT))
    best_const_model, best_const = None, None
    for m in sorted({x for rec in dev.values() for x in rec["models"]}):
        r = replay.score(dev, replay.policy_always(m))
        if best_const is None or r["arena_score"] > best_const["arena_score"]:
            best_const, best_const_model = r, m

    print(f"\n{'policy':<40}{'acc':>9}{'$/1k':>10}{'arena':>9}")
    print(f"{'invariant rules':<40}{rules['accuracy'] * 100:>8.2f}%"
          f"{rules['cost_per_1k']:>10.4f}{rules['arena_score'] * 100:>9.2f}")
    print(f"{'shipped constant (' + CONSTANT.split('/')[-1] + ')':<40}"
          f"{const['accuracy'] * 100:>8.2f}%{const['cost_per_1k']:>10.4f}"
          f"{const['arena_score'] * 100:>9.2f}")
    print(f"{'best constant (' + best_const_model.split('/')[-1] + ')':<40}"
          f"{best_const['accuracy'] * 100:>8.2f}%{best_const['cost_per_1k']:>10.4f}"
          f"{best_const['arena_score'] * 100:>9.2f}")

    delta = (rules["arena_score"] - best_const["arena_score"]) * 100
    print(f"\nGATE: beat the best constant on dev")
    print(f"  {delta:+.2f} Arena points -> {'PASS' if delta > 0 else 'FAIL'}")
    if delta <= 0:
        print("  As predicted. A rule set with no fitted parameters cannot mis-transfer, but")
        print("  it also cannot capture what it was never told -- and the rules encode only")
        print("  what a model card claims, which is not the same as what a model does on")
        print("  these questions. Routing remains unsupported; the constant stands.")
    return 0 if delta > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
