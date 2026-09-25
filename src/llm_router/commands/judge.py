#!/usr/bin/env python3
"""llm-router judge — drain the out-of-band LLM-judge grading queue.

Command: uv run llm-router judge drain [--batch-size N] [--time-budget SECONDS]

CHZ-JUDGE-QUEUE. The routing hot path only enqueues sampled responses to
``~/.llm-router/judge_queue.jsonl`` (``judge.enqueue_for_grading``) — it makes
no network call and schedules no background task, so nothing grades a queued
response until something drains the queue. This command is that something:
it is also spawned detached from every session start
(``hooks/session-start.py::_drain_judge_queue_bg``), so running it by hand is
normally only needed to grade on demand or to check what it does.

Grading uses an INDEPENDENT judge model — never the model that produced the
response being graded (see ``judge._select_judge_model``). When no
independent judge is available (e.g. no Ollama models installed besides the
one that answered), the row is left ungraded rather than scored with a
fabricated or zero value.
"""

from __future__ import annotations

import argparse
import asyncio
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="llm-router judge",
        description="Drain the out-of-band LLM-judge grading queue.",
    )
    sub = parser.add_subparsers(dest="action")
    drain_p = sub.add_parser("drain", help="grade queued responses with an independent judge model")
    drain_p.add_argument(
        "--batch-size", type=int, default=50,
        help="maximum queued items to claim in this drain (default: 50)",
    )
    drain_p.add_argument(
        "--time-budget", type=float, default=20.0,
        help="maximum wall-clock seconds to spend grading (default: 20.0)",
    )

    args = parser.parse_args(argv)
    if args.action != "drain":
        parser.print_help(file=sys.stderr)
        return 2

    from llm_router.judge import drain_queue

    result = asyncio.run(
        drain_queue(batch_size=args.batch_size, time_budget_s=args.time_budget)
    )
    print(
        f"judge drain: graded={result['graded']} ungraded={result['ungraded']} "
        f"failed={result['failed']} requeued={result['requeued']}"
    )
    return 0


def cmd_judge(argv: list[str] | None = None) -> int:
    """Alias kept for symmetry with the other `cmd_<name>` dispatchers in cli.py."""
    return main(argv)
