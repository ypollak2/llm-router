#!/usr/bin/env python3
"""How much real traffic is routed by a default instead of by a score (S8).

Every published form of this number — CHANGELOG, CLAUDE.md,
`audit/25_REMEDIATION_PLAN_2.md`, the K4 corpus docstring, the comment above
`_low_signal_classifications` in `classify.py` — came from this script. It is
committed so the figure can be re-derived rather than believed.

    $ python scripts/measure_low_signal_rate.py

It reuses `scripts/groundtruth/sources.py` for prompt collection and dropping.
That is deliberate and CLAUDE.md requires it: two ad-hoc parsers of this repo's
traffic have already disagreed with each other, and the benchmark-sandbox and
synthetic-session rules are subtle enough that a second implementation would be
wrong in a way nobody noticed.

Recorded run, 2026-09-23 on this machine:

    real prompts after drops: n=1571   (dropped 1389)
      score == 0 (NO signal, default decides) .... 651/1571 = 41.4%
      0 < score < 2 (weak, default decides too) .. 132/1571 =  8.4%
      confident .................................. 788/1571 = 50.2%
      gateway vs hook task_type disagree ......... 783/1571 = 49.8%

The last row is the one that matters. It is not a classifier accuracy figure —
nothing here knows the right answer for any prompt. It is the share of real
traffic on which the two doors return different task types *because their
defaults differ*, which is the same thing as the share nothing classified.
"""

from __future__ import annotations

import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import groundtruth.sources as sources  # noqa: E402

from llm_router import classify  # noqa: E402

MIN_N = 50  # CLAUDE.md: below ~50 real prompts, say "too few to tell".


def collect() -> tuple[list[str], collections.Counter]:
    kept: list[str] = []
    drops: collections.Counter = collections.Counter()
    for name, reader in sources.READERS.items():
        try:
            for rec in reader():
                reason = sources.classify_drop(
                    rec.text, rec.session_id, rec.workspace_is_sandbox
                )
                if reason:
                    drops[reason] += 1
                    continue
                kept.append(rec.text)
        except Exception as exc:  # noqa: BLE001 — one dead source must not hide the rest
            print(f"  reader {name}: {type(exc).__name__}: {exc}", file=sys.stderr)
    return kept, drops


def main() -> int:
    prompts, drops = collect()
    n = len(prompts)
    print(f"real prompts after drops: n={n}   (dropped {sum(drops.values())})")
    for reason, count in drops.most_common():
        print(f"    dropped {reason}: {count}")

    if n < MIN_N:
        # A rate without its denominator is not a measurement.
        print(f"\ntoo few to tell (n={n} < {MIN_N})")
        return 1

    scores = [max(classify._score_categories(p).values()) for p in prompts]
    zero = sum(1 for s in scores if s == 0)
    weak = sum(1 for s in scores if 0 < s < classify._CONFIDENCE_THRESHOLD)
    confident = n - zero - weak

    gateway = [
        classify.classify_signals(p, classify.GATEWAY_POLICY).task_type.value
        for p in prompts
    ]
    hook = [
        classify.classify_signals(p, classify.HOOK_POLICY).task_type.value
        for p in prompts
    ]
    disagree = sum(1 for a, b in zip(gateway, hook) if a != b)

    def row(label: str, k: int) -> str:
        return f"  {label:<44} {k}/{n} = {100 * k / n:.1f}%"

    print()
    print(row("score == 0 (NO signal, default decides)", zero))
    print(row(f"0 < score < {classify._CONFIDENCE_THRESHOLD} (weak, default too)", weak))
    print(row("confident", confident))
    print(row("gateway vs hook task_type disagree", disagree))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
