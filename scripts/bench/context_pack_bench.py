#!/usr/bin/env python3
"""Wall-clock benchmark for ``context_pack.build_pack`` on synthetic transcripts.

PLAN v16 P1.1: build p95 <= 50 ms on a 10 MB transcript. Run under a scratch
HOME (it writes only to a temp dir):

    HOME=$(mktemp -d) python scripts/bench/context_pack_bench.py [--project-root PATH]

Prints n, p50 and p95 per case. Synthetic text only; no real transcript is read.
"""
from __future__ import annotations

import argparse
import json
import math
import os as _os
import statistics
import tempfile
import time
from pathlib import Path

# M-02: any ledger row written while this runs is a benchmark row, not production.
_os.environ.setdefault("LLM_ROUTER_SYNTHETIC", "1")

from llm_router import context_pack as cp  # noqa: E402


def write_transcript(path: Path, target_bytes: int) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        size, n = 0, 0
        while size < target_bytes:
            role = "user" if n % 2 == 0 else "assistant"
            line = json.dumps({"type": role, "message": {
                "role": role, "id": f"m{n}" if role == "assistant" else None,
                "content": f"synthetic turn {n} " + "p" * 900}}) + "\n"
            fh.write(line)
            size += len(line.encode())
            n += 1


def run(label: str, n: int, **kw) -> None:
    cp.build_pack("q", target_window=200_000, door="hook", **kw)  # warm imports
    samples = []
    for _ in range(n):
        t0 = time.perf_counter()
        cp.build_pack("q", target_window=200_000, door="hook", **kw)
        samples.append((time.perf_counter() - t0) * 1000)
    samples.sort()
    p95 = samples[math.ceil(0.95 * n) - 1]
    print(f"{label}: n={n} p50={statistics.median(samples):.2f} ms p95={p95:.2f} ms")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--project-root", default=None)
    args = ap.parse_args()
    with tempfile.TemporaryDirectory() as d:
        big, small = Path(d) / "big.jsonl", Path(d) / "small.jsonl"
        write_transcript(big, 10 * 1024 * 1024)
        write_transcript(small, cp.TAIL_BYTES - 5000)
        run("10 MB transcript", args.n, transcript_path=str(big))
        run("1.99 MB transcript (parsed whole)", args.n, transcript_path=str(small))
        if args.project_root:
            run("10 MB transcript + project_root", max(10, args.n // 3),
                transcript_path=str(big), project_root=args.project_root)


if __name__ == "__main__":
    main()
