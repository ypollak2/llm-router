#!/usr/bin/env python3
"""Evaluate the classifier prompt against a versioned golden set."""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict, dataclass
from typing import Awaitable, Callable

from llm_router.classifier import (
    CLASSIFIER_PROMPT_PATH,
    CLASSIFIER_PROMPT_VERSION,
    classify_complexity,
)


@dataclass(frozen=True)
class GoldenExample:
    prompt: str
    expected_task_type: str
    expected_complexity: str
    note: str


def _examples(
    task_type: str,
    complexity: str,
    note: str,
    prompts: list[str],
) -> list[GoldenExample]:
    return [
        GoldenExample(
            prompt=prompt,
            expected_task_type=task_type,
            expected_complexity=complexity,
            note=note,
        )
        for prompt in prompts
    ]


GOLDEN_SET: list[GoldenExample] = [
    *_examples(
        "query",
        "simple",
        "basic_lookup",
        [
            "What does git status do?",
            "How do I create a Python virtualenv?",
            "What is the capital of Japan?",
            "What port does PostgreSQL use by default?",
            "How do I exit Vim?",
            "What does HTTP 404 mean?",
            "What is 19 multiplied by 7?",
            "What does os.path.join do in Python?",
            "How do I list files in the current directory on macOS?",
            "What is the difference between == and is in Python?",
            "What does npm init create?",
            "How do I check my current git branch?",
            "What is the file extension for a JPEG image?",
            "What does docker ps show?",
            "How many bytes are in a kilobyte?",
            "What does JSON stand for?",
            "What is the default branch name in most new git repos?",
            "How do I comment out a line in YAML?",
            "What does localhost usually resolve to?",
            "What is the purpose of a README file?",
        ],
    ),
    *_examples(
        "generate",
        "moderate",
        "content_creation",
        [
            "Write a friendly follow-up email after a product demo.",
            "Draft a concise release note for a bug-fix deployment.",
            "Create a short onboarding checklist for new engineers.",
            "Write three marketing taglines for a budgeting app.",
            "Draft a polite message asking a teammate to review a PR.",
            "Create a weekly standup update for backend work.",
            "Write a project kickoff agenda for a 30-minute meeting.",
            "Draft a customer apology email for a delayed shipment.",
            "Create a short product announcement for a new dashboard.",
            "Write a hiring outreach note for a senior frontend engineer.",
            "Draft a changelog entry for a performance optimization release.",
            "Create a one-page FAQ for our internal tooling.",
            "Write a launch tweet thread for a developer product.",
            "Draft a support reply explaining how to reset an API key.",
            "Create a concise proposal for adding SSO to our app.",
            "Write a README introduction for a Python CLI project.",
            "Draft a meeting recap with action items and owners.",
            "Create a short feature brief for dark mode settings.",
            "Write a demo script for a new billing page walkthrough.",
            "Draft a blog outline about migrating from REST to GraphQL.",
        ],
    ),
    *_examples(
        "code",
        "moderate",
        "implementation_request",
        [
            "Fix the null pointer in auth.py line 42.",
            "Refactor this React component to use a custom hook.",
            "Implement pagination for the users endpoint.",
            "Add unit tests for the retry helper.",
            "Write a SQL migration to add a created_at column.",
            "Optimize this loop to avoid repeated database queries.",
            "Add a CLI flag for dry-run mode.",
            "Implement exponential backoff for failed webhooks.",
            "Create a FastAPI endpoint to upload CSV files.",
            "Convert this callback-based function to async/await.",
            "Add caching around the settings lookup.",
            "Write a Bash script that backs up a directory to S3.",
            "Fix flaky timestamp assertions in the test suite.",
            "Add structured validation for incoming webhook payloads.",
            "Implement a debounce utility in TypeScript.",
            "Create a Terraform module for an SQS queue and dead-letter queue.",
            "Add a background job to refresh exchange rates hourly.",
            "Generate a Pydantic model for this JSON payload.",
            "Refactor the payment service into smaller functions.",
            "Implement optimistic locking for order updates.",
        ],
    ),
    *_examples(
        "analyze",
        "complex",
        "tradeoff_analysis",
        [
            "Compare event sourcing and CRUD for a multi-region fintech platform.",
            "Evaluate whether we should split this monolith into services next quarter.",
            "Analyze why our p95 latency spikes only during cache warmup.",
            "Assess tradeoffs between Postgres logical replication and Debezium.",
            "Compare Kafka and SQS for a bursty internal event pipeline.",
            "Evaluate the risks of moving authentication from session cookies to JWTs.",
            "Analyze the likely bottlenecks in a 100k RPS API architecture.",
            "Compare row-level security with app-layer authorization for B2B SaaS.",
            "Evaluate CAP tradeoffs for a globally distributed shopping cart service.",
            "Analyze why our retry storm is amplifying downstream outages.",
            "Compare feature flags stored in Redis versus Postgres.",
            "Evaluate whether WebSockets or SSE fit a live ops dashboard better.",
            "Analyze the operational risks of self-hosting vector search.",
            "Compare ClickHouse and BigQuery for product analytics workloads.",
            "Evaluate the blast radius of rotating all customer API keys at once.",
            "Analyze the likely failure modes of a leader-election system in Kubernetes.",
            "Compare blue-green and canary deploys for a regulated healthcare app.",
            "Evaluate the tradeoffs of server-side rendering versus static generation here.",
            "Analyze our current incident response process and where it breaks down.",
            "Compare managed Redis with DynamoDB for session storage at scale.",
        ],
    ),
    *_examples(
        "research",
        "complex",
        "current_information",
        [
            "Research the latest SOC 2 expectations for startup engineering teams.",
            "Find current best practices for rotating AWS access keys in production.",
            "Research recent guidance on PostgreSQL major version upgrade planning.",
            "Find the latest browser support for CSS container queries.",
            "Research current pricing patterns for usage-based API products.",
            "Find recent guidance on incident severity definitions for SaaS teams.",
            "Research the latest OWASP API Security Top 10 changes.",
            "Find current recommendations for PCI tokenization providers.",
            "Research recent compliance expectations for AI feature audit logs.",
            "Find the latest migration advice from REST to GraphQL for public APIs.",
            "Research current benchmarks for open-source vector databases.",
            "Find recent guidance on webhook signature verification patterns.",
            "Research the latest EU guidance on cookie consent for product analytics.",
            "Find the current trade press view on passkeys for consumer apps.",
            "Research recent patterns for multi-tenant feature flag governance.",
            "Find the latest platform support for WebAuthn autofill.",
            "Research current recommended rollout patterns for database schema changes.",
            "Find the latest guidance on secrets scanning in CI pipelines.",
            "Research recent studies on developer productivity measurement pitfalls.",
            "Find current recommendations for disaster recovery testing frequency.",
        ],
    ),
]


async def evaluate_examples(
    classify_fn: Callable[[str], Awaitable[object]] = classify_complexity,
    examples: list[GoldenExample] | None = None,
) -> dict:
    """Run the golden set against a classifier function and summarize the result."""
    examples = examples or GOLDEN_SET

    failures: list[dict] = []
    task_hits = 0
    complexity_hits = 0
    exact_hits = 0

    for example in examples:
        result = await classify_fn(example.prompt)
        actual_task = (
            result.inferred_task_type.value
            if getattr(result, "inferred_task_type", None) is not None
            else "none"
        )
        actual_complexity = result.complexity.value

        task_ok = actual_task == example.expected_task_type
        complexity_ok = actual_complexity == example.expected_complexity
        exact_ok = task_ok and complexity_ok

        task_hits += int(task_ok)
        complexity_hits += int(complexity_ok)
        exact_hits += int(exact_ok)

        if not exact_ok:
            failures.append(
                {
                    "prompt": example.prompt,
                    "note": example.note,
                    "expected_task_type": example.expected_task_type,
                    "expected_complexity": example.expected_complexity,
                    "actual_task_type": actual_task,
                    "actual_complexity": actual_complexity,
                    "confidence": getattr(result, "confidence", None),
                    "classifier_model": getattr(result, "classifier_model", None),
                }
            )

    total = len(examples)
    return {
        "prompt_version": CLASSIFIER_PROMPT_VERSION,
        "prompt_path": str(CLASSIFIER_PROMPT_PATH),
        "total": total,
        "task_hits": task_hits,
        "complexity_hits": complexity_hits,
        "exact_hits": exact_hits,
        "task_accuracy": task_hits / total if total else 0.0,
        "complexity_accuracy": complexity_hits / total if total else 0.0,
        "accuracy": exact_hits / total if total else 0.0,
        "failures": failures,
    }


def _print_report(report: dict, *, show_failures: int = 10) -> None:
    print(f"Classifier prompt: {report['prompt_version']} ({report['prompt_path']})")
    print(f"Examples: {report['total']}")
    print(
        f"Exact accuracy: {report['exact_hits']}/{report['total']} "
        f"({report['accuracy']:.1%})"
    )
    print(
        f"Task accuracy: {report['task_hits']}/{report['total']} "
        f"({report['task_accuracy']:.1%})"
    )
    print(
        f"Complexity accuracy: {report['complexity_hits']}/{report['total']} "
        f"({report['complexity_accuracy']:.1%})"
    )

    failures = report["failures"][:show_failures]
    if failures:
        print("\nFailures:")
        for failure in failures:
            print(f"- [{failure['note']}] {failure['prompt']}")
            print(
                "  expected:"
                f" {failure['expected_task_type']}/{failure['expected_complexity']}"
            )
            print(
                "  got:"
                f" {failure['actual_task_type']}/{failure['actual_complexity']}"
            )


async def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Evaluate only the first N golden examples.",
    )
    parser.add_argument(
        "--fail-below",
        type=float,
        default=0.0,
        help="Exit non-zero when exact accuracy falls below this fraction (0.0-1.0).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the full report as JSON.",
    )
    args = parser.parse_args()

    examples = GOLDEN_SET[: args.limit] if args.limit else GOLDEN_SET
    report = await evaluate_examples(examples=examples)

    if args.json:
        serializable = dict(report)
        serializable["examples"] = [asdict(example) for example in examples]
        print(json.dumps(serializable, indent=2))
    else:
        _print_report(report)

    return 1 if report["accuracy"] < args.fail_below else 0


def main() -> None:
    raise SystemExit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
