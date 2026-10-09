#!/usr/bin/env python3
"""Wall time of the proxy's harness-tag strip (``proxy.steps._human_parts``) on
adversarial and plain user turns (TURNFIRST-1, docs/bugs/TURNFIRST-1.md).

Synthetic text only, no network, no state. Each shape is one text block of the
given lines followed by 3,000,000 ``x`` characters; each figure is the median of
``--repeats`` runs (default 5). The pre-TURNFIRST-1 reminder regex is timed on the
tag-free shape for comparison.

    python3 scripts/bench_harness_strip.py
"""
from __future__ import annotations

import argparse
import os
import re
import statistics
import sys
import time
from pathlib import Path

os.environ.setdefault("LLM_ROUTER_SYNTHETIC", "1")  # benchmark traffic is not production
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from llm_router.proxy import steps  # noqa: E402

PAD = "x" * 3_000_000


def shapes() -> dict[str, str]:
    out = {f"{n:,} line-start '<command-name foo' lines, no '>'":
           "\n".join("<command-name foo" for _ in range(n)) + "\n" + PAD
           for n in (100, 1_000, 10_000, 150_000)}
    out["150,000 uniquely named closed blocks"] = (
        "\n".join(f"<command-name{i}>a</command-name{i}>" for i in range(150_000)) + PAD)
    out["150,000 unclosed mid-line '<system-reminder>'"] = (
        " ".join("a <system-reminder>" for _ in range(150_000)) + PAD)
    out["150,000 inline close tags only"] = "\n".join("q </system-reminder> q" for _ in range(150_000)) + PAD
    out["10,000 attribute runs of 199 chars"] = "\n".join("<command-name " + "a" * 199 for _ in range(10_000)) + PAD
    out["30 KB tag-free text"] = "x " * 15_000
    return out


def median_ms(fn, repeats: int) -> float:
    runs = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn()
        runs.append((time.perf_counter() - t0) * 1000.0)
    return statistics.median(runs)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repeats", type=int, default=5)
    args = ap.parse_args()
    for name, text in shapes().items():
        content = [{"type": "text", "text": text}]
        ms = median_ms(lambda: steps._human_parts(content), args.repeats)
        print(f"{name} ({len(text) / 1e6:.1f} MB): median {ms:.3f} ms (n={args.repeats})")
    old = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
    plain = "x " * 15_000
    ms = median_ms(lambda: old.sub("", plain), args.repeats)
    print(f"pre-TURNFIRST-1 reminder regex on 30 KB tag-free text: median {ms:.3f} ms (n={args.repeats})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
