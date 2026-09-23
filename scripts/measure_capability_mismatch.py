#!/usr/bin/env python3
"""How often is a prompt routed to something that cannot serve it? (F-2, Phase 0.5a)

`architecture/IMPLEMENTATION_PLAN.md` Phase 0.5 says the capability filter starts
with a shadow measurement, because **a filter that never excludes anything is
indistinguishable from a broken one**. This is that measurement. It changes
nothing; it only counts.

    $ python scripts/measure_capability_mismatch.py

What it counts, and why that is the right thing to count
--------------------------------------------------------

The two halves of "capability" in this repo do not speak the same language:

* what a TASK needs — `capabilities.CapabilityRequirement`: `read_files`,
  `write_files`, `run_commands`, `repo_search`, `git_operations`,
  `network_access`, `objective_verification`, `multi_step_execution`.
* what a MODEL offers — `model_registry.ModelMetadata.capabilities`:
  `"vision"`, `"function-calling"`, `"json"`.

There is no mapping between them, so "this task needs to read the repo" cannot
be checked against a model record at all. That is a deeper gap than "the filter
is not wired": there is nothing for a filter to compare.

And the decisive half is not the model. **Filesystem access is a property of the
execution HARNESS, not of the model.** `gateway.py:_refuse_tools_if_present`
returns HTTP 400 for any request carrying `tools`/`tool_choice`, and
`router._call_text` returns plain text only — so a prompt needing repo access is
unserviceable through those doors *whatever model is chosen*.

So this script measures the harness mismatch, which is the one that costs:

    prompts needing tools, routed to a text-only door

Observed live on 2026-09-23: "check if agenticgraphs accepts an injected runner"
was classified `research/moderate` and routed to a stateless model with no
filesystem — 3 routed calls, ~96s wall clock, zero information produced, against
a claimed saving of $0.0030.
"""

from __future__ import annotations

import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import groundtruth.sources as sources  # noqa: E402

from llm_router import classify  # noqa: E402
from llm_router.capabilities import detect_capabilities  # noqa: E402

MIN_N = 50  # CLAUDE.md: below ~50 real prompts, say "too few to tell".


def collect() -> list[str]:
    """Real prompts, with the documented drop rules applied.

    Reuses `groundtruth/sources.py` deliberately — two ad-hoc parsers of this
    repo's traffic have already disagreed with each other.
    """
    kept: list[str] = []
    for name, reader in sources.READERS.items():
        try:
            for rec in reader():
                if sources.classify_drop(rec.text, rec.session_id,
                                         rec.workspace_is_sandbox):
                    continue
                kept.append(rec.text)
        except Exception as exc:  # noqa: BLE001 — one dead source must not hide the rest
            print(f"  reader {name}: {type(exc).__name__}: {exc}", file=sys.stderr)
    return kept


def main() -> int:
    prompts = collect()
    n = len(prompts)
    print(f"real prompts after drops: n={n}")
    if n < MIN_N:
        print(f"\ntoo few to tell (n={n} < {MIN_N})")
        return 1

    needs_tools = 0
    by_task: collections.Counter = collections.Counter()
    needs_by_task: collections.Counter = collections.Counter()
    unconfident_and_needs = 0
    examples: list[tuple[str, str]] = []

    for text in prompts:
        sig = classify.classify_signals(text)
        task = sig.task_type.value
        by_task[task] += 1
        try:
            req = detect_capabilities(text, task).required
        except Exception:  # noqa: BLE001 — never raises, but do not trust that here
            continue
        if req.needs_tools:
            needs_tools += 1
            needs_by_task[task] += 1
            if not sig.confident:
                unconfident_and_needs += 1
            if len(examples) < 5:
                examples.append((task, text[:70].replace("\n", " ")))

    def pct(k: int) -> str:
        return f"{k}/{n} = {100 * k / n:.1f}%"

    print()
    print("  ── the mismatch ──")
    print(f"  prompts whose detected capabilities need TOOLS .... {pct(needs_tools)}")
    print(f"     ...of which the classifier was NOT confident ... "
          f"{unconfident_and_needs}/{needs_tools if needs_tools else 1} = "
          f"{100 * unconfident_and_needs / needs_tools if needs_tools else 0:.1f}%")
    print()
    print("  Every one of these is unserviceable through the gateway")
    print("  (_refuse_tools_if_present -> HTTP 400) and through _call_text,")
    print("  which returns plain text only — regardless of which model is picked.")

    print()
    print("  ── by task type ──")
    for task, total in by_task.most_common():
        need = needs_by_task.get(task, 0)
        share = 100 * need / total if total else 0.0
        print(f"  {task:<12} {need:>5}/{total:<5} need tools = {share:5.1f}%")

    if examples:
        print()
        print("  ── examples ──")
        for task, text in examples:
            print(f"  [{task}] {text}")

    print()
    if needs_tools == 0:
        print("  VERDICT: no prompt needs tools. A capability filter would")
        print("  exclude nothing — do NOT build it; fix the detector instead.")
        return 0
    if 100 * needs_tools / n < 1:
        print("  VERDICT: under 1%. Phase 0.5b is not justified by volume;")
        print("  its value would be avoiding a rare catastrophic mis-route.")
    else:
        print("  VERDICT: material. Phase 0.5b is justified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
