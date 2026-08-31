"""``llm_router policy …`` — Plan 07 Cat G policy-side CLI.

Today this hosts the policy diff tool from G.2; future policy-related
operational commands (validate, lint, freeze) will land alongside it.

The diff subcommand reads sample subjects either from a JSONL file
(``--samples path.jsonl``) or from a synthesised default set covering the
classifier's main subject taxonomy. The latter makes the tool useful
out-of-the-box for "show me how my two policies differ" without callers
needing to first build a representative prompt corpus.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from llm_router.policy_diff import Sample, diff_policies, format_diff_report
from llm_router.types import TaskType

__all__ = ["cmd_policy"]


# Default sample set used when no --samples file is supplied. Covers the
# subjects that the classifier emits in production, weighted lightly toward
# the ones that move most in policy diffs.
_DEFAULT_SAMPLES: list[Sample] = [
    Sample(id="default:general", subject="general"),
    Sample(id="default:code", subject="code", task_type=TaskType.CODE),
    Sample(id="default:medical", subject="medical"),
    Sample(id="default:legal", subject="legal"),
    Sample(id="default:finance", subject="finance"),
    Sample(id="default:math", subject="math"),
    Sample(id="default:physics", subject="physics"),
    Sample(id="default:history", subject="history"),
]


def cmd_policy(args: list[str]) -> int:
    """Entry point dispatched from :func:`llm_router.cli.main`."""
    if not args:
        return _print_help()
    if args[0] in {"-h", "--help"}:
        return _print_help()
    if args[0] == "diff":
        return _cmd_diff(args[1:])
    print(f"Unknown policy subcommand: {args[0]!r}", file=sys.stderr)
    return _print_help(exit_code=2)


def _cmd_diff(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="llm-router policy diff")
    parser.add_argument("policy_a")
    parser.add_argument("policy_b")
    parser.add_argument(
        "--samples", type=Path, default=None,
        help="JSONL file of {id, subject, task_type?, input_tokens?} rows. "
             "Default set covers the main classifier subjects.",
    )
    opts = parser.parse_args(argv)

    samples = _load_samples(opts.samples) if opts.samples else list(_DEFAULT_SAMPLES)
    if not samples:
        print(
            f"No samples loaded from {opts.samples}; aborting.",
            file=sys.stderr,
        )
        return 2

    try:
        report = diff_policies(opts.policy_a, opts.policy_b, samples)
    except FileNotFoundError as err:
        print(f"Policy not found: {err}", file=sys.stderr)
        return 2

    print(format_diff_report(report))
    return 0


def _load_samples(path: Path) -> list[Sample]:
    """Load Sample rows from a JSONL file; malformed lines are skipped."""
    out: list[Sample] = []
    if not path.is_file():
        return out
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                tt = TaskType(row.get("task_type", "query"))
            except ValueError:
                tt = TaskType.QUERY
            out.append(
                Sample(
                    id=str(row.get("id", f"sample:{len(out)}")),
                    subject=str(row.get("subject", "general")),
                    task_type=tt,
                    input_tokens=int(row.get("input_tokens", 200)),
                )
            )
    return out


def _print_help(exit_code: int = 0) -> int:
    print(
        "Usage: llm-router policy <subcommand> [options]\n"
        "\n"
        "Subcommands:\n"
        "  diff <policy_a> <policy_b> [--samples path.jsonl]\n"
        "        Show per-sample model differences and projected total cost.\n"
    )
    return exit_code
