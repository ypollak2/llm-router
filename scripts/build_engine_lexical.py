#!/usr/bin/env python3
"""Fit the engine's L3 lexical model from labelled rows and write the artifact.

Input JSONL, one object per line: ``{"text": "...", "tier": "haiku|sonnet|opus"}``.
Output: ``--out`` (default ``$LLM_ROUTER_HOME/engine_lexical.json``). Deterministic:
the same rows and ``--seed`` give a byte-identical file. Exits 2 on an empty or
single-class input (a model fitted on nothing must not ship).
"""
from __future__ import annotations

import argparse
import json
import sys

from llm_router import ml_lexical
from llm_router.paths import state_path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rows", required=True)
    ap.add_argument("--out", default=str(state_path("engine_lexical.json")))
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args(argv)
    texts, tiers = [], []
    with open(a.rows) as fh:
        for line in fh:
            if line.strip():
                r = json.loads(line)
                texts.append(r["text"])
                tiers.append(r["tier"])
    try:
        model = ml_lexical.fit(texts, tiers, seed=a.seed)
    except ValueError as exc:
        print("build_engine_lexical: %s" % exc, file=sys.stderr)
        return 2
    ml_lexical.save(model, a.out)
    print(json.dumps({"n": len(texts), "classes": list(model.classes), "version": model.version, "out": a.out}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
