#!/usr/bin/env python3
"""Does this release regress `classify_signals`? North Star point 7: "a quality
regression blocks a release" — and until this script existed,
`pre-release-verify.sh` had no quality check of any kind. Steps 1-9 verify the
repo is in a shippable STATE (clean tree, tests green, CI green); none of them
ever asked whether the router still routes correctly.

## This is a HAND-LABELLED fixture set, not real traffic — on purpose

CLAUDE.md's groundtruth section (`scripts/groundtruth/sources.py`) records that
after excluding benchmark-sandbox sessions and fixture-shaped session ids from
the production log, "the mechanically-verifiable population from real traffic
was ZERO." There is no corpus of real prompts with a trustworthy human label on
what `task_type`/`complexity` SHOULD have been — the ledger records what the
classifier did, which is exactly what this gate needs to check, not assume.

So `FIXTURES` below is authored by hand: 56 prompts I read and labelled myself,
each with the `(task_type, complexity)` the classifier is designed to produce —
`classify.py`'s intent/topic/format regex tables and its `_TASK_COMPLEXITY_FLOOR`
(generate/code >= moderate, analyze/research >= complex) are the specification
this gate pins against. This is NOT a claim that these 56 prompts represent
production traffic, its complexity mix, or its low-signal rate — see the
measurement below. It is a claim that these 56 specific prompts should classify
a specific way, and a future change that breaks that is a regression worth
stopping a release for.

**Never tune a fixture's expected label to make a broken classifier pass.**
A mismatch is a finding: either the fixture's expectation was wrong (fix the
fixture, and say so in the commit) or the classifier changed behaviour (fix the
classifier, or accept the new baseline deliberately — not by silently editing
this file in the same PR that changed `classify.py`).

## A fixture that can't fail is not coverage (found by independent review, PR #146)

`FIXTURES`' code/generate rows were commented "task-type floor, no keyword" —
implying they prove `_TASK_COMPLEXITY_FLOOR["code"/"generate"] = MODERATE`
does something. Reproduced as WRONG: under `HOOK_POLICY` (`simple_max=0`), a
short code/generate prompt with no keyword already computes base complexity
MODERATE from the plain length fallback (`n <= simple_max` is never true when
`simple_max=0`) — a value IDENTICAL to the floor. Deleting those two floor
entries via monkeypatch leaves every `FIXTURES` row's expected complexity
unchanged: accuracy stays 1.000 and this gate reports PASS on a broken floor.
(The analyze/research rows do NOT have this problem — their floor is COMPLEX,
which differs from HOOK_POLICY's MODERATE fallback, so deleting THOSE floor
entries genuinely breaks 8 fixtures; see the mutation test.)

`FLOOR_FIXTURES` below exists to close exactly this gap: the same prompts,
checked under `ROUTER_POLICY` (`simple_max=599`, `keyword_complexity=False`),
where the length fallback for a short prompt is SIMPLE — so the floor's clamp
to MODERATE is the only thing keeping the expected complexity what it is, and
deleting the floor entry is observable. `classify_signals`' task_type scoring
(`_score_categories`) takes no policy argument, so using a different policy
here changes nothing about which task_type is expected — only which
complexity computation exercises the floor.

## Runs `classify_signals()` only — no network, no models

Deterministic and offline by construction: `classify_signals` (unlike
`classify()`) never calls a model or the network (see its docstring in
classify.py — "Sync fast path... always a valid routing decision"). This script
imports nothing else. A release gate that could hang on a flaky network call or
an unavailable API key would train people to skip it.

## The low-signal share is reported so a gate can't pass by defaulting (S8)

S8 (CLAUDE.md, audit 2026-09-22) found that on n=1571 REAL prompts, 49.8% were
never scored by a category at all and got `policy.low_signal_default` ("query")
by luck, not by measurement — `low_signal_classifications()` exists so that
share is observable instead of assumed. A fixture set that is 90% defaulted
prompts could still show 100% accuracy while testing almost nothing but the
spelling of one default string. This gate prints the share for THIS run so that
number is visible next to the accuracy, not just the accuracy alone.

    python3 scripts/release/classifier_gate.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

from llm_router.classify import (  # noqa: E402
    HOOK_POLICY,
    ROUTER_POLICY,
    classify_signals,
    low_signal_classifications,
    reset_low_signal_counters,
)

# Pinned at HEAD (commit that introduces this gate): FIXTURES measured
# 56/56 = 1.000 under HOOK_POLICY, FLOOR_FIXTURES 4/4 = 1.000 under
# ROUTER_POLICY. A future PR that lowers either number is changing routing
# behaviour, not breaking a typo — treat a drop as a finding, not as "update
# the baseline".
BASELINE = 1.0

# (prompt, expected_task_type, expected_complexity). Grouped by task_type, then
# by why that complexity is expected: the task-type FLOOR (generate/code >=
# moderate, analyze/research >= complex — `_TASK_COMPLEXITY_FLOOR` in
# classify.py) or an explicit keyword hit (_COMPLEXITY_SIMPLE / _COMPLEXITY_
# COMPLEX / _COMPLEXITY_DEEP, checked before the floor and never lowered by it).
FIXTURES: list[tuple[str, str, str]] = [
    # ── query: simple (no floor; short, no keyword) ──
    ("what is a foreign key?", "query", "simple"),
    ("what does HTTP 429 mean?", "query", "simple"),
    ("define idempotent", "query", "simple"),
    ("how does OAuth work?", "query", "simple"),
    ("what's the difference between TCP and UDP?", "query", "simple"),
    ("give me a quick overview of JWT", "query", "simple"),
    ("what is os.path.join for?", "query", "simple"),
    ("briefly explain what a database index is", "query", "simple"),
    # ── query: moderate (long, no keyword — query_moderate_min=400) ──
    ("what does " + "x" * 420 + " mean?", "query", "moderate"),
    # ── query: complex (_COMPLEXITY_COMPLEX keyword) ──
    ("what is the architecture of a distributed system, comprehensively?", "query", "complex"),
    # ── query: deep_reasoning (_COMPLEXITY_DEEP keyword) ──
    ("what is 17 * 24? think step by step", "query", "deep_reasoning"),
    ("explain your reasoning for why the sky is blue", "query", "deep_reasoning"),

    # ── code: moderate (HOOK_POLICY's length fallback for a short, non-query
    # prompt is MODERATE regardless of the floor — see FLOOR_FIXTURES below for
    # a check that actually exercises _TASK_COMPLEXITY_FLOOR["code"]) ──
    ("implement a function that reverses a linked list", "code", "moderate"),
    ("write a test for the login endpoint", "code", "moderate"),
    ("fix the bug in the auto-route classifier", "code", "moderate"),
    ("add a retry handler to the webhook consumer", "code", "moderate"),
    ("refactor the database migration script", "code", "moderate"),
    ("rename the function parse_line to parse_row", "code", "moderate"),
    ("optimize the slow database query", "code", "moderate"),
    ("remove the unused imports from cost.py", "code", "moderate"),
    ("build a cli tool for exporting logs", "code", "moderate"),
    # ── code: complex (_COMPLEXITY_COMPLEX keyword overrides the moderate floor) ──
    ("set up a github actions ci pipeline", "code", "complex"),  # "pipeline"
    ("implement a scalable distributed microservice architecture from scratch", "code", "complex"),
    ("build a production-grade end-to-end pipeline for ingestion", "code", "complex"),
    # ── code: deep_reasoning (_COMPLEXITY_DEEP keyword) ──
    ("walk me through the reasoning behind this recursive algorithm", "code", "deep_reasoning"),

    # ── analyze: complex (task-type floor, no keyword) ──
    ("review this pull request for bugs", "analyze", "complex"),
    ("diagnose why the checkout flow is slow", "analyze", "complex"),
    ("compare Kafka to SQS for this workload", "analyze", "complex"),
    ("what are the pros and cons of microservices here", "analyze", "complex"),
    ("explain why the cache hit rate dropped", "analyze", "complex"),
    ("which is better for this workload, Postgres or MySQL", "analyze", "complex"),
    ("audit this code for security vulnerabilities", "analyze", "complex"),
    ("what went wrong in yesterday's incident", "analyze", "complex"),
    ("assess the performance bottleneck in this query", "analyze", "complex"),
    # ── analyze: deep_reasoning (_COMPLEXITY_DEEP keyword; floor never lowers it) ──
    ("do a root cause analysis, reasoning step by step, of this outage", "analyze", "deep_reasoning"),

    # ── research: complex (task-type floor, no keyword) ──
    ("research the latest LLM routing benchmarks", "research", "complex"),
    ("what's the current pricing for OpenRouter", "research", "complex"),
    ("who acquired Cursor", "research", "complex"),
    ("how much did Anthropic raise in its last round", "research", "complex"),
    ("find the best practices for plugin marketplaces", "research", "complex"),
    ("what happened in the AI industry this week", "research", "complex"),
    ("competitive analysis of model routers", "research", "complex"),
    ("top 5 vector databases this year", "research", "complex"),

    # ── generate: moderate (same caveat as code above — this is HOOK_POLICY's
    # length fallback, not a floor check; see FLOOR_FIXTURES) ──
    ("write a blog post about prompt caching", "generate", "moderate"),
    ("draft an email to the team about the outage", "generate", "moderate"),
    ("brainstorm names for a new CLI tool", "generate", "moderate"),
    ("write a poem about debugging at 2am", "generate", "moderate"),
    ("rewrite this paragraph to be more concise", "generate", "moderate"),
    ("write a checklist for onboarding new engineers", "generate", "moderate"),
    ("draft release notes for v15.2.0", "generate", "moderate"),
    ("write a tweet announcing the launch", "generate", "moderate"),
    # ── generate: complex (_COMPLEXITY_COMPLEX keyword) ──
    ("write a whitepaper on distributed tracing that is comprehensive", "generate", "complex"),

    # ── image (no task-type floor) ──
    ("generate an image of a mountain at sunset", "image", "moderate"),
    ("create a logo for a coffee shop", "image", "moderate"),
    ("draw me a quick sketch of a cat", "image", "simple"),  # "quick" -> _COMPLEXITY_SIMPLE
    ("design a minimalist poster in the style of bauhaus", "image", "moderate"),
]

# Checked under ROUTER_POLICY, not HOOK_POLICY — see "A fixture that can't
# fail is not coverage" above. Under ROUTER_POLICY (simple_max=599,
# keyword_complexity=False) a short prompt's base complexity is SIMPLE, so
# _TASK_COMPLEXITY_FLOOR["code"/"generate"] = MODERATE is the only reason the
# expected complexity is MODERATE and not SIMPLE — deleting the floor entry
# is observable here in a way it is not in FIXTURES above.
FLOOR_FIXTURES: list[tuple[str, str, str]] = [
    ("implement a function that reverses a linked list", "code", "moderate"),
    ("write a test for the login endpoint", "code", "moderate"),
    ("write a blog post about prompt caching", "generate", "moderate"),
    ("draft an email to the team about the outage", "generate", "moderate"),
]


def _check(fixtures: list[tuple[str, str, str]], policy) -> tuple[int, list[str]]:
    """Runs one fixture set under one policy. Returns (correct, [failure text])."""
    correct = 0
    fails: list[str] = []
    for prompt, exp_type, exp_complexity in fixtures:
        signal = classify_signals(prompt, policy)
        got_type, got_complexity = signal.task_type.value, signal.complexity.value
        if got_type == exp_type and got_complexity == exp_complexity:
            correct += 1
        else:
            fails.append(
                f"  {prompt!r}\n"
                f"    expected (task_type={exp_type}, complexity={exp_complexity})\n"
                f"    got      (task_type={got_type}, complexity={got_complexity})  "
                f"score={signal.score} confident={signal.confident}"
            )
    return correct, fails


def main() -> int:
    reset_low_signal_counters()

    hook_correct, hook_fails = _check(FIXTURES, HOOK_POLICY)
    floor_correct, floor_fails = _check(FLOOR_FIXTURES, ROUTER_POLICY)

    correct = hook_correct + floor_correct
    fails = hook_fails + floor_fails
    n = len(FIXTURES) + len(FLOOR_FIXTURES)
    accuracy = correct / n
    decided_by_default, total = low_signal_classifications()
    default_share = decided_by_default / total if total else 0.0

    print(f"accuracy {accuracy:.3f} >= baseline {BASELINE:.3f} (n={n})")
    print(
        f"  FIXTURES (HOOK_POLICY): {hook_correct}/{len(FIXTURES)}"
    )
    print(
        f"  FLOOR_FIXTURES (ROUTER_POLICY, covers the code/generate floor "
        f"HOOK_POLICY can't observe): {floor_correct}/{len(FLOOR_FIXTURES)}"
    )
    print(
        f"decided by low_signal_default: {decided_by_default}/{total} "
        f"({default_share:.1%}) — a gate whose fixtures mostly hit the default "
        "is not measuring the classifier; compare against S8's 49.8% on real "
        "traffic (CLAUDE.md) before trusting a high accuracy number here."
    )

    if accuracy < BASELINE:
        print(f"\n{len(fails)} of {n} fixture(s) FAILED:\n")
        print("\n".join(fails))
        print(
            f"\naccuracy {accuracy:.3f} is below the pinned baseline "
            f"{BASELINE:.3f}. This is a classifier regression: either fix "
            "classify.py, or if the new behaviour is deliberate, update "
            "BASELINE, FIXTURES and/or FLOOR_FIXTURES in this file in a "
            "commit that says why."
        )
        return 1

    print("\nclassifier regression gate: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
