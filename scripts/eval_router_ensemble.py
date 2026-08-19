#!/usr/bin/env python3
"""Compare router classifiers against the shared golden set (scripts/eval_classifier.py).

Modes (each a ``classify_fn`` fed to ``evaluate_examples``):
  heuristic  — deterministic regex signals only (today's fast path, offline)
  local      — LLM-first: a single local Ollama classifier
  ensemble   — LLM-first blended with the heuristic score, second local model
               tiebreaker on the low-confidence tail (the new design)

Run:  uv run python scripts/eval_router_ensemble.py --modes heuristic,local,ensemble
"""
from __future__ import annotations

import argparse
import asyncio
import time
from dataclasses import dataclass

from llm_router.classify import apply_complexity_floor, classify_signals
from llm_router.ensemble import classify_ensemble, local_llm_classify
from llm_router.types import Complexity, TaskType
from eval_classifier import GOLDEN_SET, evaluate_examples  # type: ignore


@dataclass
class _Adapter:
    """Shape ``evaluate_examples`` expects: ``.inferred_task_type`` + ``.complexity``."""
    inferred_task_type: TaskType | None
    complexity: Complexity
    confidence: float = 0.0
    classifier_model: str = "heuristic"


async def _heuristic_fn(prompt: str) -> _Adapter:
    sig = classify_signals(prompt)
    return _Adapter(sig.task_type, sig.complexity, float(sig.score), "heuristic")


async def _heuristic_floor_fn(prompt: str) -> _Adapter:
    """Free 0ms heuristic + the task-type complexity floor — isolates how much of
    the ensemble's accuracy comes from the floor policy vs the local LLMs."""
    sig = classify_signals(prompt)
    complexity = apply_complexity_floor(sig.complexity, sig.task_type)
    return _Adapter(sig.task_type, complexity, float(sig.score), "heuristic+floor")


def _local_fn(model: str):
    async def fn(prompt: str) -> _Adapter:
        r = await local_llm_classify(prompt, model=model)
        return _Adapter(r.inferred_task_type, r.complexity, r.confidence, r.classifier_model)
    return fn


def _ensemble_fn(primary: str, secondary: str):
    async def fn(prompt: str) -> _Adapter:
        r = await classify_ensemble(prompt, primary=primary, secondary=secondary)
        return _Adapter(r.inferred_task_type, r.complexity, r.confidence, r.classifier_model)
    return fn


async def _run(mode: str, primary: str, secondary: str) -> None:
    if mode == "heuristic":
        fn = _heuristic_fn
    elif mode == "heuristic_floor":
        fn = _heuristic_floor_fn
    elif mode == "local":
        fn = _local_fn(primary)
    elif mode == "ensemble":
        fn = _ensemble_fn(primary, secondary)
    else:
        raise SystemExit(f"unknown mode: {mode}")

    t0 = time.monotonic()
    report = await evaluate_examples(classify_fn=fn, examples=GOLDEN_SET)
    dt = time.monotonic() - t0
    n = report["total"]
    print(
        f"[{mode:9}] exact {report['accuracy']:.0%}  "
        f"task {report['task_accuracy']:.0%}  "
        f"complexity {report['complexity_accuracy']:.0%}  "
        f"| {n} ex, {dt:.1f}s ({dt / n * 1000:.0f}ms/prompt)"
    )
    for f in report["failures"][:6]:
        print(
            f"    ✗ {f['expected_task_type']}/{f['expected_complexity']}"
            f" → {f['actual_task_type']}/{f['actual_complexity']}  {f['prompt'][:60]}"
        )


async def _main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--modes", default="heuristic")
    ap.add_argument("--primary", default="ollama/qwen2.5:7b")
    ap.add_argument("--secondary", default="ollama/qwen2.5-coder:32b")
    ap.add_argument("--per", type=int, default=0, help="stratified: first N of each of the 5 categories")
    args = ap.parse_args()
    global GOLDEN_SET
    if args.per:
        # GOLDEN_SET is 5 contiguous blocks of 20 (query/generate/code/analyze/research).
        GOLDEN_SET = [GOLDEN_SET[b * 20 + i] for b in range(5) for i in range(args.per)]
    for mode in args.modes.split(","):
        await _run(mode.strip(), args.primary, args.secondary)


if __name__ == "__main__":
    asyncio.run(_main())
