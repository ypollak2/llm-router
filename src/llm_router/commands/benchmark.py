"""``llm-router benchmark …`` — Plan 07 Cat G operational CLI.

Three subcommands:

* ``list`` — names of the registered benchmark runners.
* ``run <name> --policy P --split S [--limit N]`` — load the runner's
  dataset, route each prompt, score, and persist the score for future
  regression detection.
* ``regress --policy P --benchmark B [--split S] [--since V] [--threshold X]``
  — read stored history and surface release-over-release drops.

The CLI is intentionally thin: it parses flags, hands off to the
underlying module (``llm_router.benchmark`` / ``benchmark.regression``),
and formats the output via the module's own ``format_*`` helpers so the
layout stays consistent with non-CLI surfaces.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from llm_router import __version__
from llm_router.benchmark import Prediction, get_runner, list_runners

__all__ = ["cmd_benchmark"]


def cmd_benchmark(args: list[str]) -> int:
    """Entry point dispatched from :func:`llm_router.cli.main`."""
    if not args:
        return _print_help()

    subcommand = args[0]
    rest = args[1:]
    if subcommand == "list":
        return _cmd_list()
    if subcommand == "run":
        return _cmd_run(rest)
    if subcommand == "regress":
        return _cmd_regress(rest)
    if subcommand in {"-h", "--help"}:
        return _print_help()

    print(f"Unknown benchmark subcommand: {subcommand!r}", file=sys.stderr)
    return _print_help(exit_code=2)


# ── list ────────────────────────────────────────────────────────────────────


def _cmd_list() -> int:
    names = list_runners()
    if not names:
        print("No benchmark runners registered.")
        return 0
    print("Registered runners:")
    for n in names:
        print(f"  - {n}")
    return 0


# ── run ─────────────────────────────────────────────────────────────────────


def _cmd_run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="llm-router benchmark run")
    parser.add_argument("name", help="Runner name (see `llm-router benchmark list`)")
    parser.add_argument("--policy", default="standard")
    parser.add_argument("--split", default="full")
    parser.add_argument(
        "--limit", type=int, default=0,
        help="Cap prompts processed (0 = no cap; useful for smoke runs).",
    )
    parser.add_argument(
        "--persist", action="store_true",
        help="Store result in benchmark_results for regression tracking.",
    )
    parser.add_argument(
        "--version", default=__version__,
        help="Version tag to store under (defaults to current llm-router version).",
    )
    opts = parser.parse_args(argv)

    try:
        runner = get_runner(opts.name)
    except KeyError as err:
        print(str(err), file=sys.stderr)
        return 2

    dataset = runner.load_dataset(opts.split)
    if not dataset:
        print(
            f"No dataset found for runner={opts.name!r} split={opts.split!r}. "
            f"Place a JSONL fixture at the runner's dataset_root and retry."
        )
        return 1

    if opts.limit > 0:
        dataset = dataset[: opts.limit]

    return asyncio.run(_run_async(runner, dataset, opts))


async def _run_async(runner, dataset, opts) -> int:
    """Drive ``route_and_call`` for each prompt then score and (optionally) persist.

    Sequential rather than parallel: keeping the loop sequential makes the
    cost/latency numbers in :class:`Prediction` accurately reflect the route
    that actually happened, instead of being skewed by concurrent provider
    rate-limiting.

    Activates the policy named by ``--policy`` so the chains/specialists from
    its YAML file actually drive routing. Without this the run silently uses
    whichever policy was active at process start (typically ``standard``),
    making the policy comparison meaningless.
    """
    from llm_router.policy import get_policy_manager
    from llm_router.router import route_and_call

    try:
        get_policy_manager().set_active_policy(opts.policy)
    except FileNotFoundError as err:
        print(f"Policy {opts.policy!r} not found: {err}", file=sys.stderr)
        return 2

    predictions: list[Prediction] = []
    for prompt in dataset:
        task_type = _coerce_task_type(prompt.task_type)
        try:
            resp = await route_and_call(
                task_type=task_type,
                prompt=prompt.text,
                classification_data={"subject": prompt.subject or "general"},
            )
        except Exception as err:
            # Per-prompt failures don't abort the run — we want a regression
            # report even when a fraction of prompts fail on the provider side.
            print(
                f"[warn] prompt {prompt.id} failed: {err}",
                file=sys.stderr,
            )
            continue
        predictions.append(
            Prediction(
                prompt_id=prompt.id,
                model=resp.model,
                response=resp.content,
                cost_usd=resp.cost_usd,
                latency_ms=resp.latency_ms,
            )
        )

    result = runner.evaluate(predictions, dataset)
    print(f"Benchmark: {result.benchmark} (split={result.split})")
    print(f"Score: {result.score:.4f}  n={result.n_samples}")
    if result.per_subject:
        print("Per subject:")
        for subj, score in sorted(result.per_subject.items()):
            print(f"  {subj:<16} {score:.4f}")

    if opts.persist:
        from llm_router.benchmark.regression import store_result
        await store_result(
            version=opts.version,
            policy=opts.policy,
            benchmark=result.benchmark,
            split=result.split,
            score=result.score,
            n_samples=result.n_samples,
            per_subject=result.per_subject,
        )
        print(f"Stored under version={opts.version} policy={opts.policy}.")
    return 0


def _coerce_task_type(raw: str | None):
    """Map a runner's task_type string to the routing enum, defaulting to QUERY."""
    from llm_router.types import TaskType

    if not raw:
        return TaskType.QUERY
    try:
        return TaskType(raw)
    except ValueError:
        return TaskType.QUERY


# ── regress ─────────────────────────────────────────────────────────────────


def _cmd_regress(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="llm-router benchmark regress")
    parser.add_argument("--policy", required=True)
    parser.add_argument("--benchmark", required=True)
    parser.add_argument("--split", default=None)
    parser.add_argument("--since", default=None, dest="since_version")
    parser.add_argument(
        "--threshold", type=float, default=None,
        help="Drop > threshold flags a regression. Default uses module value (0.005).",
    )
    opts = parser.parse_args(argv)

    from llm_router.benchmark.regression import (
        DEFAULT_DROP_THRESHOLD,
        build_report,
        format_report,
    )

    report = asyncio.run(
        build_report(
            policy=opts.policy,
            benchmark=opts.benchmark,
            split=opts.split,
            since_version=opts.since_version,
            threshold=opts.threshold if opts.threshold is not None else DEFAULT_DROP_THRESHOLD,
        )
    )
    print(format_report(report))
    return 1 if report.has_regressions else 0


# ── help ────────────────────────────────────────────────────────────────────


def _print_help(exit_code: int = 0) -> int:
    print(
        "Usage: llm-router benchmark <subcommand> [options]\n"
        "\n"
        "Subcommands:\n"
        "  list                                 list registered benchmark runners\n"
        "  run <name> --policy P --split S      route a dataset and score it\n"
        "      [--limit N] [--persist] [--version V]\n"
        "  regress --policy P --benchmark B     detect score regressions from stored history\n"
        "      [--split S] [--since V] [--threshold X]\n"
    )
    return exit_code
