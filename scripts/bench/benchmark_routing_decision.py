"""WS9 Blocker 5 -- default-path routing-decision micro-benchmark.

Benchmarks the hot, synchronous routing-decision function(s) that run on
every ``route_and_call`` invocation, with all feature flags left at their
default (unset/off) state -- this is the "default path" the release-audit
blocker asks us to compare between ``main`` and this migration's feature
branch.

``pytest-benchmark`` is not installed in this project, so this is a plain
``timeit``-style harness (allowed by the task as the documented fallback).

Benchmarked targets:

1. ``llm_router.router._resolve_profile`` -- the pure, synchronous function
   that resolves (profile, complexity, use_thinking) for a prompt. It is
   unmodified by the WS9 migration and exists identically on both branches,
   so it acts as a *control*: any measurable regression here would indicate
   an environment/import-time issue rather than a WS9 code change.
2. ``llm_router.bounded_operational.bounded_operational_enabled`` -- the new
   WS9 flag-check that ``_dispatch_model_loop`` now calls once per model
   attempt on the default (flag-off) path. Only present on the feature
   branch (it does not exist on ``main``), so it cannot be diffed
   branch-to-branch -- instead we report its absolute cost and show it is
   negligible relative to the control function's own runtime, which is the
   relevant question for "does the default path regress".

Usage:
    uv run --frozen python scripts/bench/benchmark_routing_decision.py --label <name>

Prints a JSON report to stdout with median/p95 wall-clock time (microseconds)
per call, over >=1000 iterations for each benchmarked function.
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

# Make ``src/`` importable regardless of cwd (works from a plain checkout and
# from a `git worktree add` checkout that has no editable install of its own).
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

ITERATIONS = 5000
WARMUP = 200


def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return float("nan")
    k = (len(sorted_values) - 1) * pct
    f = int(k)
    c = min(f + 1, len(sorted_values) - 1)
    if f == c:
        return sorted_values[f]
    d0 = sorted_values[f] * (c - k)
    d1 = sorted_values[c] * (k - f)
    return d0 + d1


def _time_calls(fn, iterations: int = ITERATIONS, warmup: int = WARMUP) -> dict:
    for _ in range(warmup):
        fn()
    samples = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        fn()
        t1 = time.perf_counter()
        samples.append((t1 - t0) * 1_000_000)  # microseconds
    samples.sort()
    return {
        "iterations": iterations,
        "median_us": round(statistics.median(samples), 4),
        "p95_us": round(_percentile(samples, 0.95), 4),
        "mean_us": round(statistics.mean(samples), 4),
        "min_us": round(samples[0], 4),
        "max_us": round(samples[-1], 4),
    }


def _git_rev() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.strip()
    except Exception:
        return "unknown"


def bench_resolve_profile(iterations: int) -> dict:
    from llm_router.router import _resolve_profile
    from llm_router.config import get_config

    config = get_config()

    # Representative inputs covering all three prompt-length heuristic
    # branches (SIMPLE / MODERATE / COMPLEX), with no explicit profile,
    # complexity hint, or classification data -- i.e. the cheapest, most
    # common call shape (default path, no upstream classifier run).
    prompts = [
        "short prompt",  # < 300 chars -> SIMPLE
        "medium length prompt " * 15,  # ~300-3000 chars -> MODERATE
        "long detailed prompt with lots of context " * 100,  # > 3000 chars -> COMPLEX
    ]
    idx = {"i": 0}

    def call():
        p = prompts[idx["i"] % len(prompts)]
        idx["i"] += 1
        return _resolve_profile(None, None, None, p, None, config)

    return _time_calls(call, iterations=iterations)


def bench_bounded_operational_enabled(iterations: int) -> dict | None:
    try:
        from llm_router.bounded_operational import bounded_operational_enabled
    except ImportError:
        return None

    import os

    # Default path: flag unset (falsy).
    os.environ.pop("LLM_ROUTER_BOUNDED_OPERATIONAL", None)

    def call():
        return bounded_operational_enabled()

    return _time_calls(call, iterations=iterations)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", default=None, help="Human label for this run (e.g. branch name).")
    parser.add_argument("--iterations", type=int, default=ITERATIONS)
    args = parser.parse_args()

    report = {
        "label": args.label or "unlabeled",
        "git_rev": _git_rev(),
        "python": sys.version.split()[0],
        "benchmarks": {
            "_resolve_profile": bench_resolve_profile(args.iterations),
            "bounded_operational_enabled": bench_bounded_operational_enabled(args.iterations),
        },
    }
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
