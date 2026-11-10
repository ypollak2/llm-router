"""Estimator error vs real prompt_eval_count (numbers only; never prompt text).

Part 1: proxy ledger rows carrying both ``local_mode.est_prompt_tokens`` (the
calibrated formula) and ``backend_usage.prompt_tokens`` (Ollama's count).
Part 2: the 7 real qwen payloads in tests/test_proxy_local_mode.py::CALIBRATION
(chars, digits, real), scored with every local-path formula. The ledger holds
no payload text, so the other formulas can only be scored on Part 2.
Usage: python scripts/est_calibration.py [proxy_calls.jsonl]
"""
from __future__ import annotations

import json
import math
import os
import statistics
import sys

CALIBRATION = [(51541, 490, 12229), (78492, 678, 18494), (54127, 3645, 16157), (65203, 6865, 22077),
               (63645, 435, 15246), (52281, 1507, 15159), (76281, 10085, 27997)]


def stats(pairs: list[tuple[int, int]]) -> dict:
    ratios = [e / r for e, r in pairs]
    errs = sorted(abs(e - r) / r for e, r in pairs)
    return {"n": len(pairs), "median_ratio": round(statistics.median(ratios), 3),
            "p95_abs_err": round(errs[min(len(errs) - 1, math.ceil(0.95 * len(errs)) - 1)], 3),
            "under": sum(e < r for e, r in pairs)}


def formulas(chars: int, digits: int) -> dict[str, int]:
    return {"calibrated": math.ceil(1.1 * digits + (chars - digits) / 3.6),
            "guard 3.5x1.15": math.ceil(chars / 3.5 * 1.15),
            "chars/4 (compact)": chars // 4,
            "chars/3 (runtimes)": math.ceil(chars / 3.0)}


def main(path: str) -> None:
    pairs = []
    for line in open(path):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        est = (d.get("local_mode") or {}).get("est_prompt_tokens")
        real = (d.get("backend_usage") or {}).get("prompt_tokens")
        if isinstance(est, int) and isinstance(real, int) and real > 0:
            pairs.append((est, real))
    print("ledger (calibrated est vs prompt_eval_count):", stats(pairs) if pairs else "no rows")
    for name in formulas(1, 0):
        print("fixtures", name, stats([(formulas(c, d)[name], r) for c, d, r in CALIBRATION]))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/.llm-router/proxy_calls.jsonl"))
