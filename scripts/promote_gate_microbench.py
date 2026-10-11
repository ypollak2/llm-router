#!/usr/bin/env python3
"""Microbench of the P0.15 promote gate (DISC-PROMOTE-1).

Runs in a scratch LLM_ROUTER_HOME (it makes one if none is set) and never touches the real one.
Prints N, window and the numbers; nothing is written to the repo.

    LLM_ROUTER_HOME=/tmp/scratch python scripts/promote_gate_microbench.py [--n 20000] [--cold 15]

Measured: (1) `order_for_chain` warm, per call; (2) the hook `_build_chain` with the gate on and
off; (3) a cold hook process (interpreter + imports + one chain), gate on vs off.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def _setup_home() -> Path:
    home = Path(os.environ.get("LLM_ROUTER_HOME") or tempfile.mkdtemp(prefix="bench-promote-gate-"))
    os.environ["LLM_ROUTER_HOME"] = str(home)
    home.mkdir(parents=True, exist_ok=True)
    (home / "discovery.json").write_text(json.dumps({
        "cached_at": time.time(),
        "models": {f"ollama/m{i}:7b": {"model_id": f"ollama/m{i}:7b"} for i in range(6)},
    }))
    (home / "usage.db").write_bytes(b"")
    return home


def _time(fn, n: int) -> tuple[float, float, float]:
    """(mean, median, p95) microseconds per call, over n calls."""
    samples = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1e6)
    samples.sort()
    return statistics.fmean(samples), statistics.median(samples), samples[int(0.95 * (len(samples) - 1))]


COLD = (
    "import os,sys,time;t=time.perf_counter();"
    "from llm_router.hooks import chain_builder as cb;"
    "cb.build_chain('simple','green','code');"
    "print(round((time.perf_counter()-t)*1000,3))"
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20000)
    ap.add_argument("--cold", type=int, default=15)
    args = ap.parse_args()
    home = _setup_home()

    from llm_router import model_discovery as md
    from llm_router.hooks import chain_builder as cb

    models = [f"ollama/m{i}:7b" for i in range(6)]
    md.ensure_grandfather(models[:3])
    os.environ["OLLAMA_BUDGET_MODELS"] = "m0:7b"
    md.available_ollama_models = lambda *a, **k: [m.removeprefix("ollama/") for m in models]  # isolate the gate from I/O

    rows = {}
    os.environ.pop("LLM_ROUTER_PROMOTE_GATE", None)
    rows["order_for_chain, 6 models, gate on"] = _time(lambda: md.order_for_chain(models, "query", critical=False), args.n)
    rows["hook _build_chain(simple,code), gate on"] = _time(lambda: cb._build_chain("simple", "green", "code"), args.n // 4)
    os.environ["LLM_ROUTER_PROMOTE_GATE"] = "off"
    rows["order_for_chain, 6 models, gate off"] = _time(lambda: md.order_for_chain(models, "query", critical=False), args.n)
    rows["hook _build_chain(simple,code), gate off"] = _time(lambda: cb._build_chain("simple", "green", "code"), args.n // 4)
    os.environ.pop("LLM_ROUTER_PROMOTE_GATE")

    print(f"home={home}  python={sys.version.split()[0]}  n={args.n}")
    print(f"{'case':<46}{'mean us':>10}{'median us':>11}{'p95 us':>9}")
    for name, (mean, med, p95) in rows.items():
        print(f"{name:<46}{mean:>10.2f}{med:>11.2f}{p95:>9.2f}")

    # cold process: a hook is a fresh interpreter every prompt
    cold = {}
    for gate in ("on", "off"):
        env = dict(os.environ, OLLAMA_BUDGET_MODELS="m0:7b")
        if gate == "off":
            env["LLM_ROUTER_PROMOTE_GATE"] = "off"
        runs = []
        for _ in range(args.cold):
            out = subprocess.run([sys.executable, "-c", COLD], env=env, capture_output=True, text=True, timeout=60)
            runs.append(float(out.stdout.strip().splitlines()[-1]))
        cold[gate] = runs
    for gate, runs in cold.items():
        print(f"cold hook process, import+one chain, gate {gate:<3}: median {statistics.median(runs):.1f} ms, "
              f"min {min(runs):.1f}, max {max(runs):.1f} (n={len(runs)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
