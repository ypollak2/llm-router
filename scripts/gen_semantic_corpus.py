#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Self-authored labeled corpus for the semantic_classify centroids.

Emits (prompt, task_type, subject) triples used to build the centroid artifact.
Every prompt is generated from hand-written templates + vocab lists that WE own
— zero benchmark text, no RA wrappers. Seeded and reproducible; train/holdout
are disjoint draws and de-duplicated by normalized hash so a prompt can never
land in both splits (which would inflate held-out accuracy).

This is deliberately distinct from ``bench/routerarena/clean/synthetic_gen.py``: that
file produces *answer-calibration* prompts (computed pseudo-gold for the
confidence cascade); this produces *classification labels* (task_type + subject)
for the discriminative head. Different job, different corpus.

Usage:
    python scripts/gen_semantic_corpus.py --out-dir src/llm_router/data \
        --n-train 600 --n-holdout 150 --seed 7
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

# Vocab pools — generic, self-authored. None of these are benchmark phrases.
_LANGS = ["Python", "TypeScript", "Go", "Rust", "Java", "Ruby", "C++", "Kotlin",
          "Swift", "Scala", "Elixir", "PHP"]
_ARTIFACTS = ["function", "class", "endpoint", "CLI", "parser", "cache layer",
              "retry wrapper", "migration", "webhook handler", "rate limiter",
              "scheduler", "connection pool", "serializer", "state machine",
              "pagination helper", "config loader", "queue consumer"]
_SYS = ["the auth service", "the billing pipeline", "the search index",
        "the message queue", "the upload flow", "the settings page",
        "the notification worker", "the export job", "the checkout form",
        "the onboarding wizard", "the admin dashboard", "the sync daemon"]
_TOPICS_TECH = ["a REST API", "a foreign key", "database indexing", "OAuth",
                "a JWT", "a hash map", "async I/O", "a bloom filter",
                "a semaphore", "eventual consistency", "a B-tree", "memoization",
                "a circuit breaker", "content negotiation", "a WAL"]
_TOPICS_GEN = ["a launch email", "a blog intro", "product copy for a landing page",
               "a changelog entry", "a short bio", "release notes", "a FAQ answer",
               "a welcome message", "a press release", "a newsletter blurb",
               "a tagline", "an onboarding tooltip", "a cover letter", "a pitch"]
_RESEARCH_ENT = ["the AI chip market", "recent Series B rounds in fintech",
                 "the latest EU privacy ruling", "this quarter's cloud revenue",
                 "who acquired which robotics startup", "current mortgage rates",
                 "the newest open-weight models", "grid-scale battery costs",
                 "this month's semiconductor export rules", "recent airline mergers",
                 "the current state of quantum error correction",
                 "which studios greenlit sequels this year"]
_MATH = ["the integral of x^2 from 0 to 3", "whether 91 is prime",
         "the derivative of sin(x)cos(x)", "the sum of the first 20 squares",
         "the roots of x^2 - 5x + 6", "the gcd of 84 and 126",
         "the area under 1/x from 1 to e", "the 10th Fibonacci number",
         "whether 2^13 - 1 is prime", "the limit of (1+1/n)^n"]
_SCI = ["why the sky is blue", "how mRNA vaccines work",
        "what causes ocean acidification", "how a transistor switches",
        "why ice floats", "how photosynthesis fixes carbon",
        "what makes a superconductor work", "how CRISPR edits DNA",
        "why the moon has phases", "how a heat pump moves heat"]
_LAW = ["how consideration works in contract law",
        "the difference between negligence and strict liability",
        "what constitutes fair use", "how promissory estoppel applies",
        "the elements of a valid contract", "when a warranty is implied"]
_BIZ = ["how to price a SaaS tier", "whether to lease or buy equipment",
        "how unit economics affect CAC payback", "when to raise a bridge round",
        "how gross margin drives valuation", "whether to build or buy a feature"]

# Each entry: (task_type, subject, template) with {} slots filled per pool below.
# The subject is our own label for specialist routing; GENERAL where none fits.
_TEMPLATES: list[tuple[str, str, str, str]] = [
    # task_type, subject, template, pool_key
    ("code", "code", "Write a {lang} {artifact}.", "lang_art"),
    ("code", "code", "Refactor {sys} to remove the duplicated logic.", "sys"),
    ("code", "code", "Add a unit test for {sys}.", "sys"),
    ("code", "code", "Fix the failing test in {sys}.", "sys"),
    ("code", "code", "Implement a {artifact} in {lang} with error handling.", "lang_art"),
    ("code", "code", "Wire up the submit button on the settings page.", "none"),
    ("query", "trivia", "What is {topic}? Keep it brief.", "topic_tech"),
    ("query", "general", "Explain {topic} in one paragraph.", "topic_tech"),
    ("query", "general", "Define {topic}.", "topic_tech"),
    ("query", "math", "Compute {math}. Show the number.", "math"),
    ("query", "scientific", "Briefly, {sci}?", "sci"),
    ("analyze", "reasoning", "Compare {topic} with an alternative and recommend one.", "topic_tech"),
    ("analyze", "general", "Review the trade-offs of {biz}.", "biz"),
    ("analyze", "law", "Analyze {law}.", "law"),
    ("analyze", "reasoning", "Debug why {sys} is slow and propose a fix.", "sys"),
    ("analyze", "general", "Assess the security risks in {sys}.", "sys"),
    ("generate", "creative", "Write {gentopic}.", "gen"),
    ("generate", "creative", "Draft {gentopic} in a friendly tone.", "gen"),
    ("generate", "creative", "Rewrite {gentopic} to be more concise.", "gen"),
    ("research", "general", "Research {ent} and summarize the latest.", "research"),
    ("research", "general", "What's the most recent news on {ent}?", "research"),
    ("research", "business", "Find current data on {ent}.", "research"),
    # deep-reasoning-flavored (still task_type analyze/query; complexity handled elsewhere)
    ("analyze", "math", "Prove that {math} equals the claimed value, step by step.", "math"),
    ("analyze", "physics", "Derive from first principles {sci}.", "sci"),
]


def _fill(rng: random.Random, template: str, pool_key: str) -> str:
    def pick(xs):
        return rng.choice(xs)
    subs = {}
    if pool_key == "lang_art":
        subs = {"lang": pick(_LANGS), "artifact": pick(_ARTIFACTS)}
    elif pool_key == "sys":
        subs = {"sys": pick(_SYS)}
    elif pool_key == "topic_tech":
        subs = {"topic": pick(_TOPICS_TECH)}
    elif pool_key == "math":
        subs = {"math": pick(_MATH)}
    elif pool_key == "sci":
        subs = {"sci": pick(_SCI)}
    elif pool_key == "biz":
        subs = {"biz": pick(_BIZ)}
    elif pool_key == "law":
        subs = {"law": pick(_LAW)}
    elif pool_key == "gen":
        subs = {"gentopic": pick(_TOPICS_GEN)}
    elif pool_key == "research":
        subs = {"ent": pick(_RESEARCH_ENT)}
    return template.format(**subs)


def _unique_by_task(rng: random.Random, seen: set[str]) -> dict[str, list[dict]]:
    """Exhaustively draw unique prompts per task_type into balanced buckets.

    Generating per task_type (rather than uniform over templates) prevents the
    high-fanout CODE templates from swamping the corpus, and building the full
    unique pool first lets the caller split each type into disjoint train/holdout
    at a fixed ratio — guaranteeing balance AND no cross-split leakage.
    """
    from llm_router.contamination_audit import normalize

    by_task: dict[str, list[dict]] = {}
    templates_by_task: dict[str, list[tuple]] = {}
    for tt, subj, tmpl, pool in _TEMPLATES:
        templates_by_task.setdefault(tt, []).append((subj, tmpl, pool))

    for tt, tmpls in templates_by_task.items():
        bucket: list[dict] = []
        stall = 0
        # Cap draws generously; stop when we stall (pool exhausted).
        while stall < 400:
            subj, tmpl, pool = rng.choice(tmpls)
            prompt = _fill(rng, tmpl, pool)
            key = normalize(prompt)
            if key in seen:
                stall += 1
                continue
            stall = 0
            seen.add(key)
            bucket.append({"prompt": prompt, "task_type": tt, "subject": subj})
        rng.shuffle(bucket)
        by_task[tt] = bucket
    return by_task


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", type=Path, default=Path("src/llm_router/data"))
    ap.add_argument("--holdout-ratio", type=float, default=0.2)
    ap.add_argument("--per-task-cap", type=int, default=120,
                    help="Max prompts kept per task_type (balances the corpus).")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

    seen: set[str] = set()
    rng = random.Random(args.seed)
    by_task = _unique_by_task(rng, seen)

    train: list[dict] = []
    holdout: list[dict] = []
    for tt, bucket in by_task.items():
        bucket = bucket[: args.per_task_cap]
        n_hold = max(1, int(len(bucket) * args.holdout_ratio))
        holdout.extend(bucket[:n_hold])
        train.extend(bucket[n_hold:])
    rng.shuffle(train)
    rng.shuffle(holdout)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    tp = args.out_dir / "semantic_train.jsonl"
    hp = args.out_dir / "semantic_holdout.jsonl"
    tp.write_text("\n".join(json.dumps(r) for r in train) + "\n", encoding="utf-8")
    hp.write_text("\n".join(json.dumps(r) for r in holdout) + "\n", encoding="utf-8")

    from collections import Counter
    print(f"train → {tp} ({len(train)})  holdout → {hp} ({len(holdout)})")
    print(f"train task_type dist: {dict(Counter(r['task_type'] for r in train))}")
    print(f"holdout task_type dist: {dict(Counter(r['task_type'] for r in holdout))}")
    print(f"disjoint check: {len(set(normalize_all(train)) & set(normalize_all(holdout)))} overlap")
    return 0


def normalize_all(rows):
    from llm_router.contamination_audit import normalize
    return [normalize(r["prompt"]) for r in rows]


if __name__ == "__main__":
    raise SystemExit(main())
